"""Curation assistant: suggest near-dated Kalshi<->Polymarket link candidates.

    uv run python scripts/curate.py                    # 45-day horizon, top 20
    uv run python scripts/curate.py --days 30 --top 10
    uv run python scripts/curate.py --min-score 0.6 --min-volume 100000

Enumerates open binary markets on both venues (Kalshi public /markets, ordered
by close time; Polymarket Gamma, ordered by volume), fuzzy-matches titles gated
by resolution-date proximity, and prints ranked candidates with a ready-to-paste
links.yaml stanza. Pairs already curated in links.yaml are skipped.

THIS TOOL DOES NOT WRITE links.yaml. Automated semantic matching is banned for
v1 (design doc §7/§9) because a wrong match poisons the study. Every stanza is
emitted with `resolution_check: suspect`; you verify BOTH markets resolve on the
same criteria and date, then flip it to confirmed-equivalent by hand.

Manifold is out of scope: study links are real-money Kalshi<->Polymarket pairs.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import httpx

from scanner.config import load_links
from scanner.connectors.kalshi import BASE_URL as KALSHI_URL
from scanner.connectors.kalshi import parse_iso, parse_price
from scanner.connectors.polymarket import GAMMA_URL, parse_json_list, poly_url
from scanner.curation import (
    DEFAULT_DATE_TOL_DAYS,
    DEFAULT_MIN_SCORE,
    CandidateMarket,
    match_candidates,
    yaml_stanza,
)

KALSHI_EVENT_PAGES = 40  # ~7k open events @ 200/page (probed 2026-07-02, ~1 min)
POLY_PAGE_CAP = 25


def fetch_kalshi(client: httpx.Client, horizon_days: float) -> list[CandidateMarket]:
    """Open Kalshi binaries expiring within the horizon, with a live YES bid.

    Enumerates /events?with_nested_markets=true rather than /markets: the flat
    market list is 40k+ rows of hourly/parlay noise, and bounding it by
    close_time drops `can_close_early` markets whose close_time is a far-future
    placeholder (the World Cup series closes "2028"). Expiration is filtered
    client-side per nested market instead. The matching title appends
    yes_sub_title so per-outcome tokens (the team/person name) survive
    event-level phrasing; token sets dedupe any repetition.

    Volume/open-interest fields are null on the public feed (probed
    2026-07-02), so "has any YES bid" is the liveness filter. Comma-joined
    parlay titles never map to one Polymarket question and are skipped.
    """
    now = datetime.now(tz=timezone.utc)
    horizon = _horizon_end(horizon_days)
    out: list[CandidateMarket] = []
    cursor = None
    for _ in range(KALSHI_EVENT_PAGES):
        params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{KALSHI_URL}/events", params=params)
        r.raise_for_status()
        data = r.json()
        for ev in data.get("events", []):
            for m in ev.get("markets") or []:
                title = m.get("title") or ev.get("title") or ""
                if ",yes " in title.lower() or ",no " in title.lower():
                    continue  # parlay
                resolution = parse_iso(
                    m.get("expected_expiration_time") or m.get("close_time")
                )
                if resolution is None or not (now < resolution <= horizon):
                    continue
                if not parse_price(m.get("yes_bid_dollars")):
                    continue  # no bid at all -> dead book, not worth proposing
                sub = m.get("yes_sub_title") or ""
                out.append(
                    CandidateMarket(
                        venue="kalshi",
                        venue_market_id=m["ticker"],
                        title=f"{title} {sub}".strip(),
                        resolution=resolution,
                        yes_price=parse_price(m.get("yes_ask_dollars")),
                    )
                )
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def fetch_polymarket(
    client: httpx.Client, horizon_days: float, min_volume: float
) -> list[CandidateMarket]:
    """Open Polymarket binaries ending within the horizon, volume-desc until
    `min_volume` — Gamma's volume ordering is the quality anchor for the pair.
    The horizon is applied server-side (end_date_min/max, probed 2026-07-02) so
    the offset-capped page budget is spent entirely on near-dated markets."""
    now = datetime.now(tz=timezone.utc)
    out: list[CandidateMarket] = []
    for page in range(POLY_PAGE_CAP):
        r = client.get(
            f"{GAMMA_URL}/markets",
            params={
                "closed": "false", "active": "true", "limit": 500,
                "offset": page * 500, "order": "volumeNum", "ascending": "false",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": _horizon_end(horizon_days).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        if r.status_code == 422:  # Gamma caps the pagination offset (probed: ~2500)
            break
        r.raise_for_status()
        markets = r.json()
        if not markets:
            break
        for m in markets:
            volume = float(m.get("volumeNum") or 0.0)
            if volume < min_volume:
                return out  # volume-sorted: everything after is smaller
            outcomes = [o.upper() for o in parse_json_list(m.get("outcomes"))]
            if outcomes != ["YES", "NO"]:
                continue
            resolution = parse_iso(m.get("endDate"))
            if resolution is None or resolution > _horizon_end(horizon_days):
                continue
            prices = parse_json_list(m.get("outcomePrices"))
            out.append(
                CandidateMarket(
                    venue="polymarket",
                    venue_market_id=m["conditionId"],
                    title=m.get("question", ""),
                    resolution=resolution,
                    yes_price=float(prices[0]) if prices else None,
                    volume=volume,
                    url=poly_url(m),
                )
            )
    return out


def _horizon_end(days: float) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(days=days)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--days", type=float, default=45,
                    help="horizon: only markets resolving within this many days (default 45)")
    ap.add_argument("--top", type=int, default=20, help="max candidates to print (default 20)")
    ap.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                    help=f"title-similarity floor in [0,1] (default {DEFAULT_MIN_SCORE})")
    ap.add_argument("--min-volume", type=float, default=50_000,
                    help="Polymarket volume floor in $ (default 50k)")
    ap.add_argument("--date-tol", type=float, default=DEFAULT_DATE_TOL_DAYS,
                    help=f"max resolution-date mismatch in days (default {DEFAULT_DATE_TOL_DAYS:.0f})")
    args = ap.parse_args()

    already = {
        (leg.venue, leg.venue_market_id)
        for link in load_links()
        for leg in link.legs
    }

    with httpx.Client(timeout=30.0) as client:
        kalshi = fetch_kalshi(client, args.days)
        poly = fetch_polymarket(client, args.days, args.min_volume)
    print(f"fetched {len(kalshi)} kalshi + {len(poly)} polymarket open binaries "
          f"resolving within {args.days:.0f} days (poly volume >= ${args.min_volume:,.0f}); "
          f"{len(already)} venue ids already linked\n")

    matches = match_candidates(
        kalshi, poly,
        min_score=args.min_score, date_tol_days=args.date_tol, exclude=already,
    )
    if not matches:
        print("no candidates above the similarity floor — try --min-score 0.4 or a longer --days")
        return

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    for i, m in enumerate(matches[: args.top], 1):
        div = f"{m.divergence:.3f}" if m.divergence is not None else "?"
        vol = f"${m.b.volume:,.0f}" if m.b.volume else "?"
        print(f"#{i}  score {m.score:.2f} | YES {m.a.yes_price} vs {m.b.yes_price} "
              f"(divergence {div}) | poly volume {vol}")
        print(f"    kalshi: {m.a.venue_market_id} — {m.a.title}")
        print(f"            resolves ~{m.a.resolution:%Y-%m-%d}")
        print(f"    poly:   {m.b.title}")
        print(f"            resolves ~{m.b.resolution:%Y-%m-%d}  {m.b.url or ''}")
        print(yaml_stanza(m, today=today))

    print("paste chosen stanzas into config/links.yaml, VERIFY resolution equivalence,")
    print("flip resolution_check to confirmed-equivalent, then restart the scanner:")
    print("  sudo systemctl restart edge-scanner")


if __name__ == "__main__":
    main()
