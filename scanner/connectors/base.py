"""Connector Protocol + shared helpers (design doc §5).

The execution seam (`stream_quotes`, `place_order`) is intentionally left as a
comment, not an abstract method: v1 is read-only, and we don't want the Protocol
to advertise capabilities the connectors don't have.

`httpx` is imported lazily inside the methods that use it so that `fees()` — the
only piece implemented in phase 0 — and its unit tests run without the HTTP stack.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..models import Market, Quote

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx


def ceil_to(value: float, increment: float) -> float:
    """Round `value` UP to the nearest multiple of `increment`.

    Used for venue fee rounding (e.g. Kalshi rounds up to the centicent, 0.0001).
    A tiny epsilon absorbs binary-float noise so exact multiples don't tick up.
    """
    if increment <= 0:
        return value
    return math.ceil(value / increment - 1e-9) * increment


@runtime_checkable
class Connector(Protocol):
    """One Protocol, three implementations (design doc §5)."""

    venue: str

    async def list_markets(self) -> list[Market]:
        """Discover + normalize market/outcome metadata."""
        ...

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:
        """Fetch current book for the curated set. v1 ingestion path."""
        ...

    def fees(self, price: float, size: float, side: str = "taker") -> float:
        """Venue-specific cost for a fill, in dollars. Used by the edge engine.

        `side` is the LIQUIDITY ROLE — "taker" or "maker" — because that, not
        buy-vs-sell, is the axis these venues' fees turn on. The edge engine
        always crosses the spread to buy, so it passes "taker". (Polymarket also
        exempts sells; pass side="sell" there to model a closing fill.)
        """
        ...

    # Deferred (execution phase) — the seam, not implemented in v1:
    # async def stream_quotes(...) -> AsyncIterator[Quote]: ...   # WS
    # async def place_order(...): ...


class BaseConnector:
    """Shared async HTTP client lifecycle. Connectors subclass for the read path."""

    venue: str = ""

    def __init__(self, base_url: str, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: "httpx.AsyncClient | None" = None

    @property
    def client(self) -> "httpx.AsyncClient":
        if self._client is None:
            import httpx  # lazy: keeps fees() + its tests free of the HTTP stack

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


def build_connectors(venues: dict[str, dict]) -> dict[str, "Connector"]:
    """Construct the three connectors from the `[venues.*]` settings dicts."""
    from .kalshi import KalshiConnector
    from .manifold import ManifoldConnector
    from .polymarket import PolymarketConnector

    return {
        "manifold": ManifoldConnector(**venues.get("manifold", {})),
        "kalshi": KalshiConnector(**venues.get("kalshi", {})),
        "polymarket": PolymarketConnector(**venues.get("polymarket", {})),
    }
