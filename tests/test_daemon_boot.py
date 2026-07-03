"""Phase-0 'done when' check: the scanner boots, the schema is created, a poll
cycle runs without error, and shutdown is clean (design doc §7)."""

from datetime import datetime, timedelta, timezone

import pytest

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


def test_thin_quotes_keeps_recent_full_and_one_per_bucket(tmp_path):
    store = Store(tmp_path / "edge.db")
    m = Market(venue="kalshi", venue_market_id="KT", title="t",
               market_type="binary", status="open")
    store.upsert_market(m)
    for label in ("YES", "NO"):
        store.upsert_outcome(Outcome(market_id=m.market_id, label=label))

    base = datetime.fromtimestamp(1_699_999_800, tz=timezone.utc)  # 300s-aligned
    cutoff = base + timedelta(hours=1)
    for label in ("YES", "NO"):
        oid = f"{m.market_id}:{label}"
        # old: two 300s buckets, 10 quotes each at a 30s cadence
        for i in range(20):
            store.insert_quote(Quote(ts=base + timedelta(seconds=30 * i),
                                     outcome_id=oid, ask=0.5))
        # recent (after the cutoff): must keep full resolution
        for i in range(5):
            store.insert_quote(Quote(ts=cutoff + timedelta(seconds=30 * i),
                                     outcome_id=oid, ask=0.5))

    deleted = store.thin_quotes(older_than=cutoff, bucket_seconds=300)
    assert deleted == 2 * (20 - 2)  # per outcome: 20 old rows -> 2 bucket keepers
    for label in ("YES", "NO"):
        rows = store.quote_history(f"{m.market_id}:{label}")
        old = [r for r in rows if r["ts"] < cutoff.isoformat()]
        assert len(old) == 2  # one per 300s bucket, the bucket's FIRST quote
        assert [r["ts"] for r in old] == [
            base.isoformat(), (base + timedelta(seconds=300)).isoformat()]
        assert sum(1 for r in rows if r["ts"] >= cutoff.isoformat()) == 5

    assert store.thin_quotes(older_than=cutoff, bucket_seconds=300) == 0  # idempotent
    assert store.thin_quotes(older_than=cutoff, bucket_seconds=0) == 0  # disabled
    store.close()


async def test_scanner_boots_and_cycles_with_empty_links(tmp_path):
    settings = Settings.load()
    settings.scanner.db_path = tmp_path / "edge.db"  # don't touch the real db
    settings.manifold_harness.watch = []  # keep the cycle offline (no harness network)
    scanner = Scanner(settings, links=[])

    # No links + no harness -> a cycle is a clean no-op.
    await scanner._cycle()
    assert set(scanner.connectors) == {"manifold", "kalshi", "polymarket"}

    await scanner._shutdown()  # clean shutdown closes the store


async def test_poll_venue_failure_backs_off_without_raising(tmp_path):
    """A venue failure must back off and return, never propagate (design doc §9).

    Regression: the backoff sleep (`asyncio.wait_for` on the stop event) raises
    TimeoutError when it elapses; uncaught, it escaped the venue's except block and
    killed the whole daemon on every poll failure."""
    settings = Settings.load()
    settings.scanner.db_path = tmp_path / "edge.db"
    settings.manifold_harness.watch = []
    scanner = Scanner(settings, links=[])

    async def boom(ids):
        raise RuntimeError("venue down")

    scanner.connectors["kalshi"].list_markets = boom  # fail before any network I/O
    scanner._backoff["kalshi"] = 0.01  # keep the backoff sleep test-fast

    await scanner._poll_venue("kalshi", ["SOME-TICKER"])  # must not raise
    assert scanner._backoff["kalshi"] == pytest.approx(0.02)  # and must escalate

    await scanner._shutdown()
