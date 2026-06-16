"""Config + links loader tests, and a check that shipped settings.toml builds
working connectors with the derived fee constants."""

import textwrap

import pytest

from scanner.config import Settings, load_links
from scanner.connectors.base import build_connectors


def test_settings_load_from_shipped_toml():
    s = Settings.load()
    assert s.scanner.poll_interval_seconds > 0
    assert 0.0 <= s.edge.risk_free_rate < 1.0
    assert set(s.venues) == {"manifold", "kalshi", "polymarket"}


def test_shipped_config_builds_connectors_with_derived_fees():
    s = Settings.load()
    conns = build_connectors(s.venues)

    assert conns["manifold"].fees(0.50, 100, "taker") == 0.0
    assert conns["kalshi"].fees(0.50, 100, "taker") == pytest.approx(1.75)
    # intl per-category rates come straight from settings.toml
    assert conns["polymarket"].fees(0.50, 100, "taker", category="crypto") == pytest.approx(1.75)
    assert conns["polymarket"].fees(0.50, 100, "taker", category="geopolitics") == 0.0
    assert conns["polymarket"].fees(0.50, 100, "taker") == pytest.approx(1.25)  # default rate


def test_load_links_shipped_is_valid():
    """The shipped links.yaml parses and every event is well-formed (exactly 2 legs).

    (Was 'is empty'; links.yaml is populated from phase 1 on, so we pin validity,
    not emptiness.)"""
    links = load_links()
    assert isinstance(links, list)
    for link in links:
        assert len(link.legs) == 2


def test_load_links_parses_two_leg_event(tmp_path):
    p = tmp_path / "links.yaml"
    p.write_text(textwrap.dedent("""
        events:
          - event_id: fed-cut-june-2026
            note: same FOMC date
            resolution_check: confirmed-equivalent
            legs:
              - venue: kalshi
                venue_market_id: FED-26JUN
                buy_outcome: YES
              - venue: polymarket
                venue_market_id: "0xabc"
                buy_outcome: NO
    """))
    links = load_links(p)
    assert len(links) == 1
    link = links[0]
    assert link.event_id == "fed-cut-june-2026"
    assert link.is_suspect is False
    assert [leg.venue for leg in link.legs] == ["kalshi", "polymarket"]
    # YAML reads unquoted YES/NO as booleans; the loader must normalize them back.
    assert [leg.buy_outcome for leg in link.legs] == ["YES", "NO"]


def test_load_links_rejects_non_two_leg(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(textwrap.dedent("""
        events:
          - event_id: one-leg
            legs:
              - venue: kalshi
                venue_market_id: X
                buy_outcome: YES
    """))
    with pytest.raises(ValueError, match="exactly 2"):
        load_links(p)


def test_load_links_rejects_duplicate_event_id(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(textwrap.dedent("""
        events:
          - event_id: dup
            legs:
              - {venue: kalshi, venue_market_id: A, buy_outcome: YES}
              - {venue: polymarket, venue_market_id: B, buy_outcome: NO}
          - event_id: dup
            legs:
              - {venue: kalshi, venue_market_id: C, buy_outcome: YES}
              - {venue: polymarket, venue_market_id: D, buy_outcome: NO}
    """))
    with pytest.raises(ValueError, match="duplicate event_id"):
        load_links(p)
