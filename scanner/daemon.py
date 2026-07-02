"""Scanner daemon: async poll -> normalize -> compute edges -> write SQLite.

Each cycle: sync metadata (once/periodic), poll every venue's curated set with
per-venue isolation (one bad or unimplemented venue never stalls the others — design
doc §9), then compute the §6 cross-venue edge for each linked event from the latest
quotes and persist an `edge_snapshot`. Boots and restarts cleanly under systemd.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import defaultdict
from datetime import datetime, timezone

from .arb import detect
from .config import Settings, load_links
from .connectors.base import build_connectors
from .edge import EdgeInputs, compute_edge, days_between
from .models import EdgeSnapshot, EventLink, make_market_id, make_outcome_id
from .paper import PaperExecutor
from .store import Store

# Resolution timestamps within this window are treated as the same event for the
# basis-risk check (venues timestamp the same resolution slightly differently — e.g.
# a World Cup final dated 07-19 on one venue and 07-20 on the other). Materially
# different dates still flag.
BASIS_TIME_TOLERANCE_S = 172800.0  # 2 days

# For a binary link, both the encoded pair (A.YES + B.NO) and its mirror
# (A.NO + B.YES) pay $1 at resolution, so an arb can exist in either direction.
_COMPLEMENT = {"YES": "NO", "NO": "YES"}

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
        self._stop = asyncio.Event()
        self._retired: set[str] = set()  # event_ids logged as retired (log-once)
        self._backoff: dict[str, float] = defaultdict(lambda: BACKOFF_BASE)
        self._meta_synced: set[str] = set()
        self._cycle_count = 0
        mh = settings.manifold_harness
        self.harness_watch = mh.watch
        self.paper = PaperExecutor(
            self.store, max_stake=mh.max_stake, cooldown_s=mh.cooldown_s,
            min_net_edge=mh.min_net_edge,
        )

    def request_stop(self) -> None:
        self._stop.set()

    def _live_links(self) -> list[EventLink]:
        """Links whose markets are all unresolved — the auto-retire guard.

        A resolved leg ends the pair: the outcome is known, so any residual
        "edge" against the other leg is junk (a resolved Kalshi market keeps
        quoting $1.00 asks at zero size, which reads as a plausible small
        negative edge). Retired links stop being polled and computed; their
        history stays in the DB. Status comes from the market table, refreshed
        by the periodic metadata re-sync; markets not yet synced count as live."""
        live: list[EventLink] = []
        for link in self.links:
            resolved = None
            for leg in link.legs:
                row = self.store.get_market(make_market_id(leg.venue, leg.venue_market_id))
                if row is not None and row["status"] == "resolved":
                    resolved = leg
                    break
            if resolved is None:
                live.append(link)
            elif link.event_id not in self._retired:
                self._retired.add(link.event_id)
                log.info(
                    "link %s retired: %s:%s is resolved — stopping its polling and edges",
                    link.event_id, resolved.venue, resolved.venue_market_id,
                )
        return live

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
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)  # interruptible sleep
            except asyncio.TimeoutError:
                pass  # backoff elapsed without a stop request — resume next cycle
            self._backoff[venue] = min(backoff * 2, BACKOFF_MAX)
            return
        self._backoff[venue] = BACKOFF_BASE
        if quotes:
            self.store.insert_quotes(quotes)
            log.debug("%s: wrote %d quote(s)", venue, len(quotes))
        # Edge computation over self.links is wired up in phase 3 (needs all venues live).

    async def _cycle(self) -> None:
        if self._cycle_count and self._cycle_count % META_REFRESH_CYCLES == 0:
            self._meta_synced.clear()  # force a metadata re-sync this cycle
        self._cycle_count += 1
        live = self._live_links()
        targets = _venue_market_ids(live)
        if targets:
            await asyncio.gather(
                *(self._poll_venue(v, ids) for v, ids in targets.items())
            )
            try:
                self._compute_edges(live)
            except Exception:  # noqa: BLE001 - never let edge math kill the poll loop
                log.warning("edge computation failed this cycle", exc_info=True)
        try:
            await self._run_manifold_harness()
        except Exception:  # noqa: BLE001 - harness must never stall the loop
            log.warning("manifold harness failed this cycle", exc_info=True)

    async def _run_manifold_harness(self) -> None:
        """Phase-2 harness: detect within-platform arb on watched Manifold markets and
        paper-execute with fake money. Per-market isolation; zero real-money risk."""
        if not self.harness_watch:
            return
        conn = self.connectors["manifold"]
        now = datetime.now(tz=timezone.utc)
        for vmid in self.harness_watch:
            try:
                result = await conn.arb_quotes(vmid)
                if not result:
                    continue
                kind, legs = result
                arb = detect(make_market_id("manifold", vmid), kind, legs, conn.fees)
                if arb:
                    self.paper.execute(arb, now)
            except Exception:  # noqa: BLE001 - isolate per-market failures
                log.debug("manifold harness error for %s", vmid, exc_info=True)

    # --- edge engine (design doc §6) ------------------------------------

    def _days_to_resolution(self, market_row, now: datetime) -> float:
        if market_row is None:
            return 0.0
        rt = market_row["resolution_time"] or market_row["close_time"]
        return days_between(now, datetime.fromisoformat(rt)) if rt else 0.0

    @staticmethod
    def _basis_flag(link: EventLink, mkt_a, mkt_b) -> int:
        """1 when the legs may NOT resolve identically (design doc §6).

        A 'suspect' link flags; so does a resolution-time mismatch. Free-text
        resolution_source is NOT compared for equality — across venues it virtually
        always differs, which would pin the flag to 1 and make it useless; the curator's
        `resolution_check` is the source-equivalence signal instead.
        """
        if link.is_suspect:
            return 1
        ra = mkt_a["resolution_time"] if mkt_a else None
        rb = mkt_b["resolution_time"] if mkt_b else None
        if ra and rb:
            delta = abs(
                (datetime.fromisoformat(ra) - datetime.fromisoformat(rb)).total_seconds()
            )
            if delta > BASIS_TIME_TOLERANCE_S:
                return 1
        return 0

    def _eval_direction(self, leg_a, leg_b, mid_a, mid_b, out_a, out_b, days, basis, rate):
        """Evaluate one arb direction (buy out_a on A + out_b on B). None if either
        leg lacks a tradable ask. Returns (oid_a, oid_b, EdgeResult)."""
        oid_a = make_outcome_id(mid_a, out_a)
        oid_b = make_outcome_id(mid_b, out_b)
        qa = self.store.latest_quote(oid_a)
        qb = self.store.latest_quote(oid_b)
        if not qa or not qb or qa["ask"] is None or qb["ask"] is None:
            return None
        inp = EdgeInputs(
            ask_a=qa["ask"],
            ask_b=qb["ask"],
            ask_size_a=qa["ask_size"] or 0.0,
            ask_size_b=qb["ask_size"] or 0.0,
            fee_a=self.connectors[leg_a.venue].fees(qa["ask"], 1.0, "taker"),
            fee_b=self.connectors[leg_b.venue].fees(qb["ask"], 1.0, "taker"),
            days_to_resolution=days,
            basis_risk_flag=basis,
        )
        return oid_a, oid_b, compute_edge(inp, rate)

    def _compute_edges(self, links: list[EventLink] | None = None) -> None:
        """For each linked event, evaluate BOTH arb directions from the latest quotes
        and persist the more profitable one (design doc §6, direction-agnostic).

        The mirror direction (buy the complement outcome on each leg) is just as valid
        an arb for a binary equivalence, so a divergence is captured whichever way it
        leans — what the §1 frequency/duration question actually needs. `links`
        defaults to the live (unretired) set."""
        if links is None:
            links = self._live_links()
        now = datetime.now(tz=timezone.utc)
        rate = self.settings.edge.risk_free_rate
        for link in links:
            leg_a, leg_b = link.legs
            mid_a = make_market_id(leg_a.venue, leg_a.venue_market_id)
            mid_b = make_market_id(leg_b.venue, leg_b.venue_market_id)
            mkt_a = self.store.get_market(mid_a)
            mkt_b = self.store.get_market(mid_b)
            days = max(self._days_to_resolution(mkt_a, now), self._days_to_resolution(mkt_b, now))
            basis = self._basis_flag(link, mkt_a, mkt_b)

            directions = [(leg_a.buy_outcome, leg_b.buy_outcome)]
            comp_a, comp_b = _COMPLEMENT.get(leg_a.buy_outcome), _COMPLEMENT.get(leg_b.buy_outcome)
            if comp_a and comp_b:  # binary -> the mirror direction is also a valid arb
                directions.append((comp_a, comp_b))

            best = None  # (oid_a, oid_b, EdgeResult) with the highest net edge
            for out_a, out_b in directions:
                cand = self._eval_direction(leg_a, leg_b, mid_a, mid_b, out_a, out_b, days, basis, rate)
                if cand and (best is None or cand[2].net_edge > best[2].net_edge):
                    best = cand
            if best is None:
                continue  # need a tradable ask on both legs in at least one direction

            oid_a, oid_b, r = best
            self.store.insert_edge_snapshot(
                EdgeSnapshot(
                    ts=now,
                    event_id=link.event_id,
                    leg_a_outcome_id=oid_a,
                    leg_b_outcome_id=oid_b,
                    gross_edge=r.gross_edge,
                    modeled_fees=r.modeled_fees,
                    lockup_cost=r.lockup_cost,
                    net_edge=r.net_edge,
                    executable_size=r.executable_size,
                    days_to_resolution=r.days_to_resolution,
                    basis_risk_flag=r.basis_risk_flag,
                )
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
