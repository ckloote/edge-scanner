"""Polymarket connector read-path tests (phase 3).

Recorded Gamma + CLOB shapes (probed live 2026-06-16); no network in CI. fees()
lives in test_fees.py.
"""

from datetime import datetime, timezone

import httpx
import pytest

from scanner.connectors.polymarket import (
    PolymarketConnector,
    book_quote,
    build_market,
    parse_json_list,
    poly_status,
    poly_url,
    tokens_by_label,
)

CID = "0x8bf1c1536ecb1c08fe13c6b71e8ab1f58bf3461c4cb79f5f1679f869a06aef86"
YES_TOKEN = "111604417349"
NO_TOKEN = "360150509211"

POLY_MARKET = {
    "conditionId": CID,
    "question": "Will there be no change in Fed interest rates after the July 2026 meeting?",
    "slug": "will-there-be-no-change-in-fed-interest-rates-after-the-july-2026-meeting",
    "clobTokenIds": f'["{YES_TOKEN}", "{NO_TOKEN}"]',  # JSON-encoded string (Gamma quirk)
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.93", "0.07"]',
    "endDate": "2026-07-29T00:00:00Z",
    "closed": False,
    "active": True,
    "resolutionSource": "",
    "events": [{"slug": "fed-decision-in-july-181", "title": "Fed Decision in July?"}],
}

YES_BOOK = {
    "bids": [{"price": "0.92", "size": "80"}, {"price": "0.93", "size": "50"}],
    "asks": [{"price": "0.95", "size": "200"}, {"price": "0.94", "size": "100"}],
    "last_trade_price": "0.935",
}
NO_BOOK = {
    "bids": [{"price": "0.06", "size": "40"}],
    "asks": [{"price": "0.08", "size": "100"}, {"price": "0.07", "size": "300"}],
    "last_trade_price": "0.065",
}


# --- pure normalization ----------------------------------------------------

def test_parse_json_list():
    assert parse_json_list('["a", "b"]') == ["a", "b"]
    assert parse_json_list(["a"]) == ["a"]
    assert parse_json_list(None) == []


def test_status_and_url():
    assert poly_status({"active": True, "closed": False}) == "open"
    assert poly_status({"active": True, "closed": True}) == "closed"
    assert poly_url(POLY_MARKET) == "https://polymarket.com/event/fed-decision-in-july-181"


def test_tokens_by_label():
    assert tokens_by_label(POLY_MARKET) == {"YES": YES_TOKEN, "NO": NO_TOKEN}


def test_build_market():
    m = build_market(POLY_MARKET)
    assert m.venue == "polymarket"
    assert m.market_id == f"polymarket:{CID}"
    assert m.market_type == "binary"
    assert m.status == "open"
    assert m.close_time == datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert m.resolution_source is None  # empty string normalized to None
    assert m.url == "https://polymarket.com/event/fed-decision-in-july-181"
    assert [o.label for o in m.outcomes] == ["YES", "NO"]


def test_book_quote_best_of_book():
    ts = datetime(2026, 6, 16, tzinfo=timezone.utc)
    q = book_quote(f"polymarket:{CID}", "YES", YES_BOOK, ts)
    assert q.outcome_id.endswith(":YES")
    assert q.bid == pytest.approx(0.93) and q.bid_size == pytest.approx(50.0)  # max bid price
    assert q.ask == pytest.approx(0.94) and q.ask_size == pytest.approx(100.0)  # min ask price
    assert q.last == pytest.approx(0.935)


def test_book_quote_empty_side():
    q = book_quote("polymarket:x", "NO", {"bids": [], "asks": []}, datetime.now(tz=timezone.utc))
    assert q.bid is None and q.ask is None and q.last is None


# --- async integration against a mock transport ----------------------------

def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/markets"):
        cids = request.url.params.get_list("condition_ids")
        return httpx.Response(200, json=[POLY_MARKET] if CID in cids else [])
    if path.endswith("/book"):
        tok = request.url.params.get("token_id")
        return httpx.Response(200, json=YES_BOOK if tok == YES_TOKEN else NO_BOOK)
    return httpx.Response(404, json={})


def _mock_connector() -> PolymarketConnector:
    c = PolymarketConnector(category_rates={"economics": 0.05})
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return c


async def test_list_markets():
    c = _mock_connector()
    try:
        markets = await c.list_markets([CID])
        assert [m.market_id for m in markets] == [f"polymarket:{CID}"]
        assert c._tokens[CID] == {"YES": YES_TOKEN, "NO": NO_TOKEN}
    finally:
        await c.aclose()


async def test_poll_quotes_yes_and_no_books():
    c = _mock_connector()
    try:
        quotes = await c.poll_quotes([CID])  # self-resolves tokens, then fetches both books
        asks = {q.outcome_id: q.ask for q in quotes}
        assert asks[f"polymarket:{CID}:YES"] == pytest.approx(0.94)
        assert asks[f"polymarket:{CID}:NO"] == pytest.approx(0.07)
    finally:
        await c.aclose()
