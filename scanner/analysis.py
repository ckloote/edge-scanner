"""Phase-4 analysis: edge-window extraction over edge_snapshot history (design doc §1).

Answers the study question — how often does a genuine, after-fee, executable,
near-dated edge appear, and how long does each window stay open — from the
`edge_snapshot` time series. Pure functions over time-ordered snapshots, unit-
tested like edge.py; scripts/analyze.py is the CLI that feeds them from SQLite.

A WINDOW is a maximal run of consecutive snapshots with net_edge > threshold.
Coverage gaps longer than `gap_s` (daemon down / venue outage) close any open
window and never bridge one: an edge is only known to persist while we were
actually looking. A single-snapshot window (a "blip") lasted at most one poll
interval; duration stats are reported over sustained (>= 2 snapshot) windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# A coverage break longer than this closes any open window (3x the deployed 30s
# poll interval — tolerates one stretched cycle, not a real outage).
DEFAULT_GAP_S = 90.0


@dataclass(slots=True)
class Snap:
    """One edge_snapshot row, reduced to what window extraction needs."""

    ts: datetime
    net: float
    exec_size: float
    days: float  # days_to_resolution at this snapshot
    basis: int


@dataclass(slots=True)
class Window:
    """A maximal run of snapshots with net edge above the threshold."""

    event_id: str
    start: datetime
    end: datetime  # ts of the last snapshot still above threshold
    snapshots: int
    peak_net: float
    min_exec: float  # depth low-water mark while open; 0 => not fully executable
    days_at_start: float
    basis: int  # 1 if ANY snapshot in the window carried the basis-risk flag
    open_at_data_end: bool = False  # still above threshold at the last snapshot

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def sustained(self) -> bool:
        return self.snapshots >= 2

    @property
    def executable(self) -> bool:
        return self.min_exec > 0

    @property
    def clean(self) -> bool:
        return self.basis == 0


@dataclass(slots=True)
class EventStats:
    """Window extraction result for one event at one threshold."""

    event_id: str
    snapshots: int = 0
    observed_s: float = 0.0  # summed inter-snapshot time, gaps excluded
    positive_s: float = 0.0  # portion of observed_s spent above the threshold
    windows: list[Window] = field(default_factory=list)

    @property
    def pct_positive(self) -> float:
        return 100.0 * self.positive_s / self.observed_s if self.observed_s else 0.0


def extract_windows(
    event_id: str,
    snaps: list[Snap],
    *,
    threshold: float = 0.0,
    gap_s: float = DEFAULT_GAP_S,
) -> EventStats:
    """Extract positive-net windows from one event's time-ordered snapshots.

    `positive_s` credits each inter-snapshot delta to the LEFT endpoint's state
    (the edge observed at t is what held until the next look), skipping deltas
    beyond `gap_s` entirely — so %-time-positive is over observed time only.
    """
    stats = EventStats(event_id=event_id, snapshots=len(snaps))
    cur: Window | None = None
    prev: Snap | None = None
    for s in snaps:
        if prev is not None:
            delta = (s.ts - prev.ts).total_seconds()
            if delta <= gap_s:
                stats.observed_s += delta
                if prev.net > threshold:
                    stats.positive_s += delta
            elif cur is not None:
                # Coverage break: close at the last snapshot we actually saw.
                stats.windows.append(cur)
                cur = None
        if s.net > threshold:
            if cur is None:
                cur = Window(
                    event_id=event_id,
                    start=s.ts,
                    end=s.ts,
                    snapshots=1,
                    peak_net=s.net,
                    min_exec=s.exec_size,
                    days_at_start=s.days,
                    basis=s.basis,
                )
            else:
                cur.end = s.ts
                cur.snapshots += 1
                cur.peak_net = max(cur.peak_net, s.net)
                cur.min_exec = min(cur.min_exec, s.exec_size)
                cur.basis = max(cur.basis, s.basis)
        elif cur is not None:
            stats.windows.append(cur)
            cur = None
        prev = s
    if cur is not None:
        cur.open_at_data_end = True
        stats.windows.append(cur)
    return stats
