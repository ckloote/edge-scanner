"""Canonical data model (design doc §4).

Prices are normalized to float in [0, 1] at the connector boundary. Kalshi sends
dollar strings, Polymarket sends fraction strings, Manifold sends probabilities —
connectors normalize all three to the same thing before constructing these.

Timestamps are tz-aware UTC `datetime`. These dataclasses mirror the SQLite tables
in store.py one-to-one; that is deliberate so the Parquet/DuckDB migration (§2
graduation trigger) is a store-layer change, not a model rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Venue(str, Enum):
    MANIFOLD = "manifold"
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class MarketType(str, Enum):
    BINARY = "binary"
    MULTI = "multi"


class Status(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"


def make_market_id(venue: str | Venue, venue_market_id: str) -> str:
    """Canonical market id: f"{venue}:{venue_market_id}" (design doc §4)."""
    venue_str = venue.value if isinstance(venue, Venue) else venue
    return f"{venue_str}:{venue_market_id}"


def make_outcome_id(market_id: str, label: str) -> str:
    """Canonical outcome id: f"{market_id}:{label}"."""
    return f"{market_id}:{label}"


@dataclass(slots=True)
class Market:
    venue: str
    venue_market_id: str
    title: str
    market_type: str  # 'binary' | 'multi'
    status: str  # 'open' | 'closed' | 'resolved'
    close_time: datetime | None = None
    resolution_time: datetime | None = None
    resolution_source: str | None = None
    market_id: str = ""  # derived in __post_init__ if not supplied
    # Transient handoff field — NOT a market table column. Connectors populate it
    # in list_markets() so the daemon can upsert market + outcomes in one pass.
    outcomes: list["Outcome"] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.market_id:
            self.market_id = make_market_id(self.venue, self.venue_market_id)


@dataclass(slots=True)
class Outcome:
    market_id: str
    label: str  # 'YES'/'NO' for binary; option text for multi
    outcome_id: str = ""

    def __post_init__(self) -> None:
        if not self.outcome_id:
            self.outcome_id = make_outcome_id(self.market_id, self.label)


@dataclass(slots=True)
class Quote:
    """One top-of-book snapshot for a single outcome. Prices in [0, 1]."""

    ts: datetime
    outcome_id: str
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None  # shares / contracts
    ask_size: float | None = None
    last: float | None = None


@dataclass(slots=True)
class EdgeSnapshot:
    """Computed cross-venue edge at a point in time (design doc §4, §6).

    The actual research output. `gross_edge = 1 - (ask_a + ask_b)`;
    `net_edge = gross_edge - modeled_fees - lockup_cost`.
    """

    ts: datetime
    event_id: str
    leg_a_outcome_id: str
    leg_b_outcome_id: str
    gross_edge: float
    modeled_fees: float
    lockup_cost: float
    net_edge: float
    executable_size: float
    days_to_resolution: float
    basis_risk_flag: int  # 0/1 (design doc §6)


# --- Event links (hand-curated YAML; design doc §4) -----------------------


@dataclass(slots=True)
class Leg:
    venue: str
    venue_market_id: str
    buy_outcome: str  # 'YES' | 'NO' | option label — the side you BUY


@dataclass(slots=True)
class EventLink:
    event_id: str
    legs: list[Leg]
    note: str = ""
    resolution_check: str = "suspect"  # 'confirmed-equivalent' | 'suspect'

    @property
    def is_suspect(self) -> bool:
        """A 'suspect' link forces basis_risk_flag = 1 (design doc §6)."""
        return self.resolution_check != "confirmed-equivalent"
