"""Curation-assistant matching (pure functions; the CLI is scripts/curate.py)."""

from datetime import datetime, timedelta, timezone

from scanner.curation import (
    CandidateMarket,
    match_candidates,
    title_score,
    tokens,
    yaml_stanza,
)

RES = datetime(2026, 10, 28, tzinfo=timezone.utc)


def _cand(venue, vmid, title, res=RES, yes=None):
    return CandidateMarket(venue=venue, venue_market_id=vmid, title=title,
                           resolution=res, yes_price=yes)


def test_tokens_drop_stopwords_and_short():
    assert tokens("Will the USA win the 2026 FIFA World Cup?") == {
        "usa", "2026", "fifa", "world", "cup"
    }


def test_title_score_orders_related_above_unrelated():
    fed_k = "Fed decision in October 2026: maintain rates?"
    fed_p = "Fed maintains rates at the October 2026 FOMC meeting?"
    wc_p = "Will Spain win the 2026 FIFA World Cup?"
    assert title_score(fed_k, fed_p) > title_score(fed_k, wc_p)
    assert title_score(fed_k, fed_p) >= 0.5


def test_match_pairs_and_ranks():
    kalshi = [
        _cand("kalshi", "KXFED-26OCT-H0", "Fed decision in October 2026: maintain rates?"),
        _cand("kalshi", "KXWTAWIN-GAUFF", "Will Coco Gauff win the WTA Finals?"),
    ]
    poly = [
        _cand("polymarket", "0xfed", "Fed maintains rates at the October 2026 FOMC meeting?"),
        _cand("polymarket", "0xwc", "Will Spain win the 2026 FIFA World Cup?"),
    ]
    matches = match_candidates(kalshi, poly, min_score=0.4)
    assert [(m.a.venue_market_id, m.b.venue_market_id) for m in matches] == [
        ("KXFED-26OCT-H0", "0xfed")
    ]


def test_date_gate_blocks_mismatched_resolutions():
    k = [_cand("kalshi", "K1", "Fed maintains rates in October 2026?", res=RES)]
    p = [_cand("polymarket", "0x1", "Fed maintains rates in October 2026?",
               res=RES + timedelta(days=30))]
    assert match_candidates(k, p, min_score=0.4) == []
    p_unknown = [_cand("polymarket", "0x1", "Fed maintains rates in October 2026?", res=None)]
    assert match_candidates(k, p_unknown, min_score=0.4) == []


def test_already_linked_pairs_excluded():
    k = [_cand("kalshi", "K1", "Fed maintains rates in October 2026?")]
    p = [_cand("polymarket", "0x1", "Fed maintains rates in October 2026?")]
    assert match_candidates(k, p, exclude={("kalshi", "K1")}) == []
    assert match_candidates(k, p, exclude={("polymarket", "0x1")}) == []
    assert len(match_candidates(k, p)) == 1


def test_price_band_drops_effectively_decided_markets():
    k_dead = [_cand("kalshi", "K1", "Fed maintains rates in October 2026?", yes=0.99)]
    p = [_cand("polymarket", "0x1", "Fed maintains rates in October 2026?", yes=0.50)]
    assert match_candidates(k_dead, p, price_band=0.05) == []
    k_live = [_cand("kalshi", "K1", "Fed maintains rates in October 2026?", yes=0.30)]
    p_dead = [_cand("polymarket", "0x1", "Fed maintains rates in October 2026?", yes=0.02)]
    assert match_candidates(k_live, p_dead, price_band=0.05) == []
    assert len(match_candidates(k_live, p, price_band=0.05)) == 1
    # unknown price passes (missing data isn't a dead market); band 0 disables
    k_none = [_cand("kalshi", "K1", "Fed maintains rates in October 2026?", yes=None)]
    assert len(match_candidates(k_none, p, price_band=0.05)) == 1
    assert len(match_candidates(k_dead, p, price_band=0.0)) == 1


def test_divergence_and_stanza_defaults_suspect():
    k = _cand("kalshi", "KXFED-26OCT-H0", "Fed maintains rates in October 2026?", yes=0.80)
    p = _cand("polymarket", "0xabc", "Fed maintains October 2026?", yes=0.75)
    (m,) = match_candidates([k], [p], min_score=0.3)
    assert m.divergence is not None and abs(m.divergence - 0.05) < 1e-9
    stanza = yaml_stanza(m, today="2026-07-02")
    assert "resolution_check: suspect" in stanza
    assert "venue_market_id: KXFED-26OCT-H0" in stanza
    assert 'buy_outcome: "NO"' in stanza
