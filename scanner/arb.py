"""Within-platform arb detector (design doc §1, §7 phase 2).

Harness for the edge math and the paper-execution loop, run on Manifold (play money,
zero risk). A within-platform arb exists when you can buy a complete set of mutually
exclusive, collectively exhaustive outcomes for less than $1 — exactly one resolves
YES and pays $1:

    binary:  YES_ask + NO_ask < 1
    multi:   sum(answer_ask for all answers) < 1   (shouldAnswersSumToOne markets)

These are pure functions; the connector supplies tradable asks (AMM + limit book) and
its own fees() (0 on Manifold). The §6 cross-venue model is the two-venue analogue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ARB_EPSILON = 1e-9


@dataclass(slots=True)
class ArbLeg:
    outcome_id: str
    label: str
    ask: float
    ask_size: float | None = None


@dataclass(slots=True)
class WithinArb:
    market_id: str
    kind: str  # 'binary' | 'multi'
    legs: list[ArbLeg]
    cost: float  # sum of leg asks (per complete set)
    gross_edge: float  # 1 - cost
    modeled_fees: float  # per complete set
    net_edge: float  # gross - fees
    executable_size: float  # min leg depth (sets capturable at the quoted asks)


def detect(
    market_id: str,
    kind: str,
    legs: list[ArbLeg],
    fees_fn: Callable[[float, float], float],
) -> WithinArb | None:
    """Return a WithinArb iff a complete set costs < $1 (a guaranteed-payout arb).

    `fees_fn(price, size)` is the venue's per-fill cost (Manifold -> 0). `net_edge`
    is reported so the caller can require it stay positive after fees before acting.
    """
    if len(legs) < 2 or any(leg.ask is None for leg in legs):
        return None
    cost = sum(leg.ask for leg in legs)
    gross = 1.0 - cost
    if gross <= ARB_EPSILON:  # complete set costs >= $1: no arb
        return None
    fees = sum(fees_fn(leg.ask, 1.0) for leg in legs)
    sizes = [leg.ask_size for leg in legs if leg.ask_size is not None]
    return WithinArb(
        market_id=market_id,
        kind=kind,
        legs=legs,
        cost=cost,
        gross_edge=gross,
        modeled_fees=fees,
        net_edge=gross - fees,
        executable_size=min(sizes) if sizes else 0.0,
    )
