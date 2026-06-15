"""Edge math unit tests (design doc §6) — the thing most worth testing."""

from datetime import datetime, timedelta, timezone

import pytest

from scanner.edge import (
    EdgeInputs,
    capital,
    compute_edge,
    days_between,
    gross_edge,
    lockup_cost,
)


def test_gross_edge_positive_when_asks_sum_below_one():
    assert gross_edge(0.45, 0.50) == pytest.approx(0.05)
    assert gross_edge(0.55, 0.50) == pytest.approx(-0.05)  # no arb
    assert capital(0.45, 0.50) == pytest.approx(0.95)


def test_lockup_cost_annualizes_over_capital():
    # 5% rf, 6 months, $0.95 capital -> 0.05 * 0.5 * 0.95
    assert lockup_cost(0.05, 182.5, 0.95) == pytest.approx(0.05 * (182.5 / 365) * 0.95)


def test_lockup_cost_zero_when_no_time_left():
    assert lockup_cost(0.05, 0.0, 0.95) == 0.0


def test_days_between_handles_naive_and_none():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    assert days_between(now, now + timedelta(days=30)) == pytest.approx(30.0)
    assert days_between(now, None) == 0.0  # unknown resolution -> no lockup
    # past resolution clamps to 0 (never negative lockup)
    assert days_between(now, now - timedelta(days=5)) == 0.0


def test_compute_edge_full():
    inp = EdgeInputs(
        ask_a=0.45,
        ask_b=0.50,
        ask_size_a=100,
        ask_size_b=80,
        fee_a=0.0175,  # e.g. Kalshi at p=0.5, size 1
        fee_b=0.0125,  # e.g. Polymarket us at p=0.5, size 1
        days_to_resolution=30.0,
        basis_risk_flag=0,
    )
    r = compute_edge(inp, risk_free_rate=0.05)

    assert r.gross_edge == pytest.approx(0.05)
    assert r.modeled_fees == pytest.approx(0.03)  # 0.0175 + 0.0125
    assert r.lockup_cost == pytest.approx(0.05 * (30 / 365) * 0.95)
    assert r.net_edge == pytest.approx(0.05 - 0.03 - 0.05 * (30 / 365) * 0.95)
    assert r.executable_size == 80  # min(depth on each leg)
    assert r.basis_risk_flag == 0


def test_long_dated_edge_is_eaten_by_lockup():
    """A 2% gross spread resolving in 6 months should net out poorly (design doc §6.1)."""
    inp = EdgeInputs(
        ask_a=0.49, ask_b=0.49, ask_size_a=100, ask_size_b=100,
        fee_a=0.0, fee_b=0.0, days_to_resolution=182.5, basis_risk_flag=0,
    )
    r = compute_edge(inp, risk_free_rate=0.05)
    assert r.gross_edge == pytest.approx(0.02)
    # lockup ~ 0.05 * 0.5 * 0.98 = 0.0245 > gross 0.02 -> net negative
    assert r.net_edge < 0
