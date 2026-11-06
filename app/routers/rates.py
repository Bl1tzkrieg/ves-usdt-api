"""Endpoints de tasas: fuentes, último valor e histórico (JSON/CSV)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import aggregation as agg
from ..aggregation import CandleField, Fill, Interval
from ..config import settings
from ..csv_export import csv_chunks, csv_rows, export_filename
from ..db import get_db
from ..models import HistoryResponse, LatestPoint, SourceInfo
from ..series import build_series, resolve_range
from ..sources import DEFAULT_SOURCE, REGISTRY
from .common import resolve_sources, resolve_sources_or_all

router = APIRouter(prefix="/api/v1/rates", tags=["rates"])

TZ_LABEL = f"UTC{settings.tz_offset_hours:+d}"


# ------------------------------------------------------------------ endpoints

@router.get("/sources", response_model=list[SourceInfo])
async def list_sources():
    stats = {s["source"]: s for s in await asyncio.to_thread(get_db().sources)}
    out = []
    for name, src in REGISTRY.items():
        st = stats.get(name)
        out.append(
            SourceInfo(
                name=name,
                label=src.label,
                unit=src.unit,
                description=src.description,
                ticks=st["n"] if st else 0,
                first=agg.local_iso(st["first_ts"]) if st else None,
                last=agg.local_iso(st["last_ts"]) if st else None,
            )
        )
    return out


@router.get("/latest", response_model=list[LatestPoint])
async def latest(sources: str | None = Query(None, description="Lista separada por comas; vacío = todas")):
    names = resolve_sources_or_all(sources)
    ticks = await asyncio.to_thread(get_db().latest, names)
    return [LatestPoint(source=t.source, t=t.ts, date=agg.local_iso(t.ts), price=t.price, extra=t.extra) for t in ticks]


@router.get(
    "/history",
    response_model=HistoryResponse,
    summary="Histórico USDT/VES (y series comparativas) en cualquier rango de fechas",
)
async def history(
    start: str | None = Query(None, description="Inicio: YYYY-MM-DD, ISO 8601 o epoch. Por defecto `end - days`."),
    end: str | None = Query(None, description="Fin (inclusivo si es fecha). Por defecto ahora."),
    days: int | None = Query(None, ge=1, description="Alternativa a `start`: cantidad de días hacia atrás desde `end`. Máximo `max_range_days`."),
    sources: str | None = Query(None, description=f"Fuentes separadas por coma. Por defecto `{DEFAULT_SOURCE}`. Ej: binance_p2p,bcv"),
    interval: Interval = Query(Interval.day, description="raw | hour | day | week | month"),
    fill: Fill = Query(Fill.none, description="Relleno de cubetas vacías: none | forward | null"),
    max_points: int = Query(0, ge=0, description="Solo interval=raw: reduce a ~N puntos (0 = el tope del servidor, `raw_max_points`)"),
):
    ts_from, ts_to = resolve_range(start, end, days)
    names = resolve_sources(sources)
    series = []
    for name in names:
        points = await asyncio.to_thread(build_series, name, ts_from, ts_to, interval, fill, max_points)
        src = REGISTRY[name]
        series.append({"source": name, "label": src.label, "unit": src.unit, "points": points})
    return {
        "start": agg.local_iso(ts_from),
        "end": agg.local_iso(ts_to),
        "interval": interval.value,
        "fill": fill.value,
        "timezone": TZ_LABEL,
        "series": series,
    }


@router.get("/history.csv", summary="Mismo histórico, como CSV descargable (streaming, sin límite de filas)")
async def history_csv(
    start: str | None = None,
    end: str | None = None,
    days: int | None = Query(None, ge=1),
    sources: str | None = None,
    interval: Interval = Interval.day,
    fill: Fill = Fill.none,
    value: CandleField = Query(CandleField.close, description="Solo layout=wide: qué valor de la vela usar por columna"),
    layout: str = Query("long", pattern="^(long|wide)$", description="long: una fila por fuente y fecha con OHLC. wide: una columna por fuente."),
    max_points: int = Query(0, ge=0),
    sep: str = Query(",", min_length=1, max_length=1, description="Separador de columnas (use ';' para Excel en español)"),
    decimal: str | None = Query(None, min_length=1, max_length=1, description="Separador decimal. Por defecto ',' si sep es ';', si no '.'"),
    filename: str | None = None,
):
    dec = decimal or ("," if sep == ";" else ".")
    if dec == sep:
        raise HTTPException(422, "`decimal` no puede ser igual a `sep`.")
    ts_from, ts_to = resolve_range(start, end, days)
    names = resolve_sources(sources)
    data = {
        n: await asyncio.to_thread(build_series, n, ts_from, ts_to, interval, fill, max_points) for n in names
    }
    rows = csv_rows(data, interval, layout, value, dec)
    name = export_filename(filename, interval, ts_from, ts_to)
    return StreamingResponse(
        csv_chunks(rows, sep),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
