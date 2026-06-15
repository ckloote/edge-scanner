"""Streamlit dashboard (design doc §3) — reads SQLite (WAL) directly, no write path.

Phase 1: counts + canonical tables, plus a per-outcome **price-history chart**
(the phase-1 "done when": a real Manifold market's price history renders here).
The edge-over-time view (phase 4) attaches to the same read-only connection.

Run:  uv run --extra dashboard streamlit run dashboard/app.py
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from scanner.config import Settings

st.set_page_config(page_title="edge-scanner", layout="wide")


@st.cache_resource
def _connect() -> sqlite3.Connection:
    settings = Settings.load()
    # Read-only, WAL: concurrent with the daemon's writes (design doc §2).
    uri = f"file:{settings.scanner.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


st.title("Cross-venue edge scanner")
st.caption("Phase 1 — Manifold end-to-end. Read-only study harness; zero real-money risk.")

try:
    conn = _connect()
except sqlite3.OperationalError:
    st.warning("No database yet. Start the scanner (`uv run edge-scanner`) first.")
    st.stop()

cols = st.columns(4)
for col, table in zip(cols, ("market", "outcome", "quote", "edge_snapshot")):
    col.metric(table, _count(conn, table))

# --- Price history -------------------------------------------------------
st.subheader("Price history")

markets = conn.execute(
    "SELECT market_id, venue, title FROM market ORDER BY venue, title"
).fetchall()

if not markets:
    st.info("No markets ingested yet. Add links to config/links.yaml and run the scanner.")
else:
    labels = {f"[{m['venue']}] {m['title']}": m["market_id"] for m in markets}
    picked = st.selectbox("Market", list(labels))
    market_id = labels[picked]

    outcomes = conn.execute(
        "SELECT outcome_id, label FROM outcome WHERE market_id = ? ORDER BY label",
        (market_id,),
    ).fetchall()
    chosen = st.multiselect(
        "Outcomes",
        [o["label"] for o in outcomes],
        default=[o["label"] for o in outcomes][:2],
    )
    field = st.radio("Series", ["last", "bid", "ask"], horizontal=True)

    frames = []
    for o in outcomes:
        if o["label"] not in chosen:
            continue
        rows = conn.execute(
            f"SELECT ts, {field} AS value FROM quote WHERE outcome_id = ? ORDER BY ts",
            (o["outcome_id"],),
        ).fetchall()
        if rows:
            s = pd.DataFrame(rows, columns=["ts", o["label"]])
            s["ts"] = pd.to_datetime(s["ts"])
            frames.append(s.set_index("ts"))

    if frames:
        st.line_chart(pd.concat(frames, axis=1))
    else:
        st.info("No quotes recorded yet for the selected outcomes.")

# --- Markets table -------------------------------------------------------
st.subheader("Markets")
rows = conn.execute("SELECT * FROM market ORDER BY venue, title").fetchall()
if rows:
    st.dataframe([dict(r) for r in rows], use_container_width=True)
else:
    st.info("No markets ingested yet — Kalshi/Polymarket read paths are phase 3.")
