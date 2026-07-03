"""Phase-4 calibration report: frequency + duration of positive-net edge windows.

    uv run python scripts/analyze.py                  # windows where net > 0
    uv run python scripts/analyze.py --min-net 0.005  # only edges beyond 0.5%
    uv run python scripts/analyze.py --gap 120        # relax the outage rule

Reads the SQLite DB read-only (safe while the daemon writes) and answers the
design doc §1 question over `edge_snapshot` history: how often a genuine
(basis-clean), after-fee (net > threshold), executable (top-of-book depth on
both legs) edge appeared, and how long each window stayed open. Window
semantics live in scanner/analysis.py (unit-tested); this script is I/O and
formatting only.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from datetime import datetime
from itertools import groupby

from scanner.analysis import DEFAULT_GAP_S, EventStats, Snap, extract_windows
from scanner.analysis import fmt_duration as fmt_dur
from scanner.config import Settings

# Sensitivity thresholds: "an edge exists" depends on where you draw net > x.
SENSITIVITY = (0.0, 0.0025, 0.005, 0.01)


def load_event_snaps(conn: sqlite3.Connection):
    """Yield (event_id, [Snap...]) per event, time-ordered, one pass over the table."""
    cur = conn.execute(
        "SELECT event_id, ts, net_edge, executable_size, days_to_resolution,"
        " basis_risk_flag FROM edge_snapshot ORDER BY event_id, ts"
    )
    for event_id, rows in groupby(cur, key=lambda r: r["event_id"]):
        yield event_id, [
            Snap(
                ts=datetime.fromisoformat(r["ts"]),
                net=r["net_edge"],
                exec_size=r["executable_size"] or 0.0,
                days=r["days_to_resolution"] or 0.0,
                basis=r["basis_risk_flag"],
            )
            for r in rows
        ]


def _dur_stats(durs: list[float]) -> str:
    if not durs:
        return "     —        —        —"
    med = statistics.median(durs)
    p90 = statistics.quantiles(durs, n=10)[-1] if len(durs) >= 2 else durs[0]
    return f"{fmt_dur(med):>7}  {fmt_dur(p90):>7}  {fmt_dur(max(durs)):>7}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--min-net", type=float, default=0.0,
                    help="primary threshold: a window needs net_edge > this (default 0)")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP_S,
                    help=f"coverage break (s) that closes a window (default {DEFAULT_GAP_S:.0f})")
    ap.add_argument("--min-exec", type=float, default=0.0,
                    help="'executable' needs min depth >= this many contracts throughout "
                         "the window (default: any nonzero depth)")
    args = ap.parse_args()

    def is_exec(w) -> bool:
        return w.min_exec >= args.min_exec if args.min_exec > 0 else w.executable

    db = Settings.load().scanner.db_path
    if not db.exists():
        print(f"no database at {db} — has the scanner run yet?")
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    thresholds = sorted({args.min_net, *SENSITIVITY})
    # One streaming pass over the table: per event, run extraction at every
    # threshold, keep only the (small) stats — never all snapshots at once.
    by_thr: dict[float, list[EventStats]] = {t: [] for t in thresholds}
    n_snaps = 0
    span_lo: datetime | None = None
    span_hi: datetime | None = None
    for event_id, snaps in load_event_snaps(conn):
        n_snaps += len(snaps)
        span_lo = min(span_lo, snaps[0].ts) if span_lo else snaps[0].ts
        span_hi = max(span_hi, snaps[-1].ts) if span_hi else snaps[-1].ts
        for t in thresholds:
            by_thr[t].append(extract_windows(event_id, snaps, threshold=t, gap_s=args.gap))

    if n_snaps == 0:
        print("edge_snapshot is empty — nothing to analyze yet.")
        return
    span_days = (span_hi - span_lo).total_seconds() / 86400.0
    primary = by_thr[args.min_net]

    print(f"db: {db}")
    print(f"span: {span_lo:%Y-%m-%d %H:%M} -> {span_hi:%Y-%m-%d %H:%M} UTC "
          f"({span_days:.1f} days) | {len(primary)} events | {n_snaps:,} snapshots")
    print(f"window rule: net_edge > {args.min_net:.4f}; a coverage gap > {args.gap:.0f}s "
          f"closes a window; 'blip' = single snapshot (<= one poll interval)\n")

    # --- per event at the primary threshold --------------------------------
    print(f"per event (net > {args.min_net:.4f}):")
    print(f"  {'event':<18} {'%time':>6} {'sust':>5} {'blips':>5} {'longest':>8} "
          f"{'peak net':>9}  {'clean+exec sust':>15}")
    for st in sorted(primary, key=lambda s: -s.positive_s):
        sust = [w for w in st.windows if w.sustained]
        blips = len(st.windows) - len(sust)
        longest = max((w.duration_s for w in sust), default=0.0)
        peak = max((w.peak_net for w in st.windows), default=0.0)
        ce = sum(1 for w in sust if w.clean and is_exec(w))
        print(f"  {st.event_id:<18} {st.pct_positive:>5.1f}% {len(sust):>5} {blips:>5} "
              f"{fmt_dur(longest) if sust else '—':>8} "
              f"{('%+.4f' % peak) if st.windows else '—':>9}  {ce:>15}")

    # --- sustained windows, largest first -----------------------------------
    all_windows = [w for st in primary for w in st.windows if w.sustained]
    all_windows.sort(key=lambda w: -w.duration_s)
    if all_windows:
        print(f"\nsustained windows (net > {args.min_net:.4f}), top {min(15, len(all_windows))} "
              f"of {len(all_windows)} by duration:")
        print(f"  {'event':<18} {'start (UTC)':<16} {'dur':>7} {'peak net':>9} "
              f"{'min exec':>9} {'days':>5}  notes")
        for w in all_windows[:15]:
            notes = []
            if not w.clean:
                notes.append("basis⚠")
            if not is_exec(w):
                notes.append("thin" if w.executable else "no-depth")
            if w.open_at_data_end:
                notes.append("still open")
            print(f"  {w.event_id:<18} {w.start:%m-%d %H:%M} {fmt_dur(w.duration_s):>12} "
                  f"{w.peak_net:>+9.4f} {w.min_exec:>9.1f} {w.days_at_start:>5.0f}  "
                  f"{', '.join(notes) or 'clean+exec'}")

    # --- the §1 answer: threshold sensitivity -------------------------------
    exec_rule = (f"min depth >= {args.min_exec:.0f} throughout" if args.min_exec > 0
                 else "nonzero depth on both legs")
    print(f"\nthe §1 answer — genuine (basis-clean) + executable ({exec_rule}) "
          "sustained windows:")
    print(f"  {'threshold':<12} {'%time':>6} {'sust':>5} {'blips':>5} {'clean+exec':>10} "
          f"{'med':>7} {'p90':>7} {'max':>7}  {'per event-day':>13}  events")
    for t in thresholds:
        stats = by_thr[t]
        obs = sum(s.observed_s for s in stats)
        pos = sum(s.positive_s for s in stats)
        wins = [w for s in stats for w in s.windows]
        sust = [w for w in wins if w.sustained]
        ce = [w for w in sust if w.clean and is_exec(w)]
        freq = len(ce) / (obs / 86400.0) if obs else 0.0
        evs = sorted({w.event_id for w in ce})
        ev_str = ", ".join(evs[:6]) + (f" +{len(evs) - 6} more" if len(evs) > 6 else "")
        mark = " <- --min-net" if t == args.min_net and t not in SENSITIVITY else ""
        print(f"  net > {t:.4f} {100.0 * pos / obs if obs else 0.0:>5.1f}% {len(sust):>5} "
              f"{len(wins) - len(sust):>5} {len(ce):>10} {_dur_stats([w.duration_s for w in ce])}"
              f"  {freq:>13.2f}{mark}  {ev_str or '—'}")
    print("\nduration stats are over clean+exec sustained windows; 'per event-day' is that"
          "\ncount / total observed event-time. Blips last <= one poll interval and are"
          "\ncounted but excluded from durations. Windows open at data end are included.")
    conn.close()


if __name__ == "__main__":
    main()
