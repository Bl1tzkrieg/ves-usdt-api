"""Endpoints de resumen: `/stats` (variaciones y spread) y `/daily-change`."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from ..db import Database, get_db
from ..models import DailyChange, SourceStats
from ..series import DEFAULT_RANGE_DAYS, InvalidQuery, resolve_range
from ..sources import DEFAULT_SOURCE, REGISTRY
from ..stats import daily_changes, source_stats
from .common import RATES_PREFIX, RATES_TAGS, resolve_sources, resolve_sources_or_all

router = APIRouter(prefix=RATES_PREFIX, tags=RATES_TAGS)

DEFAULT_REFERENCE = "bcv"


def _single_source(raw: str | None, param: str) -> str:
    names = resolve_sources(raw)
    if len(names) != 1:
        raise InvalidQuery(f"`{param}` admite una sola fuente.")
    return names[0]


def _collect_stats(db: Database, names: list[str], reference: str) -> list[dict]:
    latest = {t.source: t for t in db.latest([*names, reference])}
    ref = latest.get(reference)
    out = []
    for name in names:
        tick = latest.get(name)
        if tick is None:
            continue  # sin datos no hay último precio que mostrar
        src = REGISTRY[name]
        out.append({**source_stats(db, tick, ref), "label": src.label, "unit": src.unit})
    return out


@router.get(
    "/stats",
    response_model=list[SourceStats],
    summary="Último precio, variación 24h/7d/30d y spread contra una referencia (BCV por defecto)",
)
async def stats(
    sources: str | None = Query(None, description="Lista separada por comas; vacío = todas"),
    reference: str = Query(DEFAULT_REFERENCE, description="Fuente contra la que se calcula el spread"),
):
    names = resolve_sources_or_all(sources)
    ref = _single_source(reference, "reference")
    return await asyncio.to_thread(_collect_stats, get_db(), names, ref)


@router.get(
    "/daily-change",
    response_model=list[DailyChange],
    summary="Variación diaria cierre-a-cierre (%) de una fuente en los últimos `days` días",
)
async def daily_change(
    source: str = Query(DEFAULT_SOURCE, description="Una sola fuente"),
    days: int = Query(DEFAULT_RANGE_DAYS, ge=1, description="Días hacia atrás desde ahora"),
):
    name = _single_source(source, "source")
    ts_from, ts_to = resolve_range(None, None, days)
    return await asyncio.to_thread(daily_changes, name, ts_from, ts_to)
