"""Kalshi connector (design doc §5, phase 3).

DERIVED FROM LIVE DOCS + live probes (docs.kalshi.com, kalshi.com/fee-schedule, 2026-06-15):
- Base URL: https://external-api.kalshi.com/trade-api/v2
- Auth: market-data endpoints are PUBLIC. The v1 read-only path needs NO auth.
  (Trading, deferred, uses per-request RSA-PSS signing via KALSHI-ACCESS-KEY/
  -SIGNATURE/-TIMESTAMP headers — NOT the old session-token flow. See api-findings.md.)
- Discovery + quotes: GET /markets?tickers=t1,t2 (batch, up to ~100), GET /markets/{ticker}.
- Prices: DOLLAR STRINGS in [0,1], e.g. yes_bid_dollars="0.4420" (NOT integer cents —
  the design doc is stale here; `response_price_units: usd_cent` is a legacy header, the
  `*_dollars` fields are dollars). Each market is a single YES/NO contract.
- Top-of-book comes entirely from the market object: yes_bid/ask_dollars,
  no_bid/ask_dollars, last_price_dollars, plus yes_bid_size_fp / yes_ask_size_fp.
  The book is BIDS ONLY, so NO bid == YES ask orders (and vice-versa): NO bid size =
  yes_ask_size, NO ask size = yes_bid_size. (The /orderbook endpoint is only needed for
  depth beyond top-of-book, which v1 defers — design doc §10.)

FEE MODEL (kalshi.com/fee-schedule, docs.kalshi.com/getting_started/fee_rounding):
    fee = ceil_to_centicent( multiplier * C * p * (1 - p) )
- multiplier = 0.07 general; the market object exposes NO category/multiplier field, so
  any override must be configured per series ticker (series_multipliers).
- maker fee = 25% of taker. Rounding is UP to the centicent ($0.0001), NOT the whole
  cent the design doc assumed. No settlement fee.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import BaseConnector, ceil_to
from ..models import Market, Outcome, Quote, make_market_id, make_outcome_id

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
_TICKERS_CHUNK = 100  # GET /markets accepts a bounded comma list per call


# --- pure normalization helpers (unit-tested without network) --------------

def parse_price(s) -> float | None:
    """Dollar string -> float in [0,1] (None/'' -> None)."""
    if s is None or s == "":
        return None
    return float(s)


def parse_size(s) -> float | None:
    """Fixed-point size string -> float (None/'' -> None)."""
    if s is None or s == "":
        return None
    return float(s)


def parse_iso(s) -> datetime | None:
    """Kalshi ISO-8601 (…Z) -> tz-aware UTC datetime (None passes through)."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)  # 3.11+ parses the trailing 'Z'
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def kalshi_status(market: dict) -> str:
    """Kalshi status/result -> canonical 'open' | 'closed' | 'resolved'."""
    if market.get("result") in ("yes", "no") or market.get("status") in ("finalized", "settled"):
        return "resolved"
    if market.get("status") == "active":
        return "open"
    return "closed"


def kalshi_url(event_ticker: str | None) -> str | None:
    """Best-effort web link (Kalshi returns no canonical url field).

    Kalshi's web path is ``/markets/{series-ticker-lowercased}[/{slug}/{event}]``.
    The API market object carries no ``series_ticker``, but the series is the event
    ticker up to the first ``-`` (``KXFEDDECISION-26JUL`` -> ``kxfeddecision``). We
    link to that bare-series page (which lists the dated markets); the deeper
    ``/{slug}/{event}`` segment isn't derivable from the API. The old form linked the
    uppercase *event* ticker, which Kalshi no longer routes (dead link).
    """
    if not event_ticker:
        return None
    series = event_ticker.split("-", 1)[0].lower()
    return f"https://kalshi.com/markets/{series}"


def build_market(market: dict) -> Market:
    """Kalshi market JSON -> canonical Market (always a YES/NO binary contract)."""
    ticker = market["ticker"]
    market_id = make_market_id("kalshi", ticker)
    m = Market(
        venue="kalshi",
        venue_market_id=ticker,
        title=market.get("title") or market.get("yes_sub_title") or ticker,
        market_type="binary",  # a Kalshi market is one YES/NO contract; multi is event-level
        status=kalshi_status(market),
        close_time=parse_iso(market.get("close_time")),
        resolution_time=parse_iso(
            market.get("expected_expiration_time") or market.get("expiration_time")
        ),
        resolution_source=market.get("rules_primary"),  # criteria text -> basis-risk signal
        url=kalshi_url(market.get("event_ticker")),
        market_id=market_id,
    )
    m.outcomes = [Outcome(market_id=market_id, label="YES"), Outcome(market_id=market_id, label="NO")]
    return m


def market_quotes(market: dict, ts: datetime) -> list[Quote]:
    """Top-of-book YES + NO quotes from one market object.

    Book is bids-only, so NO bid == YES ask orders: NO bid size = yes_ask_size,
    NO ask size = yes_bid_size.
    """
    market_id = make_market_id("kalshi", market["ticker"])
    yes_bid = parse_price(market.get("yes_bid_dollars"))
    yes_ask = parse_price(market.get("yes_ask_dollars"))
    no_bid = parse_price(market.get("no_bid_dollars"))
    no_ask = parse_price(market.get("no_ask_dollars"))
    yes_bid_size = parse_size(market.get("yes_bid_size_fp"))
    yes_ask_size = parse_size(market.get("yes_ask_size_fp"))
    yes_last = parse_price(market.get("last_price_dollars"))
    no_last = None if yes_last is None else 1.0 - yes_last

    return [
        Quote(
            ts=ts,
            outcome_id=make_outcome_id(market_id, "YES"),
            bid=yes_bid, ask=yes_ask,
            bid_size=yes_bid_size, ask_size=yes_ask_size,
            last=yes_last,
        ),
        Quote(
            ts=ts,
            outcome_id=make_outcome_id(market_id, "NO"),
            bid=no_bid, ask=no_ask,
            bid_size=yes_ask_size,  # NO bid == YES ask orders
            ask_size=yes_bid_size,  # NO ask == YES bid orders
            last=no_last,
        ),
    ]


# --- connector -------------------------------------------------------------

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

    async def _fetch_markets(self, tickers: list[str]) -> list[dict]:
        """Batch GET /markets?tickers=... (chunked); returns raw market dicts."""
        out: list[dict] = []
        for start in range(0, len(tickers), _TICKERS_CHUNK):
            chunk = tickers[start : start + _TICKERS_CHUNK]
            r = await self.client.get(
                f"{self.base_url}/markets",
                params={"tickers": ",".join(chunk), "limit": 1000},
            )
            r.raise_for_status()
            out.extend(r.json().get("markets", []))
        return out

    async def list_markets(self, venue_market_ids: list[str]) -> list[Market]:
        """Fetch + normalize metadata (market + YES/NO outcomes) for the curated set."""
        return [build_market(m) for m in await self._fetch_markets(venue_market_ids)]

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:
        """Batch-poll top-of-book and normalize to YES/NO quotes."""
        ts = datetime.now(tz=timezone.utc)
        quotes: list[Quote] = []
        for market in await self._fetch_markets(venue_market_ids):
            quotes.extend(market_quotes(market, ts))
        return quotes
