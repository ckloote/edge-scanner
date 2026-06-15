"""Phase-0 'done when' check: the scanner boots, the schema is created, a poll
cycle runs without error, and shutdown is clean (design doc §7)."""

from datetime import datetime, timezone

from scanner.config import Settings
from scanner.daemon import Scanner
from scanner.models import Market, Outcome, Quote
from scanner.store import Store


def test_store_creates_schema_and_roundtrips(tmp_path):
    store = Store(tmp_path / "edge.db")
    tables = {
        r["name"]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"market", "outcome", "quote", "edge_snapshot"} <= tables

    m = Market(
        venue="kalshi", venue_market_id="FED-26JUN", title="Fed cut June 2026",
        market_type="binary", status="open", url="https://example.com/fed",
    )
    store.upsert_market(m)
    store.upsert_market(m)  # idempotent: second upsert must not duplicate
    assert len(store.list_markets()) == 1
    assert store.list_markets()[0]["url"] == "https://example.com/fed"

    o = Outcome(market_id=m.market_id, label="YES")
    store.upsert_outcome(o)
    store.insert_quote(
        Quote(ts=datetime.now(timezone.utc), outcome_id=o.outcome_id, bid=0.6, ask=0.62)
    )
    assert len(store.quote_history(o.outcome_id)) == 1
    store.close()


async def test_scanner_boots_and_cycles_with_empty_links(tmp_path):
    settings = Settings.load()
    settings.scanner.db_path = tmp_path / "edge.db"  # don't touch the real db
    scanner = Scanner(settings, links=[])

    # No links -> no poll targets -> a cycle is a clean no-op.
    await scanner._cycle()
    assert set(scanner.connectors) == {"manifold", "kalshi", "polymarket"}

    await scanner._shutdown()  # clean shutdown closes the store
