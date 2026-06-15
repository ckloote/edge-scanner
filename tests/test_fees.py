"""Per-venue fees() unit tests (design doc §10 prime directive).

Fee formulas were derived from the live API docs on 2026-06-15; the constants
asserted here are the ones documented in docs/api-findings.md. If a venue changes
its schedule, THESE tests are the canary.

All fees are in dollars. `size` is contracts/shares (C). The arb model buys, so
`side="taker"` is the path the edge engine exercises.
"""

import pytest

from scanner.connectors import KalshiConnector, ManifoldConnector, PolymarketConnector
from scanner.connectors.base import Connector, ceil_to


# --- helper ---------------------------------------------------------------

def test_ceil_to_rounds_up_and_leaves_exact_multiples():
    assert ceil_to(0.01547, 0.0001) == pytest.approx(0.0155)
    assert ceil_to(0.0175, 0.0001) == pytest.approx(0.0175)  # exact multiple, no tick up
    assert ceil_to(1.75, 0.0001) == pytest.approx(1.75)
    assert ceil_to(0.0, 0.0001) == pytest.approx(0.0)


def test_connectors_satisfy_protocol():
    for c in (ManifoldConnector(), KalshiConnector(), PolymarketConnector()):
        assert isinstance(c, Connector)


# --- Manifold: play money, modeled as zero --------------------------------

class TestManifoldFees:
    def test_zero_by_default(self):
        c = ManifoldConnector()
        assert c.fees(0.50, 100, "taker") == 0.0
        assert c.fees(0.01, 1, "taker") == 0.0
        assert c.fees(0.99, 1_000_000, "maker") == 0.0

    def test_seam_mirrors_pq_shape_if_rate_configured(self):
        # The seam exists so the harness mirrors the real-venue interface exactly.
        c = ManifoldConnector(fee_rate=0.01)
        assert c.fees(0.50, 100, "taker") == pytest.approx(0.01 * 100 * 0.25)


# --- Kalshi: ceil_to_centicent( multiplier * C * p * (1-p) ) ---------------

class TestKalshiFees:
    def test_max_per_contract_at_midpoint(self):
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> the per-contract maximum.
        assert KalshiConnector().fees(0.50, 1, "taker") == pytest.approx(0.0175)

    def test_scales_linearly_with_contracts(self):
        assert KalshiConnector().fees(0.50, 100, "taker") == pytest.approx(1.75)

    def test_maker_is_25pct_of_taker(self):
        c = KalshiConnector()
        assert c.fees(0.50, 100, "maker") == pytest.approx(c.fees(0.50, 100, "taker") * 0.25)

    def test_rounds_up_to_centicent(self):
        # 0.07 * 0.33 * 0.67 = 0.0154770 -> ceil to 0.0001 = 0.0155
        assert KalshiConnector().fees(0.33, 1, "taker") == pytest.approx(0.0155)

    def test_symmetric_in_price(self):
        c = KalshiConnector()
        assert c.fees(0.30, 100, "taker") == pytest.approx(c.fees(0.70, 100, "taker"))

    def test_small_at_extremes(self):
        # 0.07 * 0.99 * 0.01 = 0.000693 -> ceil to 0.0007
        assert KalshiConnector().fees(0.99, 1, "taker") == pytest.approx(0.0007)

    def test_per_series_multiplier_override(self):
        c = KalshiConnector(series_multipliers={"KXBTC": 0.10})
        assert c.fees(0.50, 100, "taker", series="KXBTC") == pytest.approx(2.5)
        # Unknown series falls back to the general 0.07 multiplier.
        assert c.fees(0.50, 100, "taker", series="KXOTHER") == pytest.approx(1.75)


# --- Polymarket: feeRate * C * p * (1-p) -----------------------------------

INTL_RATES = {
    "crypto": 0.07,
    "sports": 0.03,
    "finance": 0.04,
    "economics": 0.05,
    "geopolitics": 0.0,
}


class TestPolymarketIntlFees:
    def test_default_rate_when_no_category(self):
        # intl fallback == taker_rate 0.05; 0.05 * 100 * 0.25 = 1.25
        c = PolymarketConnector(category_rates=INTL_RATES)
        assert c.fees(0.50, 100, "taker") == pytest.approx(1.25)

    def test_per_category_rates(self):
        c = PolymarketConnector(category_rates=INTL_RATES)
        assert c.fees(0.50, 100, "taker", category="crypto") == pytest.approx(1.75)
        assert c.fees(0.50, 100, "taker", category="sports") == pytest.approx(0.75)
        assert c.fees(0.50, 100, "taker", category="geopolitics") == 0.0  # fee-free

    def test_sells_are_exempt(self):
        c = PolymarketConnector(category_rates=INTL_RATES)
        assert c.fees(0.50, 100, "sell", category="crypto") == 0.0

    def test_intl_makers_pay_zero(self):
        c = PolymarketConnector(category_rates=INTL_RATES)
        assert c.fees(0.50, 100, "maker", category="crypto") == 0.0

    def test_symmetric_in_price(self):
        c = PolymarketConnector(category_rates=INTL_RATES)
        assert c.fees(0.30, 100, "taker", category="crypto") == pytest.approx(
            c.fees(0.70, 100, "taker", category="crypto")
        )


class TestPolymarketUsFees:
    def test_uniform_taker_self_caps_at_midpoint(self):
        # us uniform 0.05; parabola self-caps at p=0.5 == $1.25 / 100 contracts.
        c = PolymarketConnector(venue_mode="us", taker_rate=0.05)
        assert c.fees(0.50, 100, "taker") == pytest.approx(1.25)
        # us ignores category (uniform schedule):
        assert c.fees(0.50, 100, "taker", category="crypto") == pytest.approx(1.25)

    def test_maker_rebate_is_negative(self):
        c = PolymarketConnector(venue_mode="us", taker_rate=0.05, maker_rebate=0.0125)
        assert c.fees(0.50, 100, "maker") == pytest.approx(-0.3125)

    def test_sells_exempt(self):
        c = PolymarketConnector(venue_mode="us")
        assert c.fees(0.50, 100, "sell") == 0.0
