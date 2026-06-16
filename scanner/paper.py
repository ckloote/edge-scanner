"""Paper (fake-money) execution harness (design doc §7 phase 2).

Takes a detected WithinArb and records a simulated set of fills — proving the
execution loop end-to-end at ZERO risk. An arb's profit is locked at execution (buy a
complete set for `cost`, receive exactly `size` at resolution), so we record it once
and don't model settlement over time. A per-market cooldown stops the same standing
arb from being "re-executed" every poll cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .arb import WithinArb
from .models import PaperTrade
from .store import Store

log = logging.getLogger("scanner.paper")


class PaperExecutor:
    def __init__(self, store: Store, *, max_stake: float = 100.0, cooldown_s: float = 300.0,
                 min_net_edge: float = 0.0):
        self.store = store
        self.max_stake = max_stake
        self.cooldown_s = cooldown_s
        self.min_net_edge = min_net_edge

    def execute(self, arb: WithinArb, now: datetime) -> PaperTrade | None:
        """Record a paper fill for `arb`, or None if skipped (unprofitable net, or a
        recent trade on this market is still within the cooldown)."""
        if arb.net_edge <= self.min_net_edge:
            return None  # gross arb but fees eat it — don't "trade"
        if self.store.recent_paper_trade(arb.market_id, now - timedelta(seconds=self.cooldown_s)):
            return None  # already captured this standing arb recently

        # Cap the stake; fall back to max_stake when depth is unknown (AMM-only quote).
        size = min(arb.executable_size, self.max_stake) if arb.executable_size > 0 else self.max_stake
        total_fees = arb.modeled_fees * size  # modeled_fees is per-set
        gross_profit = size * arb.gross_edge
        trade = PaperTrade(
            ts=now,
            market_id=arb.market_id,
            kind=arb.kind,
            size=size,
            cost=arb.cost,
            modeled_fees=total_fees,
            gross_profit=gross_profit,
            net_profit=gross_profit - total_fees,
            legs=json.dumps(
                [{"outcome_id": leg.outcome_id, "label": leg.label, "ask": leg.ask}
                 for leg in arb.legs]
            ),
        )
        self.store.insert_paper_trade(trade)
        log.info(
            "paper arb %s (%s): size=%.0f cost=%.4f net_profit=%.4f",
            arb.market_id, arb.kind, size, arb.cost, trade.net_profit,
        )
        return trade
