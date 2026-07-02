"""Window extraction over edge_snapshot history (phase 4 analysis)."""

from datetime import datetime, timedelta, timezone

import pytest

from scanner.analysis import Snap, extract_windows

T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _snaps(nets, *, step_s=30.0, exec_size=100.0, days=20.0, basis=0, gaps=None):
    """Build a snapshot series from net values; `gaps` maps index -> extra seconds
    inserted BEFORE that snapshot."""
    out, t = [], T0
    for i, net in enumerate(nets):
        if i:
            t += timedelta(seconds=step_s + (gaps or {}).get(i, 0.0))
        out.append(Snap(ts=t, net=net, exec_size=exec_size, days=days, basis=basis))
    return out


def test_single_window_with_peak_and_duration():
    st = extract_windows("e", _snaps([-0.01, 0.01, 0.02, 0.015, -0.01]))
    assert len(st.windows) == 1
    w = st.windows[0]
    assert (w.snapshots, w.duration_s, w.peak_net) == (3, 60.0, 0.02)
    assert w.sustained and not w.open_at_data_end
    assert st.positive_s == pytest.approx(90.0)  # left-endpoint credit: 3 deltas
    assert st.observed_s == pytest.approx(120.0)


def test_blip_is_a_single_snapshot_window():
    st = extract_windows("e", _snaps([-0.01, 0.01, -0.01]))
    assert len(st.windows) == 1
    w = st.windows[0]
    assert w.snapshots == 1 and w.duration_s == 0.0 and not w.sustained


def test_gap_closes_window_and_never_bridges():
    # Positive on both sides of a 300s outage -> two windows, gap not observed.
    st = extract_windows("e", _snaps([0.01, 0.01, 0.01, 0.01], gaps={2: 300.0}))
    assert len(st.windows) == 2
    assert all(w.snapshots == 2 for w in st.windows)
    assert st.observed_s == pytest.approx(60.0)  # 2 x 30s; the 330s delta skipped
    assert st.positive_s == pytest.approx(60.0)


def test_threshold_filters_marginal_edges():
    st = extract_windows("e", _snaps([0.004, 0.004]), threshold=0.005)
    assert st.windows == []
    assert st.positive_s == 0.0


def test_open_at_data_end_flagged():
    st = extract_windows("e", _snaps([-0.01, 0.01, 0.01]))
    assert len(st.windows) == 1 and st.windows[0].open_at_data_end


def test_min_exec_and_basis_propagate():
    snaps = _snaps([0.01, 0.01, 0.01])
    snaps[1].exec_size = 0.0  # depth vanished mid-window
    snaps[2].basis = 1  # flag appeared mid-window
    (w,) = extract_windows("e", snaps).windows
    assert not w.executable and w.min_exec == 0.0
    assert w.basis == 1 and not w.clean
