"""Streamlit dashboard (design doc §3) — reads SQLite (WAL) directly, no write path.

Surfaces the research output: cross-venue edges per linked event (with the
positive-net ones called out and a net-edge-over-time chart), the Manifold
within-platform paper trades, per-outcome price history, and the markets table.
All views attach to one read-only connection, concurrent with the daemon's writes.

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
st.caption(
    "Cross-venue edges (Manifold / Kalshi / Polymarket) + Manifold within-platform arb. "
    "Read-only study harness; zero real-money risk."
)

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
    # --- Overview: latest snapshot per event, positives called out ---------
    # Same "latest row per event" shape as scripts/report.py, so the dashboard
    # and the SSH report agree on what's positive right now.
    summary = conn.execute(
        "SELECT e.event_id, e.net_edge, e.gross_edge, e.executable_size, "
        "       e.days_to_resolution, e.basis_risk_flag "
        "FROM edge_snapshot e "
        "JOIN (SELECT event_id, MAX(ts) mt FROM edge_snapshot GROUP BY event_id) l "
        "  ON e.event_id = l.event_id AND e.ts = l.mt "
        "ORDER BY e.net_edge DESC"
    ).fetchall()
    positives = [r for r in summary if r["net_edge"] > 0]
    if positives:
        names = ", ".join(r["event_id"] for r in positives)
        st.success(f"**{len(positives)} positive-net event(s) right now:** {names}")
        flagged = [r["event_id"] for r in positives if r["basis_risk_flag"]]
        if flagged:
            st.caption(
                "⚠ " + ", ".join(flagged) + " carry a basis-risk flag — not a clean arb."
            )
    else:
        st.caption("No positive-net edges right now.")

    sdf = pd.DataFrame(
        [
            {
                "": "🟢" if r["net_edge"] > 0 else "",
                "event": r["event_id"],
                "net": f"{r['net_edge']:+.4f}",
                "gross": f"{r['gross_edge']:+.4f}",
                "exec": f"{r['executable_size']:.0f}",
                "days": f"{r['days_to_resolution']:.0f}",
                "basis": "⚠" if r["basis_risk_flag"] else "—",
            }
            for r in summary
        ]
    )
    st.dataframe(sdf, width="stretch", hide_index=True)

    # --- Per-event detail --------------------------------------------------
    event_id = st.selectbox("Event", [r["event_id"] for r in summary])
    erows = conn.execute(
        "SELECT ts, gross_edge, net_edge, modeled_fees, lockup_cost, executable_size, "
        "days_to_resolution, basis_risk_flag, leg_a_outcome_id, leg_b_outcome_id "
        "FROM edge_snapshot WHERE event_id = ? ORDER BY ts",
        (event_id,),
    ).fetchall()
    edf = pd.DataFrame([dict(r) for r in erows])
    edf["ts"] = pd.to_datetime(edf["ts"], format="ISO8601")
    latest = erows[-1]

    m = st.columns(5)
    m[0].metric("gross edge", f"{latest['gross_edge']:+.4f}")
    # delta carries the sign-coloring (green when positive, red when negative).
    m[1].metric(
        "net edge",
        f"{latest['net_edge']:+.4f}",
        delta=f"{latest['net_edge'] * 100:+.2f}%",
    )
    m[2].metric("modeled fees", f"{latest['modeled_fees']:.4f}")
    m[3].metric("exec size", f"{latest['executable_size']:.0f}")
    m[4].metric(
        "basis risk",
        "⚠ flagged" if latest["basis_risk_flag"] else "clean",
        help="A flagged edge is not a clean arb (design doc §6).",
    )
    def _leg_line(oid: str) -> str:
        # outcome_id is "venue:venue_market_id:LABEL"; market_id drops the label.
        market_id, _, label = oid.rpartition(":")
        venue = market_id.split(":", 1)[0]
        row = conn.execute(
            "SELECT title, url FROM market WHERE market_id = ?", (market_id,)
        ).fetchone()
        title = row["title"] if row else market_id
        link = f" [↗]({row['url']})" if row and row["url"] else ""
        return f"- **{venue}** — buy **{label}** — {title}{link}"

    # The two markets being compared (best direction's legs), with click-through —
    # so you don't have to hunt for them in the Markets table below.
    st.markdown(
        "Markets compared:\n\n"
        + _leg_line(latest["leg_a_outcome_id"])
        + "\n"
        + _leg_line(latest["leg_b_outcome_id"])
    )
    st.caption(
        f"net = gross − fees − lockup · ~{latest['days_to_resolution']:.0f} days to "
        f"resolution · lockup {latest['lockup_cost']:.4f}"
    )
    st.line_chart(edf.set_index("ts")[["gross_edge", "net_edge"]])

# --- Within-platform arb: Manifold paper trades (design doc §7 phase 2) ----
st.subheader("Within-platform arb — Manifold paper trades")
try:
    prows = conn.execute(
        "SELECT ts, market_id, kind, size, cost, net_profit, legs FROM paper_trade "
        "ORDER BY ts DESC LIMIT 100"
    ).fetchall()
except sqlite3.OperationalError:
    prows = []  # table appears once the daemon (re)opens the db with the current schema
if prows:
    c1, c2 = st.columns(2)
    c1.metric("paper trades", len(prows), help="fake-money within-platform arb fills")
    c2.metric("cumulative net profit", f"{sum(r['net_profit'] for r in prows):.2f}")
    st.dataframe(
        [{"ts": r["ts"], "market": r["market_id"], "kind": r["kind"], "size": r["size"],
          "cost": round(r["cost"], 4), "net_profit": round(r["net_profit"], 4)}
         for r in prows],
        width="stretch", hide_index=True,
    )
else:
    st.info(
        "No within-platform arbs captured yet — Manifold is efficient (crossing limit "
        "orders get matched away), so the harness fires only when a complete set is "
        "briefly buyable under $1."
    )

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
