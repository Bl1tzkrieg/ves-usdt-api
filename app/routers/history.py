"""Endpoints de lectura: fuentes, último valor e histórico (JSON/CSV)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import aggregation as agg
from ..aggregation import CandleField
from ..config import settings
from ..csv_export import Layout, csv_chunks, csv_rows, export_filename
from ..db import get_db
from ..models import HistoryResponse, LatestPoint, SourceInfo
from ..series import build_series, tick_to_point
from ..sources import REGISTRY
from .common import RATES_PREFIX, RATES_TAGS, HistoryQuery, resolve_sources_or_all

router = APIRouter(prefix=RATES_PREFIX, tags=RATES_TAGS)

TZ_LABEL = f"UTC{settings.tz_offset_hours:+d}"


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
    ticks = await asyncio.to_thread(get_db().latest, resolve_sources_or_all(sources))
    return [LatestPoint(source=t.source, **tick_to_point(t)) for t in ticks]


async def _load_series(
    q: HistoryQuery, names: list[str], ts_from: int, ts_to: int
) -> dict[str, list[dict[str, Any]]]:
    return {
        n: await asyncio.to_thread(build_series, n, ts_from, ts_to, q.interval, q.fill, q.max_points)
        for n in names
    }


@router.get(
    "/history",
    response_model=HistoryResponse,
    summary="Histórico USDT/VES (y series comparativas) en cualquier rango de fechas",
)
async def history(q: Annotated[HistoryQuery, Query()]):
    ts_from, ts_to, names = q.resolve()
    data = await _load_series(q, names, ts_from, ts_to)
    series = [
        {"source": n, "label": REGISTRY[n].label, "unit": REGISTRY[n].unit, "points": points}
        for n, points in data.items()
    ]
    return {
        "start": agg.local_iso(ts_from),
        "end": agg.local_iso(ts_to),
        "interval": q.interval.value,
        "fill": q.fill.value,
        "timezone": TZ_LABEL,
        "series": series,
    }


@router.get("/history.csv", summary="Mismo histórico, como CSV descargable (streaming, sin límite de filas)")
async def history_csv(
    q: Annotated[HistoryQuery, Query()],
    value: CandleField = Query(
        CandleField.close, description="Solo layout=wide: qué valor de la vela usar por columna"
    ),
    layout: Layout = Query(
        "long",
        description="long: una fila por fuente y fecha con OHLC. wide: una columna por fuente.",
    ),
    sep: str = Query(
        ",", min_length=1, max_length=1, description="Separador de columnas (use ';' para Excel en español)"
    ),
    decimal: str | None = Query(
        None,
        min_length=1,
        max_length=1,
        description="Separador decimal. Por defecto ',' si sep es ';', si no '.'",
    ),
    filename: str | None = None,
):
    dec = decimal or ("," if sep == ";" else ".")
    if dec == sep:
        raise HTTPException(422, "`decimal` no puede ser igual a `sep`.")
    ts_from, ts_to, names = q.resolve()
    data = await _load_series(q, names, ts_from, ts_to)
    rows = csv_rows(data, q.interval, layout, value, dec)
    name = export_filename(filename, q.interval, ts_from, ts_to)
    return StreamingResponse(
        csv_chunks(rows, sep),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
