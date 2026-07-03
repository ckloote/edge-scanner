"""Quick read-only status report for the running scanner (SSH-friendly).

    uv run python scripts/report.py

Reads the SQLite DB read-only (safe to run while the daemon is writing) and prints
row counts, the edge-history time span, the latest edge per linked event sorted by net
edge, and any Manifold paper trades.
"""

from __future__ import annotations

import sqlite3

from scanner.config import Settings


def main() -> None:
    db = Settings.load().scanner.db_path
    if not db.exists():
        print(f"no database at {db} — has the scanner run yet?")
        return
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    def count(table: str) -> int:
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    print(f"db: {db}")
    for table in ("market", "outcome", "quote", "edge_snapshot", "paper_trade"):
        print(f"  {table}: {count(table)}")
    span = c.execute("SELECT MIN(ts), MAX(ts) FROM edge_snapshot").fetchone()
    if span and span[0]:
        print(f"  edge history: {span[0][:19]}  ->  {span[1][:19]}")

    rows = c.execute(
        """
        SELECT e.event_id, e.net_edge, e.gross_edge, e.executable_size sz,
               e.days_to_resolution days, e.basis_risk_flag b, e.mirror_net_edge m
        FROM edge_snapshot e
        JOIN (SELECT event_id, MAX(ts) mt FROM edge_snapshot GROUP BY event_id) l
          ON e.event_id = l.event_id AND e.ts = l.mt
        ORDER BY e.net_edge DESC
        """
    ).fetchall()
    if rows:
        print("\nlatest edge per event (sorted by net; mirror = losing direction's net):")
        for r in rows:
            mark = " *" if r["net_edge"] > 0 else "  "
            mirror = f"{r['m']:+.4f}" if r["m"] is not None else "      —"
            print(
                f" {mark} {r['event_id']:18} net={r['net_edge']:+.4f} "
                f"mirror={mirror} gross={r['gross_edge']:+.4f} exec={r['sz']:>9.0f} "
                f"days={r['days']:>3.0f} basis={r['b']}"
            )
        print(f"\npositive-net events right now: {sum(1 for r in rows if r['net_edge'] > 0)}")

    if count("paper_trade"):
        total = c.execute("SELECT SUM(net_profit) FROM paper_trade").fetchone()[0]
        print(f"manifold paper trades: {count('paper_trade')} (cumulative net {total:.2f})")
    c.close()


if __name__ == "__main__":
    main()
