"""Edge wiring in the daemon (design doc §6): seed quotes, compute, persist."""

from datetime import datetime, timedelta, timezone

import pytest

from scanner.config import Settings
from scanner.daemon import Scanner
from scanner.models import EventLink, Leg, Market, Outcome, Quote


def _seed_leg(store, venue, vmid, buy_ask, ask_size, res_time):
    m = Market(venue=venue, venue_market_id=vmid, title=f"{venue} mkt",
               market_type="binary", status="open", resolution_time=res_time)
    store.upsert_market(m)
    for label in ("YES", "NO"):
        store.upsert_outcome(Outcome(market_id=m.market_id, label=label))
    return m


def _scanner_with_link(tmp_path, link, rate=0.05):
    settings = Settings.load()
    settings.scanner.db_path = tmp_path / "edge.db"
    settings.edge.risk_free_rate = rate
    return Scanner(settings, [link])


def test_compute_edges_persists_snapshot(tmp_path):
    res = datetime.now(tz=timezone.utc) + timedelta(days=30)
    link = EventLink(
        event_id="fed-hold-jul-2026",
        legs=[Leg("kalshi", "KT", "YES"), Leg("polymarket", "0xPC", "NO")],
        resolution_check="confirmed-equivalent",
    )
    sc = _scanner_with_link(tmp_path, link)
    now = datetime.now(tz=timezone.utc)

    _seed_leg(sc.store, "kalshi", "KT", 0.93, 100, res)
    _seed_leg(sc.store, "polymarket", "0xPC", 0.07, 300, res)
    sc.store.insert_quote(Quote(ts=now, outcome_id="kalshi:KT:YES", ask=0.93, ask_size=100))
    sc.store.insert_quote(Quote(ts=now, outcome_id="polymarket:0xPC:NO", ask=0.07, ask_size=300))

    sc._compute_edges()

    rows = sc.store.edge_history("fed-hold-jul-2026")
    assert len(rows) == 1
    r = rows[0]
    assert r["gross_edge"] == pytest.approx(0.0, abs=1e-9)        # 1 - (0.93 + 0.07)
    assert r["executable_size"] == pytest.approx(100.0)           # min(100, 300)
    assert r["basis_risk_flag"] == 0                              # confirmed + same res time
    # fees come from the real connector models; net = gross - fees - lockup, all positive costs
    exp_fees = (sc.connectors["kalshi"].fees(0.93, 1.0, "taker")
                + sc.connectors["polymarket"].fees(0.07, 1.0, "taker"))
    assert r["modeled_fees"] == pytest.approx(exp_fees)
    assert r["lockup_cost"] > 0
    assert r["net_edge"] == pytest.approx(r["gross_edge"] - r["modeled_fees"] - r["lockup_cost"])
    assert r["net_edge"] < 0                                      # no free lunch here
    sc.store.close()


def test_compute_edges_skips_when_a_leg_has_no_ask(tmp_path):
    link = EventLink(
        event_id="incomplete",
        legs=[Leg("kalshi", "KT", "YES"), Leg("polymarket", "0xPC", "NO")],
    )
    sc = _scanner_with_link(tmp_path, link)
    _seed_leg(sc.store, "kalshi", "KT", 0.93, 100, None)
    _seed_leg(sc.store, "polymarket", "0xPC", 0.07, 300, None)
    # only one leg has a quote
    sc.store.insert_quote(Quote(ts=datetime.now(tz=timezone.utc),
                                outcome_id="kalshi:KT:YES", ask=0.93, ask_size=100))
    sc._compute_edges()
    assert sc.store.edge_history("incomplete") == []
    sc.store.close()


def test_basis_flag():
    t = "2026-07-29T00:00:00+00:00"
    t_far = "2026-08-15T00:00:00+00:00"
    legs = [Leg("kalshi", "A", "YES"), Leg("polymarket", "B", "NO")]
    confirmed = EventLink("e", legs, resolution_check="confirmed-equivalent")
    suspect = EventLink("e", legs, resolution_check="suspect")

    assert Scanner._basis_flag(suspect, {"resolution_time": t}, {"resolution_time": t}) == 1
    assert Scanner._basis_flag(confirmed, {"resolution_time": t}, {"resolution_time": t}) == 0
    assert Scanner._basis_flag(confirmed, {"resolution_time": t}, {"resolution_time": t_far}) == 1
    assert Scanner._basis_flag(confirmed, None, None) == 0  # unknown times -> no time-based flag
