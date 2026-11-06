"""Binance P2P — el mercado de referencia real del USDT/VES.

Se consultan los primeros N anuncios de compra y de venta y se toma la mediana de cada lado;
el precio del tick es el punto medio. `extra` guarda compra, venta y cuántos anuncios se usaron.
"""
from __future__ import annotations

import statistics

import httpx

from ..config import settings
from ..db import Tick
from .base import RateSource, SourceError

P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) rates-api/1.0",
}


class BinanceP2PSource(RateSource):
    name = "binance_p2p"
    label = "Binance P2P USDT/VES"
    unit = "USDT"
    description = "Punto medio entre la mediana de anuncios de compra y de venta en Binance P2P."

    async def _side(self, client: httpx.AsyncClient, trade_type: str) -> list[float]:
        payload = {
            "asset": "USDT",
            "fiat": "VES",
            "merchantCheck": False,
            "page": 1,
            "rows": settings.binance_rows,
            "tradeType": trade_type,  # BUY = yo compro USDT (anuncios que venden)
            "payTypes": [],
            "publisherType": None,
        }
        r = await client.post(P2P_URL, json=payload, headers=_HEADERS)
        r.raise_for_status()
        data = r.json().get("data") or []
        prices = [float(a["adv"]["price"]) for a in data if a.get("adv", {}).get("price")]
        if not prices:
            raise SourceError(f"Binance P2P sin anuncios para {trade_type}")
        return prices

    async def fetch(self, client: httpx.AsyncClient) -> Tick:
        buy = await self._side(client, "BUY")
        sell = await self._side(client, "SELL")
        buy_med, sell_med = statistics.median(buy), statistics.median(sell)
        return Tick(
            source=self.name,
            ts=self.now(),
            price=(buy_med + sell_med) / 2,
            extra={"buy": buy_med, "sell": sell_med, "ads_buy": len(buy), "ads_sell": len(sell)},
        )
