"""Manifold connector (design doc §5).

DERIVED FROM LIVE DOCS (docs.manifold.markets/api, fetched 2026-06-15):
- Base URL: https://api.manifold.markets/v0
- Auth: NONE for reads (write ops use `Authorization: Key <key>`). v1 is read-only.
- Rate limit: 500 req/min/IP.
- Discovery:  GET /markets, GET /market/{id}, GET /slug/{slug}
- Probability: GET /market/{id}/prob -> {"prob": 0.62} (binary) or {"answerProbs": {...}}
- Multi:      FullMarket.answers[] each carry their own `probability` in [0,1].

WRINKLE (flagged in docs/api-findings.md): Manifold is a CPMM **AMM**, not a CLOB.
There is no native top-of-book bid/ask/size. The canonical quote uses `probability`
as `last` (and as bid==ask mid); sizes are null in v1. Resting limit orders exist via
GET /bets?kinds=open-limit and can deepen the book in a later phase.

Fees: play money (mana). Modeled as 0 (design doc §10), seam kept so the harness
mirrors the real-venue interface exactly.
"""

from __future__ import annotations

from .base import BaseConnector
from ..models import Market, Quote

BASE_URL = "https://api.manifold.markets/v0"


class ManifoldConnector(BaseConnector):
    venue = "manifold"

    def __init__(self, base_url: str = BASE_URL, fee_rate: float = 0.0, **kw):
        super().__init__(base_url, **kw)
        self.fee_rate = float(fee_rate)

    def fees(self, price: float, size: float, side: str = "taker") -> float:
        """Play money — zero cost. If a non-zero `fee_rate` is ever configured,
        mirror the Kalshi/Polymarket p*(1-p) shape so the seam stays comparable."""
        if self.fee_rate == 0.0:
            return 0.0
        return self.fee_rate * size * price * (1.0 - price)

    async def list_markets(self) -> list[Market]:  # phase 1
        raise NotImplementedError(
            "Manifold discovery is phase 1 (One venue E2E). Endpoint: "
            "GET /market/{id} or /slug/{slug}; map outcomeType BINARY->'binary', "
            "MULTIPLE_CHOICE/FREE_RESPONSE->'multi'; closeTime/resolutionTime are "
            "epoch ms; status from isResolved + closeTime."
        )

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:  # phase 1
        raise NotImplementedError(
            "Manifold polling is phase 1. Use GET /market-probs?ids=... (batch up to "
            "100) for binary; for multi, read FullMarket.answers[].probability. "
            "Set quote.last = probability; bid = ask = probability (AMM, no book); "
            "sizes = None."
        )
