from __future__ import annotations

from .base import RateSource, SourceError
from .binance_p2p import BinanceP2PSource
from .dolarapi import BCVSource, ParaleloSource
from .yadio import YadioSource

REGISTRY: dict[str, RateSource] = {
    s.name: s for s in (BinanceP2PSource(), YadioSource(), BCVSource(), ParaleloSource())
}

# Fuente por defecto para /history cuando no se indica ninguna: USDT/VES de Binance P2P
DEFAULT_SOURCE = BinanceP2PSource.name

__all__ = ["REGISTRY", "DEFAULT_SOURCE", "RateSource", "SourceError"]
