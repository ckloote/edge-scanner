"""Venue connectors: one Protocol, three implementations (design doc §5).

Each connector owns its own auth, rate-limit etiquette, raw->canonical
normalization, and its own fee model.
"""

from .base import Connector, build_connectors, ceil_to
from .kalshi import KalshiConnector
from .manifold import ManifoldConnector
from .polymarket import PolymarketConnector

__all__ = [
    "Connector",
    "ceil_to",
    "build_connectors",
    "ManifoldConnector",
    "KalshiConnector",
    "PolymarketConnector",
]
