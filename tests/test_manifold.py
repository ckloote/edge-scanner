"""Manifold connector tests (phase 1).

Normalization is tested with RECORDED API shapes (probed live 2026-06-15) so CI
never touches the network. One integration test exercises the async read paths
against an httpx.MockTransport serving those same shapes.
"""

from datetime import datetime, timezone

import httpx
import pytest

from scanner.connectors.manifold import (
    ManifoldConnector,
    answer_labels,
    binary_quotes,
    build_market,
    market_type_of,
    ms_to_dt,
    multi_quotes,
    status_of,
)

# --- recorded fixtures (trimmed real responses) ---------------------------

BINARY_FULL = {
    "id": "P0R6uAC2nU",
    "question": "Will Anthropic restore access to Fable 5 to all customers by the end of June?",
    "slug": "will-anthropic-restore-access-to-fa",
    "outcomeType": "BINARY",
    "mechanism": "cpmm-1",
    "probability": 0.4,
    "closeTime": 1782863940000,
    "resolutionTime": None,
    "isResolved": False,
    "resolution": None,
    "token": "MANA",
    "url": "https://manifold.markets/Someone/will-anthropic-restore-access-to-fa",
}

MULTI_FULL = {
    "id": "qtUIl9NEh8",
    "question": "Who will win the 2026 Georgia Republican primary?",
    "slug": "who-will-win-the-2026-georgia-repub",
    "outcomeType": "MULTIPLE_CHOICE",
    "mechanism": "cpmm-multi-1",
    "shouldAnswersSumToOne": True,
    "closeTime": 1790000000000,
    "isResolved": False,
    "answers": [
        {"id": "ZgduO56upn", "text": "Rick Jackson", "probability": 0.24},
        {"id": "9OSOqNPqZ8", "text": "Burt Jones", "probability": 0.76},
    ],
}

BATCH_PROBS = {
    "P0R6uAC2nU": {"prob": 0.4},
    "qtUIl9NEh8": {"answerProbs": {"ZgduO56upn": 0.24, "9OSOqNPqZ8": 0.76}},
}


# --- pure normalization ----------------------------------------------------

def test_market_type_of():
    assert market_type_of("BINARY") == "binary"
    assert market_type_of("MULTIPLE_CHOICE") == "multi"
    assert market_type_of("FREE_RESPONSE") == "multi"
    with pytest.raises(ValueError):
        market_type_of("NUMERIC")


def test_ms_to_dt():
    assert ms_to_dt(None) is None
    assert ms_to_dt(0) is None
    dt = ms_to_dt(1782863940000)
    assert dt == datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc)


def test_status_of():
    assert status_of({"isResolved": True}) == "resolved"
    assert status_of({"closeTime": 1_000}, now_ms=2_000) == "closed"
    assert status_of({"closeTime": 9_000}, now_ms=2_000) == "open"


def test_build_market_binary():
    m = build_market("P0R6uAC2nU", BINARY_FULL)
    assert m.venue == "manifold"
    assert m.market_id == "manifold:P0R6uAC2nU"
    assert m.market_type == "binary"
    assert m.status == "open"
    assert m.close_time == datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc)
    assert m.url == "https://manifold.markets/Someone/will-anthropic-restore-access-to-fa"
    assert [o.label for o in m.outcomes] == ["YES", "NO"]
    assert m.outcomes[0].outcome_id == "manifold:P0R6uAC2nU:YES"


def test_build_market_multi():
    m = build_market("qtUIl9NEh8", MULTI_FULL)
    assert m.market_type == "multi"
    assert [o.label for o in m.outcomes] == ["Rick Jackson", "Burt Jones"]


def test_answer_labels():
    assert answer_labels(MULTI_FULL) == {"ZgduO56upn": "Rick Jackson", "9OSOqNPqZ8": "Burt Jones"}
    assert answer_labels(BINARY_FULL) == {}


def test_binary_quotes_yes_and_no():
    ts = datetime(2026, 6, 15, tzinfo=timezone.utc)
    qs = binary_quotes("manifold:P0R6uAC2nU", 0.4, ts)
    yes, no = qs
    assert yes.outcome_id.endswith(":YES")
    assert (yes.bid, yes.ask, yes.last) == (0.4, 0.4, 0.4)
    assert no.outcome_id.endswith(":NO")
    assert no.bid == pytest.approx(0.6)
    assert yes.bid_size is None and yes.ask_size is None  # AMM: no quoted size


def test_multi_quotes_uses_labels():
    ts = datetime(2026, 6, 15, tzinfo=timezone.utc)
    labels = answer_labels(MULTI_FULL)
    qs = multi_quotes("manifold:qtUIl9NEh8", {"ZgduO56upn": 0.24, "9OSOqNPqZ8": 0.76}, labels, ts)
    by_label = {q.outcome_id.rsplit(":", 1)[1]: q.last for q in qs}
    assert by_label == {"Rick Jackson": 0.24, "Burt Jones": 0.76}


def test_multi_quotes_skips_unknown_answer():
    ts = datetime(2026, 6, 15, tzinfo=timezone.utc)
    qs = multi_quotes("manifold:x", {"unknown": 0.5}, {}, ts)
    assert qs == []


# --- async integration against a mock transport ----------------------------

def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v0/market/P0R6uAC2nU":
        return httpx.Response(200, json=BINARY_FULL)
    if path == "/v0/market/qtUIl9NEh8":
        return httpx.Response(200, json=MULTI_FULL)
    if path == "/v0/market/will-anthropic-restore-access-to-fa":
        return httpx.Response(404, json={"message": "Not found"})
    if path == "/v0/slug/will-anthropic-restore-access-to-fa":
        return httpx.Response(200, json=BINARY_FULL)
    if path == "/v0/market-probs":
        ids = request.url.params.get_list("ids")
        return httpx.Response(200, json={k: v for k, v in BATCH_PROBS.items() if k in ids})
    return httpx.Response(404, json={"message": f"unrouted {path}"})


def _mock_connector() -> ManifoldConnector:
    c = ManifoldConnector()
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return c


async def test_list_markets_resolves_id_and_slug():
    c = _mock_connector()
    try:
        markets = await c.list_markets(["P0R6uAC2nU", "will-anthropic-restore-access-to-fa"])
        assert {m.market_id for m in markets} == {
            "manifold:P0R6uAC2nU",
            "manifold:will-anthropic-restore-access-to-fa",  # resolved via /slug fallback
        }
        assert all(m.market_type == "binary" for m in markets)
    finally:
        await c.aclose()


async def test_poll_quotes_binary_and_multi():
    c = _mock_connector()
    try:
        await c.list_markets(["P0R6uAC2nU", "qtUIl9NEh8"])  # populate the resolution cache
        quotes = await c.poll_quotes(["P0R6uAC2nU", "qtUIl9NEh8"])
        priced = {q.outcome_id: q.last for q in quotes}
        assert priced["manifold:P0R6uAC2nU:YES"] == pytest.approx(0.4)
        assert priced["manifold:P0R6uAC2nU:NO"] == pytest.approx(0.6)
        assert priced["manifold:qtUIl9NEh8:Burt Jones"] == pytest.approx(0.76)
    finally:
        await c.aclose()


async def test_poll_quotes_self_resolves_without_prior_sync():
    c = _mock_connector()
    try:
        quotes = await c.poll_quotes(["P0R6uAC2nU"])  # no list_markets first
        assert any(q.outcome_id == "manifold:P0R6uAC2nU:YES" for q in quotes)
    finally:
        await c.aclose()
