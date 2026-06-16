"""Within-platform arb detector tests (design doc §7 phase 2)."""

import pytest

from scanner.arb import ArbLeg, detect

ZERO_FEE = lambda price, size: 0.0  # noqa: E731 - Manifold (play money)


def _leg(label, ask, size=100.0):
    return ArbLeg(outcome_id=f"manifold:m:{label}", label=label, ask=ask, ask_size=size)


def test_binary_arb_detected():
    arb = detect("manifold:m", "binary", [_leg("YES", 0.45), _leg("NO", 0.50)], ZERO_FEE)
    assert arb is not None
    assert arb.cost == pytest.approx(0.95)
    assert arb.gross_edge == pytest.approx(0.05)
    assert arb.net_edge == pytest.approx(0.05)  # zero fees on Manifold
    assert arb.executable_size == pytest.approx(100.0)


def test_binary_no_arb_when_set_costs_a_dollar_or_more():
    assert detect("m", "binary", [_leg("YES", 0.55), _leg("NO", 0.50)], ZERO_FEE) is None
    assert detect("m", "binary", [_leg("YES", 0.50), _leg("NO", 0.50)], ZERO_FEE) is None  # ==1


def test_multi_arb_buy_all_under_one():
    legs = [_leg("A", 0.30), _leg("B", 0.30), _leg("C", 0.30)]
    arb = detect("m", "multi", legs, ZERO_FEE)
    assert arb is not None and arb.gross_edge == pytest.approx(0.10)


def test_multi_no_arb_when_sum_exceeds_one():
    legs = [_leg("A", 0.40), _leg("B", 0.40), _leg("C", 0.40)]
    assert detect("m", "multi", legs, ZERO_FEE) is None


def test_fees_reduce_net_edge():
    arb = detect("m", "binary", [_leg("YES", 0.45), _leg("NO", 0.50)],
                 lambda price, size: 0.01)
    assert arb.modeled_fees == pytest.approx(0.02)  # 0.01 per leg
    assert arb.net_edge == pytest.approx(0.03)      # gross 0.05 - 0.02


def test_executable_size_is_min_depth_zero_when_unknown():
    a = detect("m", "binary", [_leg("YES", 0.45, 80), _leg("NO", 0.50, 120)], ZERO_FEE)
    assert a.executable_size == pytest.approx(80.0)
    b = detect("m", "binary", [_leg("YES", 0.45, None), _leg("NO", 0.50, None)], ZERO_FEE)
    assert b.executable_size == 0.0


def test_degenerate_inputs():
    assert detect("m", "binary", [_leg("YES", 0.4)], ZERO_FEE) is None  # <2 legs
    assert detect("m", "binary", [ArbLeg("x", "YES", None), _leg("NO", 0.5)], ZERO_FEE) is None
