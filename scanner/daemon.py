"""Scanner daemon: async poll -> normalize -> compute edges -> write SQLite.

Phase 0 contract (design doc §7): boots, initializes the schema, loads links and
connectors, runs the poll loop, and restarts cleanly. It writes nothing useful yet
because the connector read paths are phase-1/3 seams — the loop catches their
NotImplementedError per venue so one unimplemented (or flaky) venue never stalls
the others (design doc §9 reliability).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import defaultdict

from .config import Settings, load_links
from .connectors.base import build_connectors
from .models import EventLink
from .store import Store

log = logging.getLogger("scanner")

# Exponential backoff bounds for per-venue errors (design doc §9).
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
# Re-sync market/outcome metadata every N cycles to catch status/close changes
# (cheap at v1 volume; ~10 min at a 3s interval).
META_REFRESH_CYCLES = 200


def _venue_market_ids(links: list[EventLink]) -> dict[str, list[str]]:
    """Group the curated venue_market_ids by venue for per-venue polling."""
    by_venue: dict[str, list[str]] = defaultdict(list)
    for link in links:
        for leg in link.legs:
            by_venue[leg.venue].append(leg.venue_market_id)
    return dict(by_venue)


class Scanner:
    def __init__(self, settings: Settings, links: list[EventLink]):
        self.settings = settings
        self.links = links
        self.store = Store(settings.scanner.db_path)
        self.connectors = build_connectors(settings.venues)
        self.targets = _venue_market_ids(links)
        self._stop = asyncio.Event()
        self._backoff: dict[str, float] = defaultdict(lambda: BACKOFF_BASE)
        self._meta_synced: set[str] = set()
        self._cycle_count = 0

    def request_stop(self) -> None:
        self._stop.set()

    async def _sync_metadata(self, venue: str, ids: list[str]) -> None:
        """Upsert market + outcome rows for the curated set (needed before quotes:
        quote.outcome_id has a FK to outcome). Runs once, then every refresh."""
        if venue in self._meta_synced:
            return
        markets = await self.connectors[venue].list_markets(ids)
        for m in markets:
            self.store.upsert_market(m)
            for o in m.outcomes:
                self.store.upsert_outcome(o)
        self._meta_synced.add(venue)
        if markets:
            log.info("%s: synced metadata for %d market(s)", venue, len(markets))

    async def _poll_venue(self, venue: str, ids: list[str]) -> None:
        """Poll one venue. Per-venue try/except so one bad venue can't stall others."""
        connector = self.connectors[venue]
        try:
            await self._sync_metadata(venue, ids)
            quotes = await connector.poll_quotes(ids)
        except NotImplementedError:
            # Phase seam: expected until the venue's read path lands (Kalshi/Polymarket).
            log.debug("%s read path not implemented yet (phase seam)", venue)
            return
        except Exception:  # noqa: BLE001 - isolate venue failures
            backoff = self._backoff[venue]
            log.warning("%s poll failed; backing off %.0fs", venue, backoff, exc_info=True)
            await asyncio.wait_for(self._stop.wait(), timeout=backoff)  # interruptible sleep
            self._backoff[venue] = min(backoff * 2, BACKOFF_MAX)
            return
        self._backoff[venue] = BACKOFF_BASE
        if quotes:
            self.store.insert_quotes(quotes)
            log.debug("%s: wrote %d quote(s)", venue, len(quotes))
        # Edge computation over self.links is wired up in phase 3 (needs all venues live).

    async def _cycle(self) -> None:
        if not self.targets:
            return
        if self._cycle_count and self._cycle_count % META_REFRESH_CYCLES == 0:
            self._meta_synced.clear()  # force a metadata re-sync this cycle
        self._cycle_count += 1
        await asyncio.gather(
            *(self._poll_venue(v, ids) for v, ids in self.targets.items())
        )

    async def run(self) -> None:
        interval = self.settings.scanner.poll_interval_seconds
        log.info(
            "scanner up: %d link(s), venues=%s, interval=%.1fs, db=%s",
            len(self.links),
            ",".join(self.connectors),
            interval,
            self.settings.scanner.db_path,
        )
        if not self.links:
            log.warning("links.yaml is empty — polling nothing (expected in phase 0).")
        try:
            while not self._stop.is_set():
                await self._cycle()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass  # normal cadence tick
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        log.info("scanner shutting down")
        for connector in self.connectors.values():
            aclose = getattr(connector, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    log.debug("error closing %s", connector.venue, exc_info=True)
        self.store.close()


async def _amain() -> None:
    settings = Settings.load()
    logging.basicConfig(
        level=getattr(logging, settings.scanner.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    links = load_links()
    scanner = Scanner(settings, links)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, scanner.request_stop)
        except NotImplementedError:  # pragma: no cover - non-Unix
            pass

    await scanner.run()


def main() -> None:
    """Console entry point (`uv run edge-scanner`)."""
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
