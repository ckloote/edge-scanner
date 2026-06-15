"""Kalshi connector (design doc §5).

DERIVED FROM LIVE DOCS (docs.kalshi.com, kalshi.com/fee-schedule, fetched 2026-06-15):
- Base URL: https://external-api.kalshi.com/trade-api/v2
- Auth: market-data endpoints are PUBLIC. The v1 read-only path needs NO auth.
  (Trading, deferred, uses per-request RSA-PSS signing via KALSHI-ACCESS-KEY/
  -SIGNATURE/-TIMESTAMP headers — NOT the old email/password session token the
  design doc assumed. See docs/api-findings.md.)
- Discovery: GET /markets (filters series_ticker, event_ticker, status),
  GET /markets/{ticker}, GET /markets/{ticker}/orderbook
- Prices: DOLLAR STRINGS, e.g. yes_bid_dollars="0.5600" (NOT integer cents — the
  design doc is stale here). Parse to float directly; already in [0,1].
- Orderbook: BIDS ONLY (yes + no). YES ask = 1 - best NO bid; YES-ask depth = NO-bid
  depth. Asks are derived, never quoted directly.
- status enum: initialized|inactive|active|closed|determined|disputed|amended|finalized.

FEE MODEL (kalshi.com/fee-schedule, docs.kalshi.com/getting_started/fee_rounding):
    fee = ceil_to_centicent( multiplier * C * p * (1 - p) )
- multiplier = 0.07 general; the current schedule appears uniform (no category
  premium found in live help docs), but the market object exposes NO category/
  multiplier field, so any override must be configured per series ticker.
- maker fee = 25% of taker.
- Rounding: UP to the centicent ($0.0001), per-fill with an accumulator that
  converges to a single-fill cost (NOT "round up to the whole cent on the
  aggregate order" — the design doc is stale here). No settlement fee.
"""

from __future__ import annotations

from .base import BaseConnector, ceil_to
from ..models import Market, Quote

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


class KalshiConnector(BaseConnector):
    venue = "kalshi"

    def __init__(
        self,
        base_url: str = BASE_URL,
        taker_multiplier: float = 0.07,
        maker_fraction: float = 0.25,
        rounding_increment: float = 0.0001,
        series_multipliers: dict[str, float] | None = None,
        **kw,
    ):
        super().__init__(base_url, **kw)
        self.taker_multiplier = float(taker_multiplier)
        self.maker_fraction = float(maker_fraction)
        self.rounding_increment = float(rounding_increment)
        self.series_multipliers = dict(series_multipliers or {})

    def multiplier_for(self, series: str | None) -> float:
        """Per-series override (API doesn't expose category), else the general rate."""
        if series and series in self.series_multipliers:
            return self.series_multipliers[series]
        return self.taker_multiplier

    def fees(
        self, price: float, size: float, side: str = "taker", *, series: str | None = None
    ) -> float:
        """fee = ceil_to_centicent( multiplier * C * p * (1 - p) ); maker = 25% of taker."""
        multiplier = self.multiplier_for(series)
        raw = multiplier * size * price * (1.0 - price)
        if side == "maker":
            raw *= self.maker_fraction
        return ceil_to(raw, self.rounding_increment)

    async def list_markets(self) -> list[Market]:  # phase 3
        raise NotImplementedError(
            "Kalshi discovery is phase 3. GET /markets?series_ticker=...&status=open; "
            "map status active->'open', closed/determined->'closed', finalized->"
            "'resolved'; market_type 'binary'->'binary', 'scalar'->'multi'; times are "
            "ISO-8601 (close_time, expected_expiration_time)."
        )

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:  # phase 3
        raise NotImplementedError(
            "Kalshi polling is phase 3. For top-of-book use GET /markets/{ticker}: "
            "yes_bid_dollars / yes_ask_dollars / no_bid_dollars / no_ask_dollars / "
            "last_price_dollars -> float (already [0,1]). For depth use "
            "GET /markets/{ticker}/orderbook (BIDS ONLY): YES ask = 1 - best no bid, "
            "YES-ask size = best no-bid size."
        )
