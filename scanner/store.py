"""SQLite (WAL) persistence layer (design doc §2, §4).

WAL mode gives the Streamlit dashboard concurrent reads while the daemon writes.
The Parquet/DuckDB graduation seam (§2) lives HERE: when `quote` / `edge_snapshot`
history outgrows SQLite, swap the write/read methods for those two tables to
partitioned Parquet without touching models, connectors, or the edge engine.

Writes are idempotent where it matters: `market` and `outcome` upsert on their
unique keys so re-discovery on restart does not duplicate rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import EdgeSnapshot, Market, Outcome, Quote

SCHEMA = """
-- Canonical market registry (one row per venue market).
CREATE TABLE IF NOT EXISTS market (
    market_id         TEXT PRIMARY KEY,   -- canonical: f"{venue}:{venue_market_id}"
    venue             TEXT NOT NULL,      -- 'manifold' | 'kalshi' | 'polymarket'
    venue_market_id   TEXT NOT NULL,
    title             TEXT NOT NULL,
    market_type       TEXT NOT NULL,      -- 'binary' | 'multi'
    close_time        TIMESTAMP,
    resolution_time   TIMESTAMP,          -- expected; nullable
    resolution_source TEXT,               -- free text; feeds basis-risk flag
    status            TEXT NOT NULL,      -- 'open' | 'closed' | 'resolved'
    url               TEXT,               -- canonical venue page (dashboard click-through)
    UNIQUE (venue, venue_market_id)
);

CREATE TABLE IF NOT EXISTS outcome (
    outcome_id  TEXT PRIMARY KEY,         -- f"{market_id}:{label}"
    market_id   TEXT NOT NULL REFERENCES market(market_id),
    label       TEXT NOT NULL,            -- 'YES'/'NO' for binary; option text for multi
    UNIQUE (market_id, label)
);

-- Time-series. Highest-volume table -> first candidate for Parquet/DuckDB later.
CREATE TABLE IF NOT EXISTS quote (
    ts          TIMESTAMP NOT NULL,
    outcome_id  TEXT NOT NULL REFERENCES outcome(outcome_id),
    bid         REAL,                     -- [0,1]
    ask         REAL,                     -- [0,1]
    bid_size    REAL,                     -- in shares / contracts
    ask_size    REAL,
    last        REAL
);
CREATE INDEX IF NOT EXISTS idx_quote_outcome_ts ON quote(outcome_id, ts);

-- Computed cross-venue edges over time. The actual research output.
CREATE TABLE IF NOT EXISTS edge_snapshot (
    ts                 TIMESTAMP NOT NULL,
    event_id           TEXT NOT NULL,     -- from the links YAML
    leg_a_outcome_id   TEXT NOT NULL,
    leg_b_outcome_id   TEXT NOT NULL,
    gross_edge         REAL,              -- 1 - (price_a + price_b)
    modeled_fees       REAL,
    lockup_cost        REAL,              -- annualized opp cost of locked capital
    net_edge           REAL,              -- gross - fees - lockup
    executable_size    REAL,              -- min(depth on each leg)
    days_to_resolution REAL,
    basis_risk_flag    INTEGER NOT NULL   -- 0/1, see design doc §6
);
CREATE INDEX IF NOT EXISTS idx_edge_event_ts ON edge_snapshot(event_id, ts);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Store:
    """Thin SQLite wrapper. One instance per process; safe for the single writer."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self.init_schema()

    def _configure(self) -> None:
        # WAL: concurrent dashboard reads while the daemon writes (design doc §2).
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent column adds for DBs created by an earlier schema version.
        (CREATE TABLE IF NOT EXISTS won't add new columns to an existing table.)"""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(market)")}
        if "url" not in cols:
            self.conn.execute("ALTER TABLE market ADD COLUMN url TEXT")

    # --- writes ----------------------------------------------------------

    def upsert_market(self, m: Market) -> None:
        self.conn.execute(
            """
            INSERT INTO market (market_id, venue, venue_market_id, title, market_type,
                                close_time, resolution_time, resolution_source, status, url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                title=excluded.title,
                market_type=excluded.market_type,
                close_time=excluded.close_time,
                resolution_time=excluded.resolution_time,
                resolution_source=excluded.resolution_source,
                status=excluded.status,
                url=excluded.url
            """,
            (
                m.market_id, m.venue, m.venue_market_id, m.title, m.market_type,
                _iso(m.close_time), _iso(m.resolution_time), m.resolution_source, m.status, m.url,
            ),
        )

    def upsert_outcome(self, o: Outcome) -> None:
        self.conn.execute(
            """
            INSERT INTO outcome (outcome_id, market_id, label)
            VALUES (?, ?, ?)
            ON CONFLICT(outcome_id) DO NOTHING
            """,
            (o.outcome_id, o.market_id, o.label),
        )

    def insert_quote(self, q: Quote) -> None:
        self.conn.execute(
            """
            INSERT INTO quote (ts, outcome_id, bid, ask, bid_size, ask_size, last)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_iso(q.ts), q.outcome_id, q.bid, q.ask, q.bid_size, q.ask_size, q.last),
        )

    def insert_quotes(self, quotes: list[Quote]) -> None:
        for q in quotes:
            self.insert_quote(q)

    def insert_edge_snapshot(self, e: EdgeSnapshot) -> None:
        self.conn.execute(
            """
            INSERT INTO edge_snapshot (ts, event_id, leg_a_outcome_id, leg_b_outcome_id,
                                       gross_edge, modeled_fees, lockup_cost, net_edge,
                                       executable_size, days_to_resolution, basis_risk_flag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(e.ts), e.event_id, e.leg_a_outcome_id, e.leg_b_outcome_id,
                e.gross_edge, e.modeled_fees, e.lockup_cost, e.net_edge,
                e.executable_size, e.days_to_resolution, e.basis_risk_flag,
            ),
        )

    # --- reads (dashboard) ----------------------------------------------

    def quote_history(self, outcome_id: str) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT ts, bid, ask, bid_size, ask_size, last FROM quote "
            "WHERE outcome_id = ? ORDER BY ts",
            (outcome_id,),
        )
        return cur.fetchall()

    def edge_history(self, event_id: str) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM edge_snapshot WHERE event_id = ? ORDER BY ts",
            (event_id,),
        )
        return cur.fetchall()

    def list_markets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM market ORDER BY venue, title").fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
