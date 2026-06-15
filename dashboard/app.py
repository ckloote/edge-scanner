"""Streamlit dashboard (design doc §3) — reads SQLite (WAL) directly, no write path.

Phase 0: boots and shows the canonical tables + counts so you can confirm the
schema and the WAL read path work. The price-history chart (phase 1) and the
edge-over-time view (phase 4) attach to the same read-only connection.

Run:  uv run --extra dashboard streamlit run dashboard/app.py
"""

from __future__ import annotations

import sqlite3

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
st.caption("Phase 0 — read-only study harness. Zero real-money risk.")

try:
    conn = _connect()
except sqlite3.OperationalError:
    st.warning("No database yet. Start the scanner (`uv run edge-scanner`) first.")
    st.stop()

cols = st.columns(4)
for col, table in zip(cols, ("market", "outcome", "quote", "edge_snapshot")):
    col.metric(table, _count(conn, table))

st.subheader("Markets")
rows = conn.execute("SELECT * FROM market ORDER BY venue, title").fetchall()
if rows:
    st.dataframe([dict(r) for r in rows], use_container_width=True)
else:
    st.info("No markets ingested yet — connector read paths are phase 1/3.")
