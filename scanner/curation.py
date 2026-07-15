"""Curation-assistant matching: rank candidate cross-venue market pairs.

Pure functions over normalized candidate lists; scripts/curate.py is the CLI
that feeds them from the venue enumeration endpoints. This ASSISTS the human
curator — it never writes links.yaml. Automated semantic matching is banned for
v1 (design doc §7, §9): a wrong match manufactures fake edges that poison the
study, so a human must verify resolution equivalence on every suggested pair.

Matching is title similarity (token Jaccard + normalized-string ratio) gated by
resolution-date proximity, with an inverted token index for blocking so a few
thousand markets per venue stays fast on a Pi.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

# Words too common in market titles to carry matching signal.
STOPWORDS = frozenset(
    "a an and at be by for if in of on or the to vs will win wins won"
    " what which who yes no market question".split()
)

DEFAULT_MIN_SCORE = 0.5
DEFAULT_DATE_TOL_DAYS = 5.0
MIN_SHARED_TOKENS = 2  # blocking: only score pairs sharing at least this many tokens


def tokens(title: str) -> frozenset[str]:
    """Informative lowercase tokens of a market title (stopwords/1-char dropped)."""
    return frozenset(
        t for t in re.findall(r"[a-z0-9]+", title.lower())
        if len(t) > 1 and t not in STOPWORDS
    )


def title_score(a: str, b: str) -> float:
    """Similarity in [0,1]: mean of token Jaccard and sorted-token-string ratio.

    Jaccard catches reworded titles with shared vocabulary; the SequenceMatcher
    ratio (over sorted tokens, so word order doesn't matter) softens Jaccard's
    harshness on titles of very different lengths."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return (jaccard + ratio) / 2.0


@dataclass(slots=True)
class CandidateMarket:
    """One open venue market, normalized for matching."""

    venue: str
    venue_market_id: str
    title: str
    resolution: datetime | None  # expected resolution/end time
    yes_price: float | None = None  # current YES ask/price, for the divergence hint
    volume: float | None = None
    url: str | None = None


@dataclass(slots=True)
class MatchResult:
    a: CandidateMarket  # kalshi side
    b: CandidateMarket  # polymarket side
    score: float

    @property
    def divergence(self) -> float | None:
        """|YES price difference| across venues — the "worth watching" hint."""
        if self.a.yes_price is None or self.b.yes_price is None:
            return None
        return abs(self.a.yes_price - self.b.yes_price)


def _dates_compatible(a: CandidateMarket, b: CandidateMarket, tol_days: float) -> bool:
    """Both resolutions known and within tolerance. Unknown dates fail the gate:
    the assistant only proposes pairs it can sanity-check."""
    if a.resolution is None or b.resolution is None:
        return False
    return abs((a.resolution - b.resolution).total_seconds()) <= tol_days * 86400.0


def _price_sane(m: CandidateMarket, band: float) -> bool:
    """YES price inside [band, 1-band] — an effectively-decided market (an
    eliminated team's prop trades at ~0 or ~1) can't yield a real two-sided
    edge, just lockup on a foregone conclusion. Unknown prices pass: missing
    data isn't evidence of a dead market."""
    if band <= 0 or m.yes_price is None:
        return True
    return band <= m.yes_price <= 1.0 - band


def match_candidates(
    kalshi: list[CandidateMarket],
    poly: list[CandidateMarket],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    date_tol_days: float = DEFAULT_DATE_TOL_DAYS,
    exclude: set[tuple[str, str]] | None = None,
    price_band: float = 0.0,
) -> list[MatchResult]:
    """Rank candidate (kalshi, polymarket) pairs by title similarity.

    `exclude` is a set of (venue, venue_market_id) already curated in links.yaml
    — any pair touching one is skipped. `price_band` > 0 drops candidates whose
    YES price on either venue is outside [band, 1-band] (see _price_sane).
    Returns the best polymarket match per kalshi market (a market shouldn't
    seed two links), sorted by score desc.
    """
    exclude = exclude or set()
    kalshi = [k for k in kalshi if _price_sane(k, price_band)]
    poly = [
        p for p in poly
        if (p.venue, p.venue_market_id) not in exclude and _price_sane(p, price_band)
    ]
    index: dict[str, list[int]] = {}
    for i, p in enumerate(poly):
        for t in tokens(p.title):
            index.setdefault(t, []).append(i)

    out: list[MatchResult] = []
    for k in kalshi:
        if (k.venue, k.venue_market_id) in exclude:
            continue
        shared = Counter()
        for t in tokens(k.title):
            shared.update(index.get(t, ()))
        best: MatchResult | None = None
        for i, n in shared.items():
            if n < MIN_SHARED_TOKENS:
                continue
            p = poly[i]
            if not _dates_compatible(k, p, date_tol_days):
                continue
            score = title_score(k.title, p.title)
            if score >= min_score and (best is None or score > best.score):
                best = MatchResult(a=k, b=p, score=score)
        if best is not None:
            out.append(best)
    out.sort(key=lambda m: -m.score)
    return out


def yaml_stanza(m: MatchResult, *, today: str) -> str:
    """A ready-to-paste links.yaml entry for a suggested pair.

    Ships as `resolution_check: suspect` ON PURPOSE: the loader treats suspect
    as basis-flagged, so a pasted-but-unverified link cannot pollute the clean
    edge set. The curator flips it to confirmed-equivalent only after verifying
    both markets resolve on the same criteria and date."""
    event_id = re.sub(r"[^a-z0-9-]+", "-", m.a.venue_market_id.lower()).strip("-")
    return (
        f"  - event_id: {event_id}   # EDIT: rename; verify resolution equivalence\n"
        f'    note: "{m.a.title} == {m.b.title} (suggested {today}; VERIFY before trusting)"\n'
        f"    resolution_check: suspect   # flip to confirmed-equivalent after verifying\n"
        f"    legs:   # encoding only — the daemon evaluates BOTH directions each cycle\n"
        f'      - {{venue: kalshi, venue_market_id: {m.a.venue_market_id}, buy_outcome: "YES"}}\n'
        f'      - {{venue: polymarket, venue_market_id: "{m.b.venue_market_id}", buy_outcome: "NO"}}\n'
    )
