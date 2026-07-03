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


def test_compute_edges_picks_better_direction(tmp_path):
    """When the encoded direction is a loss but its mirror is profitable, persist the
    mirror (buy NO on A + YES on B)."""
    res = datetime.now(tz=timezone.utc) + timedelta(days=30)
    link = EventLink(
        event_id="divergent",
        legs=[Leg("kalshi", "KT", "YES"), Leg("polymarket", "0xPC", "NO")],
        resolution_check="confirmed-equivalent",
    )
    sc = _scanner_with_link(tmp_path, link)
    now = datetime.now(tz=timezone.utc)
    _seed_leg(sc.store, "kalshi", "KT", 0, 0, res)
    _seed_leg(sc.store, "polymarket", "0xPC", 0, 0, res)
    # encoded dir (KT:YES + 0xPC:NO) costs 0.78 + 0.32 = 1.10 -> gross -0.10
    sc.store.insert_quote(Quote(ts=now, outcome_id="kalshi:KT:YES", ask=0.78, ask_size=100))
    sc.store.insert_quote(Quote(ts=now, outcome_id="polymarket:0xPC:NO", ask=0.32, ask_size=100))
    # mirror dir (KT:NO + 0xPC:YES) costs 0.22 + 0.71 = 0.93 -> gross +0.07
    sc.store.insert_quote(Quote(ts=now, outcome_id="kalshi:KT:NO", ask=0.22, ask_size=100))
    sc.store.insert_quote(Quote(ts=now, outcome_id="polymarket:0xPC:YES", ask=0.71, ask_size=100))

    sc._compute_edges()
    r = sc.store.edge_history("divergent")[-1]
    assert r["gross_edge"] == pytest.approx(0.07)  # the mirror, not -0.10
    assert r["leg_a_outcome_id"] == "kalshi:KT:NO"
    assert r["leg_b_outcome_id"] == "polymarket:0xPC:YES"
    # The losing direction (gross -0.10) is captured too: its net is the -0.10
    # gross minus its own fees and lockup, so strictly below the winner's net.
    assert r["mirror_net_edge"] is not None
    assert r["mirror_net_edge"] < r["net_edge"]
    assert r["mirror_net_edge"] < -0.10  # gross -0.10 less costs
    assert r["mirror_executable_size"] == pytest.approx(100.0)
    sc.store.close()


def test_mirror_columns_null_when_only_one_direction_quotable(tmp_path):
    """Only the encoded direction has asks -> main columns fill, mirror stays NULL."""
    res = datetime.now(tz=timezone.utc) + timedelta(days=30)
    link = EventLink(
        event_id="one-sided",
        legs=[Leg("kalshi", "KT", "YES"), Leg("polymarket", "0xPC", "NO")],
        resolution_check="confirmed-equivalent",
    )
    sc = _scanner_with_link(tmp_path, link)
    now = datetime.now(tz=timezone.utc)
    _seed_leg(sc.store, "kalshi", "KT", 0, 0, res)
    _seed_leg(sc.store, "polymarket", "0xPC", 0, 0, res)
    sc.store.insert_quote(Quote(ts=now, outcome_id="kalshi:KT:YES", ask=0.60, ask_size=50))
    sc.store.insert_quote(Quote(ts=now, outcome_id="polymarket:0xPC:NO", ask=0.35, ask_size=80))
    # no KT:NO / 0xPC:YES quotes -> the mirror direction is unquotable

    sc._compute_edges()
    r = sc.store.edge_history("one-sided")[-1]
    assert r["gross_edge"] == pytest.approx(0.05)
    assert r["mirror_net_edge"] is None
    assert r["mirror_executable_size"] is None
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


def test_retired_link_stops_polling_and_edges(tmp_path):
    """A link with a resolved leg drops out of the live set and gains no edge rows;
    its history stays put (auto-retire guard)."""
    res = datetime.now(tz=timezone.utc) + timedelta(days=30)
    link = EventLink(
        event_id="retiree",
        legs=[Leg("kalshi", "KT", "YES"), Leg("polymarket", "0xPC", "NO")],
        resolution_check="confirmed-equivalent",
    )
    sc = _scanner_with_link(tmp_path, link)
    now = datetime.now(tz=timezone.utc)
    kalshi_mkt = _seed_leg(sc.store, "kalshi", "KT", 0.93, 100, res)
    _seed_leg(sc.store, "polymarket", "0xPC", 0.07, 300, res)
    sc.store.insert_quote(Quote(ts=now, outcome_id="kalshi:KT:YES", ask=0.93, ask_size=100))
    sc.store.insert_quote(Quote(ts=now, outcome_id="polymarket:0xPC:NO", ask=0.07, ask_size=300))

    assert [lk.event_id for lk in sc._live_links()] == ["retiree"]
    sc._compute_edges()
    assert len(sc.store.edge_history("retiree")) == 1

    kalshi_mkt.status = "resolved"  # the venue finalized this market
    sc.store.upsert_market(kalshi_mkt)
    assert sc._live_links() == []
    sc._compute_edges()  # default path must apply the guard too
    assert len(sc.store.edge_history("retiree")) == 1  # no new rows
    sc.store.close()


def test_unsynced_markets_count_as_live(tmp_path):
    """Before the first metadata sync there are no market rows — the link must
    still be polled (retiring on ignorance would kill every link at boot)."""
    link = EventLink(
        event_id="fresh",
        legs=[Leg("kalshi", "KT", "YES"), Leg("polymarket", "0xPC", "NO")],
    )
    sc = _scanner_with_link(tmp_path, link)
    assert [lk.event_id for lk in sc._live_links()] == ["fresh"]
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
