"""Kalshi connector read-path tests (phase 3).

Normalization is tested with RECORDED API shapes (probed live 2026-06-15) so CI
never touches the network; one integration test exercises the async batch fetch
against an httpx.MockTransport. (fees() lives in test_fees.py.)
"""

from datetime import datetime, timezone

import httpx
import pytest

from scanner.connectors.kalshi import (
    KalshiConnector,
    build_market,
    kalshi_status,
    kalshi_url,
    market_quotes,
    parse_iso,
    parse_price,
    parse_size,
)

# --- recorded fixture (trimmed real market; bid/ask satisfy the complement rule) ---

KALSHI_MARKET = {
    "ticker": "KXSAMPLE-26JUN-T1",
    "event_ticker": "KXSAMPLE-26JUN",
    "market_type": "binary",
    "status": "active",
    "title": "Will the sample resolve YES?",
    "yes_sub_title": "Sample",
    "yes_bid_dollars": "0.4200",
    "yes_ask_dollars": "0.4420",
    "no_bid_dollars": "0.5580",   # 1 - yes_ask
    "no_ask_dollars": "0.5800",   # 1 - yes_bid
    "last_price_dollars": "0.4300",
    "yes_bid_size_fp": "150.00",
    "yes_ask_size_fp": "116.00",
    "close_time": "2026-06-29T12:50:00Z",
    "expected_expiration_time": "2026-06-30T00:00:00Z",
    "rules_primary": "Resolves YES if the sample condition is met.",
    "result": "",
}

KALSHI_MARKET_2 = {**KALSHI_MARKET, "ticker": "KXSAMPLE-26JUN-T2",
                   "yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.1100",
                   "no_bid_dollars": "0.8900", "no_ask_dollars": "0.9000",
                   "last_price_dollars": "0.1050"}


# --- pure helpers ----------------------------------------------------------

def test_parsers():
    assert parse_price("0.4420") == pytest.approx(0.442)
    assert parse_price("") is None and parse_price(None) is None
    assert parse_size("116.00") == pytest.approx(116.0)
    assert parse_size(None) is None
    dt = parse_iso("2026-06-29T12:50:00Z")
    assert dt == datetime(2026, 6, 29, 12, 50, tzinfo=timezone.utc)
    assert parse_iso(None) is None


def test_status_mapping():
    assert kalshi_status({"status": "active"}) == "open"
    assert kalshi_status({"status": "finalized"}) == "resolved"
    assert kalshi_status({"status": "active", "result": "yes"}) == "resolved"
    assert kalshi_status({"status": "closed"}) == "closed"
    assert kalshi_status({"status": "initialized"}) == "closed"


def test_url_best_effort():
    # series-ticker (event ticker before first '-'), lowercased -> the live web path
    assert kalshi_url("KXFEDDECISION-26JUL") == "https://kalshi.com/markets/kxfeddecision"
    assert kalshi_url("KXSAMPLE-26JUN") == "https://kalshi.com/markets/kxsample"
    assert kalshi_url(None) is None


def test_build_market():
    m = build_market(KALSHI_MARKET)
    assert m.venue == "kalshi"
    assert m.market_id == "kalshi:KXSAMPLE-26JUN-T1"
    assert m.market_type == "binary"
    assert m.status == "open"
    assert m.close_time == datetime(2026, 6, 29, 12, 50, tzinfo=timezone.utc)
    assert m.resolution_source == "Resolves YES if the sample condition is met."
    assert m.url == "https://kalshi.com/markets/kxsample"
    assert [o.label for o in m.outcomes] == ["YES", "NO"]
    assert m.outcomes[0].outcome_id == "kalshi:KXSAMPLE-26JUN-T1:YES"


def test_market_quotes_prices_and_sizes():
    ts = datetime(2026, 6, 15, tzinfo=timezone.utc)
    yes, no = market_quotes(KALSHI_MARKET, ts)

    assert yes.outcome_id.endswith(":YES")
    assert (yes.bid, yes.ask) == (pytest.approx(0.42), pytest.approx(0.442))
    assert (yes.bid_size, yes.ask_size) == (pytest.approx(150.0), pytest.approx(116.0))
    assert yes.last == pytest.approx(0.43)

    assert no.outcome_id.endswith(":NO")
    assert (no.bid, no.ask) == (pytest.approx(0.558), pytest.approx(0.58))
    # bids-only book: NO bid == YES ask orders, NO ask == YES bid orders
    assert no.bid_size == pytest.approx(116.0)
    assert no.ask_size == pytest.approx(150.0)
    assert no.last == pytest.approx(0.57)  # 1 - yes last


def test_market_quotes_complement_holds():
    """YES ask + NO bid == 1 (and YES bid + NO ask == 1) — the book's defining identity."""
    yes, no = market_quotes(KALSHI_MARKET, datetime.now(tz=timezone.utc))
    assert yes.ask + no.bid == pytest.approx(1.0)
    assert yes.bid + no.ask == pytest.approx(1.0)


def test_market_quotes_tolerates_missing_fields():
    sparse = {"ticker": "KXX", "yes_ask_dollars": "0.30"}
    yes, no = market_quotes(sparse, datetime.now(tz=timezone.utc))
    assert yes.ask == pytest.approx(0.30)
    assert yes.bid is None and yes.bid_size is None and yes.last is None
    assert no.last is None


# --- async integration against a mock transport ----------------------------

def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/markets"):
        wanted = set((request.url.params.get("tickers") or "").split(","))
        markets = [m for m in (KALSHI_MARKET, KALSHI_MARKET_2) if m["ticker"] in wanted]
        return httpx.Response(200, json={"markets": markets, "cursor": ""})
    return httpx.Response(404, json={"error": f"unrouted {request.url.path}"})


def _mock_connector() -> KalshiConnector:
    c = KalshiConnector()
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return c


async def test_list_markets_batch():
    c = _mock_connector()
    try:
        markets = await c.list_markets(["KXSAMPLE-26JUN-T1", "KXSAMPLE-26JUN-T2"])
        assert {m.market_id for m in markets} == {
            "kalshi:KXSAMPLE-26JUN-T1", "kalshi:KXSAMPLE-26JUN-T2"
        }
        assert all(len(m.outcomes) == 2 for m in markets)
    finally:
        await c.aclose()


async def test_poll_quotes_batch():
    c = _mock_connector()
    try:
        quotes = await c.poll_quotes(["KXSAMPLE-26JUN-T1", "KXSAMPLE-26JUN-T2"])
        priced = {q.outcome_id: q.ask for q in quotes}
        assert priced["kalshi:KXSAMPLE-26JUN-T1:YES"] == pytest.approx(0.442)
        assert priced["kalshi:KXSAMPLE-26JUN-T2:YES"] == pytest.approx(0.11)
        assert len(quotes) == 4  # 2 markets x YES/NO
    finally:
        await c.aclose()
