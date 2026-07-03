"""Streamlit dashboard (design doc §3) — reads SQLite (WAL) directly, no write path.

Surfaces the research output: cross-venue edges per linked event (with the
positive-net ones called out and a net-edge-over-time chart), the Manifold
within-platform paper trades, per-outcome price history, and the markets table.
All views attach to one read-only connection, concurrent with the daemon's writes.

Performance (a Streamlit script reruns TOP TO BOTTOM on every widget click):
- Every query is wrapped in @st.cache_data(ttl=30s) — interactions that reuse
  the same inputs skip SQLite entirely; data refreshes at the poll cadence.
- Chart series are DOWNSAMPLED IN SQL to ~CHART_MAX_POINTS buckets (the bucket's
  last row wins). A chart is ~1000px wide; shipping 37k points per series just
  melts the browser's Vega renderer. Both chart sections have a time-window
  selector (default 7d) so the work stays bounded as the study grows.

Run:  uv run --extra dashboard streamlit run dashboard/app.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from scanner.config import Settings

st.set_page_config(page_title="edge-scanner", layout="wide")

CACHE_TTL_S = 30  # the deployed poll cadence — fresher would just re-read the same data
CHART_MAX_POINTS = 1000  # ~chart pixel width; more points than pixels is pure overhead
WINDOWS: dict[str, float | None] = {"24h": 1.0, "7d": 7.0, "30d": 30.0, "all": None}


@st.cache_resource
def _connect() -> sqlite3.Connection:
    settings = Settings.load()
    # Read-only, WAL: concurrent with the daemon's writes (design doc §2).
    uri = f"file:{settings.scanner.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _since_iso(window_days: float | None) -> str:
    if window_days is None:
        return "1970-01-01T00:00:00+00:00"
    return (datetime.now(tz=timezone.utc) - timedelta(days=window_days)).isoformat()


def _bucket_seconds(window_days: float | None, oldest_iso: str | None) -> int:
    """Bucket width that caps a window at ~CHART_MAX_POINTS, floored at the poll cadence."""
    if window_days is not None:
        span_s = window_days * 86400.0
    elif oldest_iso:
        span_s = (
            datetime.now(tz=timezone.utc) - datetime.fromisoformat(oldest_iso)
        ).total_seconds()
    else:
        span_s = 0.0
    return max(30, int(span_s / CHART_MAX_POINTS))


# --- cached queries (sqlite3.Row isn't picklable -> return dicts/DataFrames) ---


@st.cache_data(ttl=CACHE_TTL_S)
def q_counts() -> dict[str, int]:
    conn = _connect()
    out: dict[str, int] = {}
    for table in ("market", "outcome", "quote", "edge_snapshot"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            out[table] = 0
    return out


@st.cache_data(ttl=CACHE_TTL_S)
def q_summary() -> list[dict]:
    """Latest snapshot per event — same shape as scripts/report.py, so the
    dashboard and the SSH report agree on what's positive right now."""
    return [dict(r) for r in _connect().execute(
        "SELECT e.event_id, e.net_edge, e.gross_edge, e.executable_size, "
        "       e.days_to_resolution, e.basis_risk_flag, e.mirror_net_edge "
        "FROM edge_snapshot e "
        "JOIN (SELECT event_id, MAX(ts) mt FROM edge_snapshot GROUP BY event_id) l "
        "  ON e.event_id = l.event_id AND e.ts = l.mt "
        "ORDER BY e.net_edge DESC"
    ).fetchall()]


@st.cache_data(ttl=CACHE_TTL_S)
def q_latest_edge(event_id: str) -> dict | None:
    row = _connect().execute(
        "SELECT * FROM edge_snapshot WHERE event_id = ? ORDER BY ts DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    return dict(row) if row else None


@st.cache_data(ttl=CACHE_TTL_S)
def q_oldest_ts(table: str, key_col: str, key: str) -> str | None:
    row = _connect().execute(
        f"SELECT MIN(ts) FROM {table} WHERE {key_col} = ?", (key,)
    ).fetchone()
    return row[0] if row else None


@st.cache_data(ttl=CACHE_TTL_S)
def q_edge_chart(event_id: str, since_iso: str, bucket_s: int) -> pd.DataFrame:
    """Downsampled edge series: one row per time bucket (the bucket's LAST
    snapshot — SQLite's bare-column-with-MAX() semantics guarantee that)."""
    rows = _connect().execute(
        "SELECT MAX(ts) AS ts, gross_edge, net_edge, mirror_net_edge "
        "FROM edge_snapshot WHERE event_id = ? AND ts >= ? "
        "GROUP BY CAST(strftime('%s', ts) AS INTEGER) / ? ORDER BY 1",
        (event_id, since_iso, bucket_s),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], format="ISO8601")
    return df


@st.cache_data(ttl=CACHE_TTL_S)
def q_quote_chart(outcome_id: str, field: str, since_iso: str, bucket_s: int) -> pd.DataFrame:
    assert field in ("last", "bid", "ask")  # radio-constrained; belt & suspenders
    rows = _connect().execute(
        f"SELECT MAX(ts) AS ts, {field} AS value "
        "FROM quote WHERE outcome_id = ? AND ts >= ? "
        "GROUP BY CAST(strftime('%s', ts) AS INTEGER) / ? ORDER BY 1",
        (outcome_id, since_iso, bucket_s),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], format="ISO8601")
    return df


@st.cache_data(ttl=CACHE_TTL_S)
def q_market(market_id: str) -> dict | None:
    row = _connect().execute(
        "SELECT title, url FROM market WHERE market_id = ?", (market_id,)
    ).fetchone()
    return dict(row) if row else None


@st.cache_data(ttl=CACHE_TTL_S)
def q_markets() -> list[dict]:
    return [dict(r) for r in _connect().execute(
        "SELECT market_id, venue, title, status, close_time, url "
        "FROM market ORDER BY venue, title"
    ).fetchall()]


@st.cache_data(ttl=CACHE_TTL_S)
def q_outcomes(market_id: str) -> list[dict]:
    return [dict(r) for r in _connect().execute(
        "SELECT outcome_id, label FROM outcome WHERE market_id = ? ORDER BY label",
        (market_id,),
    ).fetchall()]


@st.cache_data(ttl=CACHE_TTL_S)
def q_paper_trades() -> list[dict]:
    try:
        return [dict(r) for r in _connect().execute(
            "SELECT ts, market_id, kind, size, cost, net_profit FROM paper_trade "
            "ORDER BY ts DESC LIMIT 100"
        ).fetchall()]
    except sqlite3.OperationalError:
        return []  # table appears once the daemon (re)opens the db with the current schema


def _window_picker(key: str) -> tuple[str, float | None]:
    label = st.radio("window", list(WINDOWS), index=1, horizontal=True, key=key)
    return label, WINDOWS[label]


st.title("Cross-venue edge scanner")
st.caption(
    "Cross-venue edges (Manifold / Kalshi / Polymarket) + Manifold within-platform arb. "
    "Read-only study harness; zero real-money risk."
)

try:
    counts = q_counts()
except sqlite3.OperationalError:
    st.warning("No database yet. Start the scanner (`uv run edge-scanner`) first.")
    st.stop()

cols = st.columns(4)
for col, table in zip(cols, ("market", "outcome", "quote", "edge_snapshot")):
    col.metric(table, counts[table])

# --- Cross-venue edges (the research output, design doc §6) ---------------
st.subheader("Cross-venue edges")
summary = q_summary()
if not summary:
    st.info(
        "No edges computed yet — needs a curated link with a tradable ask on both legs "
        "(see config/links.yaml)."
    )
else:
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
                # losing direction's net: net-vs-mirror gap = the two-sided spread
                "mirror": (f"{r['mirror_net_edge']:+.4f}"
                           if r["mirror_net_edge"] is not None else "—"),
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
    latest = q_latest_edge(event_id)

    m = st.columns(6)
    m[0].metric("gross edge", f"{latest['gross_edge']:+.4f}")
    # delta carries the sign-coloring (green when positive, red when negative).
    m[1].metric(
        "net edge",
        f"{latest['net_edge']:+.4f}",
        delta=f"{latest['net_edge'] * 100:+.2f}%",
    )
    m[2].metric(
        "mirror net",
        (f"{latest['mirror_net_edge']:+.4f}"
         if latest["mirror_net_edge"] is not None else "—"),
        help="The losing direction's net edge; the gap to net edge is the "
             "two-sided spread. A flip in which direction wins shows as the "
             "chart lines crossing.",
    )
    m[3].metric("modeled fees", f"{latest['modeled_fees']:.4f}")
    m[4].metric("exec size", f"{latest['executable_size']:.0f}")
    m[5].metric(
        "basis risk",
        "⚠ flagged" if latest["basis_risk_flag"] else "clean",
        help="A flagged edge is not a clean arb (design doc §6).",
    )

    def _leg_line(oid: str) -> str:
        # outcome_id is "venue:venue_market_id:LABEL"; market_id drops the label.
        market_id, _, label = oid.rpartition(":")
        venue = market_id.split(":", 1)[0]
        row = q_market(market_id)
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
    win_label, win_days = _window_picker("edge_window")
    bucket = _bucket_seconds(win_days, q_oldest_ts("edge_snapshot", "event_id", event_id))
    edf = q_edge_chart(event_id, _since_iso(win_days), bucket)
    st.caption(
        f"net = gross − fees − lockup · ~{latest['days_to_resolution']:.0f} days to "
        f"resolution · lockup {latest['lockup_cost']:.4f} · mirror = the losing "
        f"direction's net (null before 2026-07-02) · chart downsampled to one point "
        f"per {bucket}s over {win_label}"
    )
    if edf.empty:
        st.info("No edge history in this window.")
    else:
        st.line_chart(edf.set_index("ts")[["gross_edge", "net_edge", "mirror_net_edge"]])

# --- Within-platform arb: Manifold paper trades (design doc §7 phase 2) ----
st.subheader("Within-platform arb — Manifold paper trades")
prows = q_paper_trades()
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

markets = q_markets()
if not markets:
    st.info("No markets ingested yet. Add links to config/links.yaml and run the scanner.")
else:
    labels = {f"[{m['venue']}] {m['title']}": m["market_id"] for m in markets}
    picked = st.selectbox("Market", list(labels))
    market_id = labels[picked]

    outcomes = q_outcomes(market_id)
    chosen = st.multiselect(
        "Outcomes",
        [o["label"] for o in outcomes],
        default=[o["label"] for o in outcomes][:2],
    )
    field = st.radio("Series", ["last", "bid", "ask"], horizontal=True)
    pwin_label, pwin_days = _window_picker("price_window")

    frames = []
    for o in outcomes:
        if o["label"] not in chosen:
            continue
        bucket = _bucket_seconds(pwin_days, q_oldest_ts("quote", "outcome_id", o["outcome_id"]))
        s = q_quote_chart(o["outcome_id"], field, _since_iso(pwin_days), bucket)
        if not s.empty:
            frames.append(s.rename(columns={"value": o["label"]}).set_index("ts"))

    if frames:
        st.caption(f"downsampled to one point per bucket over {pwin_label}")
        st.line_chart(pd.concat(frames, axis=1))
    else:
        st.info("No quotes recorded yet for the selected outcomes.")

# --- Markets table -------------------------------------------------------
st.subheader("Markets")
if markets:
    df = pd.DataFrame(markets)[["venue", "title", "status", "close_time", "url"]]
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
    st.info("No markets ingested yet — add links and run the scanner.")
