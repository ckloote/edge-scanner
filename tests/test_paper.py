"""Paper-execution harness tests (design doc §7 phase 2)."""

import json
from datetime import datetime, timezone

import pytest

from scanner.arb import ArbLeg, WithinArb
from scanner.paper import PaperExecutor
from scanner.store import Store


def _arb(net=0.05, gross=0.05, fees=0.0, exec_size=100.0):
    legs = [ArbLeg("manifold:m:YES", "YES", 0.45, exec_size),
            ArbLeg("manifold:m:NO", "NO", 0.50, exec_size)]
    return WithinArb("manifold:m", "binary", legs, cost=0.95, gross_edge=gross,
                     modeled_fees=fees, net_edge=net, executable_size=exec_size)


def _store(tmp_path):
    return Store(tmp_path / "p.db")


def test_execute_records_locked_profit(tmp_path):
    store = _store(tmp_path)
    ex = PaperExecutor(store, max_stake=100.0)
    now = datetime.now(tz=timezone.utc)
    t = ex.execute(_arb(), now)
    assert t is not None
    assert t.size == pytest.approx(100.0)
    assert t.net_profit == pytest.approx(5.0)  # 100 * 0.05, zero fees
    rows = store.list_paper_trades()
    assert len(rows) == 1
    assert [leg["label"] for leg in json.loads(rows[0]["legs"])] == ["YES", "NO"]
    store.close()


def test_skips_when_net_not_positive(tmp_path):
    store = _store(tmp_path)
    ex = PaperExecutor(store)
    # gross arb but fees make it net-negative -> don't trade
    assert ex.execute(_arb(net=-0.01, gross=0.01, fees=0.02), datetime.now(tz=timezone.utc)) is None
    assert store.list_paper_trades() == []
    store.close()


def test_cooldown_dedups_standing_arb(tmp_path):
    store = _store(tmp_path)
    ex = PaperExecutor(store, cooldown_s=300.0)
    now = datetime.now(tz=timezone.utc)
    assert ex.execute(_arb(), now) is not None
    assert ex.execute(_arb(), now) is None  # same standing arb, within cooldown
    assert len(store.list_paper_trades()) == 1
    store.close()


def test_stake_capped_and_falls_back_when_depth_unknown(tmp_path):
    store = _store(tmp_path)
    ex = PaperExecutor(store, max_stake=50.0)
    now = datetime.now(tz=timezone.utc)
    assert ex.execute(_arb(exec_size=1_000_000), now).size == pytest.approx(50.0)  # capped
    store2 = _store(tmp_path / "sub")
    ex2 = PaperExecutor(store2, max_stake=50.0)
    assert ex2.execute(_arb(exec_size=0.0), now).size == pytest.approx(50.0)  # unknown depth
    store.close()
    store2.close()
