"""The edge model (design doc §6) — pure functions, unit-tested.

For a binary event linked across two venues, the arb pair is: buy YES on venue A
at ask_a, buy NO on venue B at ask_b (per the link's polarity). At resolution
exactly one leg pays $1, so:

    gross_edge      = 1 - (ask_a + ask_b)          # arb exists when ask_a + ask_b < 1
    capital         = ask_a + ask_b                # tied up until resolution
    modeled_fees    = connector_A.fees(...) + connector_B.fees(...)
    lockup_cost     = risk_free_rate * (days_to_resolution / 365) * capital
    net_edge        = gross_edge - modeled_fees - lockup_cost
    executable_size = min(ask_size_a, ask_size_b)

Fees and lockup are expressed PER CONTRACT/SHARE here (gross_edge is a per-$1-payout
quantity), so the connector fee is computed at size=1 and the model stays
size-independent except for `executable_size`, which is reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def gross_edge(ask_a: float, ask_b: float) -> float:
    """1 - (ask_a + ask_b). Positive => a quoted cross-venue arb exists."""
    return 1.0 - (ask_a + ask_b)


def capital(ask_a: float, ask_b: float) -> float:
    """Capital tied up per unit payout until resolution."""
    return ask_a + ask_b


def lockup_cost(risk_free_rate: float, days_to_resolution: float, capital_: float) -> float:
    """Annualized opportunity cost of the locked capital (design doc §6).

    A 2% gross spread resolving in 6 months is ~4% annualized BEFORE fees — worse
    than T-bills. This term surfaces that directly.
    """
    return risk_free_rate * (days_to_resolution / 365.0) * capital_


def days_between(now: datetime, resolution_time: datetime | None) -> float:
    """Days from `now` to `resolution_time`; 0.0 if unknown (conservative: no lockup)."""
    if resolution_time is None:
        return 0.0
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if resolution_time.tzinfo is None:
        resolution_time = resolution_time.replace(tzinfo=timezone.utc)
    return max(0.0, (resolution_time - now).total_seconds() / 86400.0)


@dataclass(slots=True)
class EdgeInputs:
    """One linked event's live state, ready for the edge computation."""

    ask_a: float
    ask_b: float
    ask_size_a: float
    ask_size_b: float
    fee_a: float  # connector_A.fees(ask_a, size=1, side="taker")
    fee_b: float  # connector_B.fees(ask_b, size=1, side="taker")
    days_to_resolution: float
    basis_risk_flag: int


@dataclass(slots=True)
class EdgeResult:
    gross_edge: float
    modeled_fees: float
    lockup_cost: float
    net_edge: float
    executable_size: float
    days_to_resolution: float
    basis_risk_flag: int


def compute_edge(inp: EdgeInputs, risk_free_rate: float) -> EdgeResult:
    """Apply the §6 model to one linked event. Pure; no I/O."""
    g = gross_edge(inp.ask_a, inp.ask_b)
    cap = capital(inp.ask_a, inp.ask_b)
    fees = inp.fee_a + inp.fee_b
    lock = lockup_cost(risk_free_rate, inp.days_to_resolution, cap)
    return EdgeResult(
        gross_edge=g,
        modeled_fees=fees,
        lockup_cost=lock,
        net_edge=g - fees - lock,
        executable_size=min(inp.ask_size_a, inp.ask_size_b),
        days_to_resolution=inp.days_to_resolution,
        basis_risk_flag=inp.basis_risk_flag,
    )
