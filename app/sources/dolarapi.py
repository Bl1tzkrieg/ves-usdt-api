"""DolarApi (ve.dolarapi.com) — tasa oficial BCV y promedio paralelo.

GET https://ve.dolarapi.com/v1/dolares/oficial
    {"moneda":"USD","fuente":"oficial","promedio":832.4883,"fechaActualizacion":"2026-09-11T00:00:00-04:00"}
"""
from __future__ import annotations

from datetime import datetime

import httpx

from ..aggregation import LOCAL_TZ
from ..db import Tick
from .base import RateSource, SourceError

BASE = "https://ve.dolarapi.com/v1/dolares/"


class _DolarApiSource(RateSource):
    slug: str = ""

    async def fetch(self, client: httpx.AsyncClient) -> Tick:
        r = await client.get(BASE + self.slug)
        r.raise_for_status()
        data = r.json()
        price = data.get("promedio")
        if price is None:
            raise SourceError(f"DolarApi/{self.slug} sin 'promedio': {str(data)[:200]}")
        # Usamos la fecha de actualización de la fuente: el BCV publica una tasa por día hábil,
        # así que el UNIQUE(source, ts) evita duplicar el mismo valor cada 5 minutos.
        # Una fecha ausente o inválida es un error: caer a now() duplicaría filas cada 5 minutos.
        raw = data.get("fechaActualizacion")
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as e:
            raise SourceError(f"DolarApi/{self.slug} con 'fechaActualizacion' inválida: {str(raw)[:200]!r}") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        ts = int(dt.timestamp())
        return Tick(source=self.name, ts=ts, price=float(price), extra={"fecha": raw})


class BCVSource(_DolarApiSource):
    name = "bcv"
    slug = "oficial"
    label = "BCV oficial USD/VES"
    unit = "USD"
    description = "Tasa oficial del Banco Central de Venezuela (vía DolarApi)."


class ParaleloSource(_DolarApiSource):
    name = "paralelo"
    slug = "paralelo"
    label = "Paralelo promedio USD/VES"
    unit = "USD"
    description = "Promedio de monitores del dólar paralelo (vía DolarApi)."
