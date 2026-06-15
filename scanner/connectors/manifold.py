"""Manifold connector (design doc §5, phase 1 — One venue E2E).

DERIVED FROM LIVE DOCS + live probes (docs.manifold.markets/api, 2026-06-15):
- Base URL: https://api.manifold.markets/v0
- Auth: NONE for reads (write ops use `Authorization: Key <key>`). v1 is read-only.
- Rate limit: 500 req/min/IP.
- Metadata:  GET /market/{id}  (FullMarket); GET /slug/{slug} mirrors it.
- Quotes:    GET /market-probs?ids=a&ids=b  (batch, up to 100; `ids` is a REPEATED
             array param). Returns {marketId: {"prob": x}}  (binary) or
             {marketId: {"answerProbs": {answerId: p}}}  (multiple choice).

WRINKLE (api-findings.md): Manifold is a CPMM **AMM**, not a CLOB — no quoted
bid/ask/size. v1 maps `probability` -> price: YES bid==ask==last==prob, NO == 1-prob;
sizes are None. A binary market stores two outcomes (YES, NO); a multi market stores
one outcome per answer, priced at that answer's probability (the phase-2 within-
platform harness reads these).

`venue_market_id` from links.yaml may be a Manifold market **id** OR a URL **slug** —
both are accepted (id first, slug fallback). The canonical market id we persist is
`manifold:{venue_market_id}` (whatever the curator wrote); the Manifold-internal id is
cached only to drive the batch /market-probs call.

Fees: play money (mana). Modeled as 0 (design doc §10), seam kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .base import BaseConnector
from ..models import Market, Outcome, Quote, make_market_id, make_outcome_id

BASE_URL = "https://api.manifold.markets/v0"
_PROBS_CHUNK = 100  # /market-probs accepts up to 100 ids per call


# --- pure normalization helpers (unit-tested without network) --------------

def market_type_of(outcome_type: str) -> str:
    """Manifold outcomeType -> canonical market_type ('binary' | 'multi')."""
    if outcome_type == "BINARY":
        return "binary"
    if outcome_type in ("MULTIPLE_CHOICE", "FREE_RESPONSE"):
        return "multi"
    raise ValueError(f"unsupported Manifold outcomeType: {outcome_type!r}")


def ms_to_dt(ms: int | None) -> datetime | None:
    """Manifold epoch-milliseconds -> tz-aware UTC datetime (None passes through)."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def status_of(full: dict, *, now_ms: int | None = None) -> str:
    """isResolved -> 'resolved'; past closeTime -> 'closed'; else 'open'."""
    if full.get("isResolved"):
        return "resolved"
    close = full.get("closeTime")
    if now_ms is None:
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if close is not None and close < now_ms:
        return "closed"
    return "open"


def answer_labels(full: dict) -> dict[str, str]:
    """Map Manifold answerId -> answer text (empty for binary markets)."""
    return {a["id"]: a["text"] for a in full.get("answers", [])}


def outcomes_of(market_id: str, full: dict, market_type: str) -> list[Outcome]:
    if market_type == "binary":
        labels = ["YES", "NO"]
    else:
        labels = [a["text"] for a in full.get("answers", [])]
    return [Outcome(market_id=market_id, label=label) for label in labels]


def build_market(curated_id: str, full: dict) -> Market:
    """FullMarket JSON -> canonical Market with its Outcomes attached."""
    market_type = market_type_of(full["outcomeType"])
    market_id = make_market_id("manifold", curated_id)
    market = Market(
        venue="manifold",
        venue_market_id=curated_id,
        title=full.get("question", ""),
        market_type=market_type,
        status=status_of(full),
        close_time=ms_to_dt(full.get("closeTime")),
        resolution_time=ms_to_dt(full.get("resolutionTime")),
        resolution_source=None,  # Manifold has no clean source field; basis risk N/A in-venue
        url=full.get("url"),  # canonical {creatorUsername}/{slug} page
        market_id=market_id,
    )
    market.outcomes = outcomes_of(market_id, full, market_type)
    return market


def binary_quotes(market_id: str, prob: float, ts: datetime) -> list[Quote]:
    """AMM probability -> YES/NO quotes (bid==ask==last; sizes None)."""
    yes = make_outcome_id(market_id, "YES")
    no = make_outcome_id(market_id, "NO")
    return [
        Quote(ts=ts, outcome_id=yes, bid=prob, ask=prob, last=prob),
        Quote(ts=ts, outcome_id=no, bid=1.0 - prob, ask=1.0 - prob, last=1.0 - prob),
    ]


def multi_quotes(
    market_id: str, answer_probs: dict[str, float], labels: dict[str, str], ts: datetime
) -> list[Quote]:
    """Per-answer probabilities -> one quote per answer outcome."""
    out: list[Quote] = []
    for answer_id, prob in answer_probs.items():
        label = labels.get(answer_id)
        if label is None:
            continue  # answer not seen during metadata sync; skip rather than guess
        out.append(
            Quote(
                ts=ts,
                outcome_id=make_outcome_id(market_id, label),
                bid=prob,
                ask=prob,
                last=prob,
            )
        )
    return out


@dataclass(slots=True)
class _Resolved:
    """Cached resolution of a curated id -> Manifold internals (for batch polling)."""

    internal_id: str
    market_type: str
    answer_label_by_id: dict[str, str] = field(default_factory=dict)


# --- connector -------------------------------------------------------------

class ManifoldConnector(BaseConnector):
    venue = "manifold"

    def __init__(self, base_url: str = BASE_URL, fee_rate: float = 0.0, **kw):
        super().__init__(base_url, **kw)
        self.fee_rate = float(fee_rate)
        self._cache: dict[str, _Resolved] = {}

    def fees(self, price: float, size: float, side: str = "taker") -> float:
        """Play money — zero cost. If a non-zero `fee_rate` is ever configured,
        mirror the Kalshi/Polymarket p*(1-p) shape so the seam stays comparable."""
        if self.fee_rate == 0.0:
            return 0.0
        return self.fee_rate * size * price * (1.0 - price)

    async def _fetch_full(self, curated_id: str) -> dict:
        """GET /market/{id}, falling back to /slug/{slug} (curated id may be either)."""
        r = await self.client.get(f"{self.base_url}/market/{curated_id}")
        if r.status_code == 404:
            r = await self.client.get(f"{self.base_url}/slug/{curated_id}")
        r.raise_for_status()
        return r.json()

    async def _resolve(self, curated_id: str) -> Market:
        full = await self._fetch_full(curated_id)
        market = build_market(curated_id, full)
        self._cache[curated_id] = _Resolved(
            internal_id=full["id"],
            market_type=market.market_type,
            answer_label_by_id=answer_labels(full),
        )
        return market

    async def list_markets(self, venue_market_ids: list[str]) -> list[Market]:
        """Fetch + normalize metadata (market + outcomes) for the curated set."""
        return [await self._resolve(cid) for cid in venue_market_ids]

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:
        """Batch-poll current probabilities and normalize to quotes."""
        ts = datetime.now(tz=timezone.utc)

        # Ensure every curated id is resolved (poll may run before a metadata sync).
        for cid in venue_market_ids:
            if cid not in self._cache:
                await self._resolve(cid)

        internal_to_curated = {
            self._cache[cid].internal_id: cid for cid in venue_market_ids
        }
        internal_ids = list(internal_to_curated)

        quotes: list[Quote] = []
        for start in range(0, len(internal_ids), _PROBS_CHUNK):
            chunk = internal_ids[start : start + _PROBS_CHUNK]
            r = await self.client.get(f"{self.base_url}/market-probs", params={"ids": chunk})
            r.raise_for_status()
            for internal_id, payload in r.json().items():
                cid = internal_to_curated.get(internal_id)
                if cid is None:
                    continue
                market_id = make_market_id("manifold", cid)
                if "prob" in payload:
                    quotes.extend(binary_quotes(market_id, payload["prob"], ts))
                elif "answerProbs" in payload:
                    labels = self._cache[cid].answer_label_by_id
                    quotes.extend(
                        multi_quotes(market_id, payload["answerProbs"], labels, ts)
                    )
        return quotes
