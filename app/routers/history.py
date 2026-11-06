"""Endpoints de lectura: fuentes, último valor, histórico (JSON/CSV/XLSX) e informe mensual en PDF."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from .. import aggregation as agg
from ..aggregation import CandleField, Fill, Interval
from ..config import settings
from ..csv_export import Layout, csv_chunks, csv_rows, export_filename
from ..db import get_db
from ..exports_guard import run_heavy
from ..models import HistoryResponse, LatestPoint, SourceInfo
from ..pdf_report import (
    PDF_MEDIA_TYPE,
    REFERENCE_SOURCE,
    build_monthly_report,
    month_bounds,
    parse_month,
    summarize,
)
from ..series import InvalidQuery, build_series, tick_to_point
from ..sources import REGISTRY
from ..xlsx_export import XLSX_MEDIA_TYPE, build_workbook
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
    name = export_filename(filename, q.interval, ts_from, ts_to, ext="csv")
    return StreamingResponse(
        csv_chunks(rows, sep),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get(
    "/history.xlsx",
    summary="Mismo histórico, como libro de Excel con hoja resumen, una hoja por fuente y gráfico",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Libro .xlsx"}},
)
async def history_xlsx(
    q: Annotated[HistoryQuery, Query()],
    value: CandleField = Query(
        CandleField.close, description="Qué valor de la vela va en cada columna de la hoja Resumen"
    ),
    filename: str | None = None,
):
    ts_from, ts_to, names = q.resolve()
    data = await _load_series(q, names, ts_from, ts_to)
    columns = 4 if q.interval is Interval.raw else 7
    cells = sum(len(points) for points in data.values()) * columns
    if cells > settings.export_max_cells:
        raise InvalidQuery(
            f"El libro tendría unas {cells:,} celdas y el tope es {settings.export_max_cells:,}. "
            "Reduzca el rango, use un intervalo mayor o pida `history.csv`, que no tiene ese límite."
        )
    content = await run_heavy(build_workbook, data, names, q.interval, value)
    name = export_filename(filename, q.interval, ts_from, ts_to, ext="xlsx")
    return Response(
        content, media_type=XLSX_MEDIA_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get(
    "/report.pdf",
    summary="Informe mensual en PDF: resumen por fuente, gráfico de cierres y tabla diaria",
    response_class=Response,
    responses={200: {"content": {PDF_MEDIA_TYPE: {}}, "description": "Informe .pdf"}},
)
async def report_pdf(
    month: str = Query(..., description="Mes a informar, YYYY-MM (hora local). No puede ser futuro."),
    sources: str | None = Query(None, description="Fuentes separadas por coma; vacío = todas"),
    filename: str | None = None,
):
    now = int(time.time())
    first_day = parse_month(month, agg.to_local(now).date())
    names = resolve_sources_or_all(sources)
    ts_from, ts_to = month_bounds(first_day, now)

    def load(name: str) -> list[dict[str, Any]]:
        return build_series(name, ts_from, ts_to, Interval.day, Fill.forward, 0)

    data = {n: await asyncio.to_thread(load, n) for n in names}
    reference = data.get(REFERENCE_SOURCE)
    if reference is None:  # la brecha contra el BCV se calcula aunque no se haya pedido como serie
        reference = await asyncio.to_thread(load, REFERENCE_SOURCE)
    stats = summarize(data, reference)
    content = await run_heavy(build_monthly_report, first_day, data, stats)
    name = export_filename(
        filename or f"ves_report_{first_day:%Y-%m}.pdf", Interval.day, ts_from, ts_to, "pdf"
    )
    return Response(
        content, media_type=PDF_MEDIA_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )
