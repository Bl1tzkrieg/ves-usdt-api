"""Yadio.io — tasa de mercado libre VES/USD, derivada de Binance P2P (USDT).

GET https://api.yadio.io/rate/VES/USD  ->  {"rate": 938.9177, "timestamp": 1788954013210, ...}
"""
from __future__ import annotations

import httpx

from ..db import Tick
from .base import RateSource, SourceError

URL = "https://api.yadio.io/rate/VES/USD"


class YadioSource(RateSource):
    name = "yadio"
    label = "Yadio USDT/VES"
    unit = "USDT"
    description = "Tasa de mercado libre publicada por Yadio (basada en Binance P2P)."

    async def fetch(self, client: httpx.AsyncClient) -> Tick:
        r = await client.get(URL)
        r.raise_for_status()
        data = r.json()
        if "rate" not in data:
            raise SourceError(f"Yadio respondió sin 'rate': {str(data)[:200]}")
        # Yadio devuelve el timestamp de su última actualización en milisegundos
        ts = int(data.get("timestamp", self.now() * 1000)) // 1000
        return Tick(source=self.name, ts=ts, price=float(data["rate"]))
