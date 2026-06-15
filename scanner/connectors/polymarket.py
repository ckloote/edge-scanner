"""Polymarket connector (design doc §5).

DERIVED FROM LIVE DOCS (docs.polymarket.com, help.polymarket.com, docs.polymarket.us,
fetched 2026-06-15):
- The PUBLIC read endpoints we poll (Gamma + clob.polymarket.com) serve the
  INTERNATIONAL / crypto-native venue. The CFTC-regulated Polymarket US DCM is a
  SEPARATE venue (polymarket.us); the design doc conflates "the venue you'd trade"
  (US) with what's readable (intl). `venue_mode` selects which fee model to apply.
- Gamma:  https://gamma-api.polymarket.com  (public, no auth)
    GET /markets?closed=false&active=true&slug=...  ->  id, question, conditionId,
    slug, clobTokenIds, outcomes, outcomePrices, endDate, closed, active, volume.
    clobTokenIds / outcomes / outcomePrices are JSON-ENCODED STRINGS — json.loads
    them. outcomes == ["Yes","No"]; clobTokenIds index 0 = YES token, 1 = NO token.
- CLOB:   https://clob.polymarket.com  (book is public, no auth)
    GET /book?token_id=<id>  ->  {bids:[{price,size}...], asks:[{price,size}...],
    tick_size, min_order_size, ...}. Prices are 0-1 fraction strings; bids desc,
    asks asc. Best YES ask = asks[0]; size = asks[0].size.

FEE MODEL (Fee Structure V2 intl, effective 2026-03-30; US effective 2026-04-03):
    fee = feeRate * C * p * (1 - p)
The design doc's "flat 0.10% x premium" (US) and "0.0625 * p*(1-p)" (crypto) are
BOTH stale — see docs/api-findings.md. Current:
- intl: per-category feeRate (crypto 0.07, sports 0.03, finance/politics/tech/
  mentions 0.04, economics/culture/weather/other 0.05, geopolitics 0.0). Makers 0
  (+ rebates), sells exempt.
- us:   uniform taker feeRate 0.05, maker rebate -0.0125. The p*(1-p) parabola
  self-caps at p=0.50 == $1.25 / 100 contracts.
"""

from __future__ import annotations

from .base import BaseConnector
from ..models import Market, Quote

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


class PolymarketConnector(BaseConnector):
    venue = "polymarket"

    def __init__(
        self,
        gamma_url: str = GAMMA_URL,
        clob_url: str = CLOB_URL,
        venue_mode: str = "intl",
        taker_rate: float = 0.05,
        maker_rebate: float = 0.0125,
        category_rates: dict[str, float] | None = None,
        us_cap_per_contract: float = 0.0125,
        default_category: str | None = None,
        **kw,
    ):
        # CLOB is the order-book host; Gamma is discovery. BaseConnector.client is
        # generic, so we keep both base URLs as attributes for the phase-3 read path.
        super().__init__(clob_url, **kw)
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.venue_mode = venue_mode
        self.taker_rate = float(taker_rate)
        self.maker_rebate = float(maker_rebate)
        self.category_rates = dict(category_rates or {})
        self.us_cap_per_contract = float(us_cap_per_contract)
        self.default_category = default_category

    def _taker_rate(self, category: str | None) -> float:
        if self.venue_mode == "us":
            return self.taker_rate
        cat = category or self.default_category
        return self.category_rates.get(cat, self.taker_rate)

    def fees(
        self, price: float, size: float, side: str = "taker", *, category: str | None = None
    ) -> float:
        """fee = feeRate * C * p * (1 - p). Sells exempt; maker rebate (us) is negative."""
        if side == "sell":
            return 0.0  # intl exempts sells; us has no sell-side taker fee either
        if side == "maker":
            if self.venue_mode == "us":
                return -self.maker_rebate * size * price * (1.0 - price)
            return 0.0  # intl makers pay 0 (rebates handled out-of-band)
        rate = self._taker_rate(category)
        return rate * size * price * (1.0 - price)

    async def list_markets(self, venue_market_ids: list[str]) -> list[Market]:  # phase 3
        raise NotImplementedError(
            "Polymarket discovery is phase 3. Gamma GET /markets?closed=false; "
            "json.loads clobTokenIds (0=YES,1=NO) and outcomes; conditionId is the "
            "venue_market_id; endDate is ISO-8601; market_type binary when "
            "outcomes==['Yes','No']."
        )

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:  # phase 3
        raise NotImplementedError(
            "Polymarket polling is phase 3. For each YES/NO token: CLOB "
            "GET /book?token_id=<id>; best bid = bids[0], best ask = asks[0]; "
            "price/size are strings -> float (already [0,1])."
        )
