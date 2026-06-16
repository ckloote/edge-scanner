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

# --- Cross-venue edges (the research output, design doc §6) ---------------
st.subheader("Cross-venue edges")
events = conn.execute(
    "SELECT DISTINCT event_id FROM edge_snapshot ORDER BY event_id"
).fetchall()
if not events:
    st.info(
        "No edges computed yet — needs a curated link with a tradable ask on both legs "
        "(see config/links.yaml)."
    )
else:
    event_id = st.selectbox("Event", [e["event_id"] for e in events])
    erows = conn.execute(
        "SELECT ts, gross_edge, net_edge, modeled_fees, lockup_cost, executable_size, "
        "days_to_resolution, basis_risk_flag FROM edge_snapshot WHERE event_id = ? "
        "ORDER BY ts",
        (event_id,),
    ).fetchall()
    edf = pd.DataFrame([dict(r) for r in erows])
    edf["ts"] = pd.to_datetime(edf["ts"], format="ISO8601")
    latest = erows[-1]

    m = st.columns(5)
    m[0].metric("gross edge", f"{latest['gross_edge']:+.4f}")
    m[1].metric("net edge", f"{latest['net_edge']:+.4f}")
    m[2].metric("modeled fees", f"{latest['modeled_fees']:.4f}")
    m[3].metric("exec size", f"{latest['executable_size']:.0f}")
    m[4].metric(
        "basis risk",
        "⚠ flagged" if latest["basis_risk_flag"] else "clean",
        help="A flagged edge is not a clean arb (design doc §6).",
    )
    st.caption(
        f"net = gross − fees − lockup · ~{latest['days_to_resolution']:.0f} days to "
        f"resolution · lockup {latest['lockup_cost']:.4f}"
    )
    st.line_chart(edf.set_index("ts")[["gross_edge", "net_edge"]])

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
            # ISO8601: quote timestamps vary in sub-second precision (live points
            # carry microseconds; whole-second ones don't), so don't infer one format.
            s["ts"] = pd.to_datetime(s["ts"], format="ISO8601")
            frames.append(s.set_index("ts"))

    if frames:
        st.line_chart(pd.concat(frames, axis=1))
    else:
        st.info("No quotes recorded yet for the selected outcomes.")

# --- Markets table -------------------------------------------------------
st.subheader("Markets")
rows = conn.execute(
    "SELECT venue, title, status, close_time, url FROM market ORDER BY venue, title"
).fetchall()
if rows:
    df = pd.DataFrame(rows, columns=["venue", "title", "status", "close_time", "url"])
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "url": st.column_config.LinkColumn("page", display_text="open ↗"),
            "close_time": st.column_config.DatetimeColumn("close_time"),
        },
    )
else:
    st.info("No markets ingested yet — Kalshi/Polymarket read paths are phase 3.")
