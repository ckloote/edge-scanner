"""Polymarket connector (design doc §5, phase 3).

DERIVED FROM LIVE DOCS + live probes (docs.polymarket.com, 2026-06-15..16):
- The PUBLIC read endpoints we poll (Gamma + clob.polymarket.com) serve the
  INTERNATIONAL / crypto-native venue. The CFTC-regulated Polymarket US DCM is a
  SEPARATE venue; `venue_mode` selects which fee model to apply (see api-findings.md).
- Gamma: https://gamma-api.polymarket.com  (public, no auth)
    GET /markets?condition_ids=0x..  ->  list of markets. Fields used: conditionId,
    question, slug, clobTokenIds, outcomes, endDate, closed, active, resolutionSource,
    events[0].slug. clobTokenIds / outcomes are JSON-ENCODED STRINGS — json.loads them;
    outcomes == ["Yes","No"], clobTokenIds index-aligned (0=YES token, 1=NO token).
- CLOB: https://clob.polymarket.com  (book is public, no auth)
    GET /book?token_id=<id>  ->  {bids:[{price,size}], asks:[{price,size}], tick_size,
    last_trade_price, ...}. Prices are 0-1 fraction strings. YES and NO are SEPARATE
    tokens with SEPARATE books (not exact complements), so both are fetched. Best bid =
    max bid price, best ask = min ask price (robust to sort order).

FEE MODEL (Fee Structure V2 intl 2026-03-30; US 2026-04-03):
    fee = feeRate * C * p * (1 - p)   (see api-findings.md; design doc's flat 0.10% and
    0.0625 formulas are both stale). Makers 0 (intl) / rebate (us); sells exempt.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .base import BaseConnector
from ..models import Market, Outcome, Quote, make_market_id, make_outcome_id

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
_CID_CHUNK = 100


# --- pure normalization helpers (unit-tested without network) --------------

def parse_json_list(value) -> list:
    """Gamma encodes clobTokenIds/outcomes as JSON strings; pass-through if already a list."""
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def parse_iso(s) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)  # 3.11+ parses trailing 'Z'
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def poly_status(market: dict) -> str:
    """active & not closed -> 'open'; else 'closed' (resolved detection is a refinement)."""
    return "open" if market.get("active") and not market.get("closed") else "closed"


def poly_url(market: dict) -> str | None:
    events = market.get("events") or []
    if events and events[0].get("slug"):
        return f"https://polymarket.com/event/{events[0]['slug']}"
    if market.get("slug"):
        return f"https://polymarket.com/event/{market['slug']}"
    return None


def tokens_by_label(market: dict) -> dict[str, str]:
    """{'YES': token_id, 'NO': token_id} (outcome labels upper-cased to canonical)."""
    tokens = parse_json_list(market.get("clobTokenIds"))
    outcomes = parse_json_list(market.get("outcomes"))
    return {label.upper(): tok for label, tok in zip(outcomes, tokens)}


def build_market(market: dict) -> Market:
    """Gamma market JSON -> canonical Market (venue_market_id = conditionId)."""
    cid = market["conditionId"]
    market_id = make_market_id("polymarket", cid)
    outcomes = parse_json_list(market.get("outcomes")) or ["Yes", "No"]
    m = Market(
        venue="polymarket",
        venue_market_id=cid,
        title=market.get("question", ""),
        market_type="binary" if len(outcomes) == 2 else "multi",
        status=poly_status(market),
        close_time=parse_iso(market.get("endDate")),
        resolution_time=parse_iso(market.get("endDate")),  # no separate field; endDate ≈ resolution
        resolution_source=market.get("resolutionSource") or None,
        url=poly_url(market),
        market_id=market_id,
    )
    m.outcomes = [Outcome(market_id=market_id, label=o.upper()) for o in outcomes]
    return m


def _best(entries: list, *, lowest: bool):
    if not entries:
        return None
    chooser = min if lowest else max
    return chooser(entries, key=lambda e: float(e["price"]))


def book_quote(market_id: str, label: str, book: dict, ts: datetime) -> Quote:
    """CLOB book for one token -> a Quote (best bid = max price, best ask = min price)."""
    bid = _best(book.get("bids", []), lowest=False)
    ask = _best(book.get("asks", []), lowest=True)
    last = book.get("last_trade_price")
    return Quote(
        ts=ts,
        outcome_id=make_outcome_id(market_id, label),
        bid=float(bid["price"]) if bid else None,
        ask=float(ask["price"]) if ask else None,
        bid_size=float(bid["size"]) if bid else None,
        ask_size=float(ask["size"]) if ask else None,
        last=float(last) if last not in (None, "") else None,
    )


# --- connector -------------------------------------------------------------

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
        super().__init__(clob_url, **kw)
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.venue_mode = venue_mode
        self.taker_rate = float(taker_rate)
        self.maker_rebate = float(maker_rebate)
        self.category_rates = dict(category_rates or {})
        self.us_cap_per_contract = float(us_cap_per_contract)
        self.default_category = default_category
        self._tokens: dict[str, dict[str, str]] = {}  # conditionId -> {label: token}

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
            return 0.0
        if side == "maker":
            if self.venue_mode == "us":
                return -self.maker_rebate * size * price * (1.0 - price)
            return 0.0
        rate = self._taker_rate(category)
        return rate * size * price * (1.0 - price)

    async def _fetch_gamma(self, condition_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(condition_ids), _CID_CHUNK):
            chunk = condition_ids[start : start + _CID_CHUNK]
            r = await self.client.get(
                f"{self.gamma_url}/markets", params={"condition_ids": chunk}
            )
            r.raise_for_status()
            out.extend(r.json())
        return out

    async def _resolve(self, condition_id: str) -> Market | None:
        markets = await self._fetch_gamma([condition_id])
        if not markets:
            return None
        self._tokens[condition_id] = tokens_by_label(markets[0])
        return build_market(markets[0])

    async def list_markets(self, venue_market_ids: list[str]) -> list[Market]:
        """Fetch + normalize metadata (market + YES/NO outcomes) for the curated set."""
        out: list[Market] = []
        for market in await self._fetch_gamma(venue_market_ids):
            self._tokens[market["conditionId"]] = tokens_by_label(market)
            out.append(build_market(market))
        return out

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:
        """Fetch each YES/NO token's CLOB book and normalize to quotes."""
        ts = datetime.now(tz=timezone.utc)
        for cid in venue_market_ids:
            if cid not in self._tokens:
                await self._resolve(cid)

        quotes: list[Quote] = []
        for cid in venue_market_ids:
            market_id = make_market_id("polymarket", cid)
            for label, token in self._tokens.get(cid, {}).items():
                r = await self.client.get(f"{self.clob_url}/book", params={"token_id": token})
                r.raise_for_status()
                quotes.append(book_quote(market_id, label, r.json(), ts))
        return quotes
