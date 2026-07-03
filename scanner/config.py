"""Configuration loading: settings.toml (tomllib) + links.yaml (PyYAML).

Pure parsing into typed dataclasses; no I/O side effects beyond reading the files.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import EventLink, Leg

# Repo root = parent of the scanner/ package dir.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DEFAULT_SETTINGS_PATH = CONFIG_DIR / "settings.toml"
DEFAULT_LINKS_PATH = CONFIG_DIR / "links.yaml"


@dataclass(slots=True)
class ScannerConfig:
    poll_interval_seconds: float
    db_path: Path
    log_level: str
    # Quote retention (design doc §9 housekeeping): rows older than
    # `retention_full_hours` are thinned to one per outcome per
    # `retention_bucket_seconds`. 0 bucket seconds disables retention.
    retention_full_hours: float
    retention_bucket_seconds: float


@dataclass(slots=True)
class EdgeConfig:
    risk_free_rate: float


@dataclass(slots=True)
class HarnessConfig:
    """Manifold within-platform arb harness (design doc §7 phase 2)."""

    watch: list[str]  # Manifold market ids/slugs to monitor
    max_stake: float
    cooldown_s: float
    min_net_edge: float


@dataclass(slots=True)
class Settings:
    scanner: ScannerConfig
    edge: EdgeConfig
    manifold_harness: HarnessConfig
    # Per-venue raw config dicts, passed straight to each connector's __init__.
    venues: dict[str, dict]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_SETTINGS_PATH) -> "Settings":
        path = Path(path)
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

        scanner_raw = raw.get("scanner", {})
        db_path = Path(scanner_raw.get("db_path", "data/edge_scanner.db"))
        if not db_path.is_absolute():
            db_path = ROOT / db_path

        mh = raw.get("manifold_harness", {})
        return cls(
            scanner=ScannerConfig(
                poll_interval_seconds=float(scanner_raw.get("poll_interval_seconds", 3)),
                db_path=db_path,
                log_level=str(scanner_raw.get("log_level", "INFO")),
                retention_full_hours=float(scanner_raw.get("retention_full_hours", 48)),
                retention_bucket_seconds=float(
                    scanner_raw.get("retention_bucket_seconds", 300)
                ),
            ),
            edge=EdgeConfig(
                risk_free_rate=float(raw.get("edge", {}).get("risk_free_rate", 0.0)),
            ),
            manifold_harness=HarnessConfig(
                watch=[str(x) for x in mh.get("watch", [])],
                max_stake=float(mh.get("max_stake", 100.0)),
                cooldown_s=float(mh.get("cooldown_seconds", 300.0)),
                min_net_edge=float(mh.get("min_net_edge", 0.0)),
            ),
            venues=raw.get("venues", {}),
        )


def _norm_outcome(raw) -> str:
    """Normalize a leg's buy_outcome.

    YAML 1.1 parses unquoted YES/NO as booleans, so `buy_outcome: YES` arrives as
    True. Coerce bools and case-insensitive yes/no to canonical 'YES'/'NO'; leave any
    other label (multi-outcome option text) untouched.
    """
    if isinstance(raw, bool):
        return "YES" if raw else "NO"
    if str(raw).strip().lower() in ("yes", "no"):
        return str(raw).strip().upper()
    return str(raw)


def load_links(path: Path | str = DEFAULT_LINKS_PATH) -> list[EventLink]:
    """Parse and validate hand-curated event links (design doc §4)."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    events = raw.get("events") or []
    links: list[EventLink] = []
    seen_ids: set[str] = set()
    for entry in events:
        event_id = entry["event_id"]
        if event_id in seen_ids:
            raise ValueError(f"duplicate event_id in links.yaml: {event_id!r}")
        seen_ids.add(event_id)

        legs = [
            Leg(
                venue=leg["venue"],
                venue_market_id=str(leg["venue_market_id"]),
                buy_outcome=_norm_outcome(leg["buy_outcome"]),
            )
            for leg in entry.get("legs", [])
        ]
        if len(legs) != 2:
            # v1 cross-venue edge model is binary, two-leg only (design doc §10).
            raise ValueError(
                f"event {event_id!r} has {len(legs)} legs; v1 supports exactly 2"
            )

        links.append(
            EventLink(
                event_id=event_id,
                legs=legs,
                note=entry.get("note", ""),
                resolution_check=entry.get("resolution_check", "suspect"),
            )
        )
    return links
