"""Endpoints de tasas: histórico (JSON/CSV), último valor, stream en vivo, importación."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import date, datetime, timedelta
from typing import AsyncIterator, Iterator

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import aggregation as agg
from ..aggregation import Candle, Fill, Interval, Value
from ..collector import collector
from ..config import settings
from ..db import get_db
from ..models import HistoryResponse, ImportResult, LatestPoint, SourceInfo
from ..sources import DEFAULT_SOURCE, REGISTRY
from ..tick_parsing import PRICE_KEYS, SOURCE_KEYS, TS_KEYS, rows_to_ticks, sniff_dialect

router = APIRouter(prefix="/api/v1/rates", tags=["rates"])

TZ_LABEL = f"UTC{settings.tz_offset_hours:+d}"
IMPORT_REJECTED_SAMPLE = 10
_READ_CHUNK = 1 << 20  # 1 MiB


# ------------------------------------------------------------------ helpers de parámetros

def _parse_when(raw: str | None, *, end: bool) -> int | None:
    """Acepta 'YYYY-MM-DD', ISO 8601 con hora, o epoch en segundos.

    Una fecha sin hora se interpreta en hora local; para `end` es inclusiva (hasta fin del día).
    """
    if raw is None or raw == "":
        return None
    if raw.isdigit():
        return int(raw)
    try:
        d = date.fromisoformat(raw)
        if end:
            d = d + timedelta(days=1)
        return agg.local_midnight(d)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(422, f"Fecha inválida: {raw!r}. Use YYYY-MM-DD, ISO 8601 o epoch.") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=agg.LOCAL_TZ)
    return int(dt.timestamp())


def _resolve_range(start: str | None, end: str | None, days: int | None) -> tuple[int, int]:
    ts_end = _parse_when(end, end=True) or int(datetime.now().timestamp())
    ts_start = _parse_when(start, end=False)
    if ts_start is None:
        ts_start = ts_end - (days or 30) * 86400
    if ts_start >= ts_end:
        raise HTTPException(422, "`start` debe ser anterior a `end`.")
    return ts_start, ts_end


def _resolve_sources(raw: str | None) -> list[str]:
    names = [s.strip() for s in (raw or DEFAULT_SOURCE).split(",") if s.strip()]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise HTTPException(422, f"Fuente(s) desconocida(s): {unknown}. Disponibles: {sorted(REGISTRY)}")
    return names


def _build_series(source: str, ts_from: int, ts_to: int, interval: Interval, fill: Fill, max_points: int) -> list:
    db = get_db()
    if interval is Interval.raw:
        ticks = agg.downsample(db.raw(source, ts_from, ts_to), max_points or settings.raw_max_points)
        return [
            {"t": t.ts, "date": agg.local_iso(t.ts), "price": t.price, "extra": t.extra} for t in ticks
        ]
    bucket = 3600 if interval is Interval.hour else 86400
    candles = agg.candles_from_buckets(db.buckets(source, ts_from, ts_to, bucket, agg.OFFSET_SECONDS))
    candles = agg.rollup(candles, interval) if interval in (Interval.week, Interval.month) else candles
    seed = None
    if fill is Fill.forward:
        prev = db.last_before(source, ts_from)
        seed = prev.price if prev else None
    candles = agg.fill_gaps(candles, interval, ts_from, ts_to, fill, seed=seed)
    return [c.to_dict() for c in candles]


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
    names = _resolve_sources(sources) if sources else list(REGISTRY)
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
    days: int | None = Query(None, ge=1, description="Alternativa a `start`: cantidad de días hacia atrás desde `end`. Sin tope."),
    sources: str | None = Query(None, description=f"Fuentes separadas por coma. Por defecto `{DEFAULT_SOURCE}`. Ej: binance_p2p,bcv"),
    interval: Interval = Query(Interval.day, description="raw | hour | day | week | month"),
    fill: Fill = Query(Fill.none, description="Relleno de cubetas vacías: none | forward | null"),
    max_points: int = Query(0, ge=0, description="Solo interval=raw: reduce a ~N puntos para graficar (0 = todos)"),
):
    ts_from, ts_to = _resolve_range(start, end, days)
    names = _resolve_sources(sources)
    series = []
    for name in names:
        points = await asyncio.to_thread(_build_series, name, ts_from, ts_to, interval, fill, max_points)
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
    value: Value = Query(Value.close, description="Solo layout=wide: qué valor de la vela usar por columna"),
    layout: str = Query("long", pattern="^(long|wide)$", description="long: una fila por fuente y fecha con OHLC. wide: una columna por fuente."),
    max_points: int = Query(0, ge=0),
    sep: str = Query(",", max_length=1, description="Separador de columnas (use ';' para Excel en español)"),
    decimal: str | None = Query(None, max_length=1, description="Separador decimal. Por defecto ',' si sep es ';', si no '.'"),
    filename: str | None = None,
):
    dec = decimal or ("," if sep == ";" else ".")
    if dec == sep:
        raise HTTPException(422, "`decimal` no puede ser igual a `sep`.")
    ts_from, ts_to = _resolve_range(start, end, days)
    names = _resolve_sources(sources)
    data = {
        n: await asyncio.to_thread(_build_series, n, ts_from, ts_to, interval, fill, max_points) for n in names
    }

    def _date(iso: str) -> str:
        return iso if interval in (Interval.hour, Interval.raw) else iso[:10]

    def _num(v: float | None) -> str:
        if v is None:
            return ""
        txt = f"{v:.6f}".rstrip("0").rstrip(".")
        return txt.replace(".", dec) if dec != "." else txt

    def records() -> Iterator[list]:
        if interval is Interval.raw:
            yield ["source", "date", "timestamp", "price", "buy", "sell"]
            for n, pts in data.items():
                for p in pts:
                    ex = p.get("extra") or {}
                    yield [n, p["date"], p["t"], _num(p["price"]), _num(ex.get("buy")), _num(ex.get("sell"))]
        elif layout == "long":
            yield ["source", "date", "timestamp", "open", "high", "low", "close", "avg", "count", "filled"]
            for n, pts in data.items():
                for c in pts:
                    yield [n, _date(c["date"]), c["t"], _num(c["open"]), _num(c["high"]), _num(c["low"]),
                           _num(c["close"]), _num(c["avg"]), c["count"], int(c["filled"])]
        else:  # wide: una columna por fuente, alineadas por inicio de cubeta
            all_t = sorted({c["t"] for pts in data.values() for c in pts})
            by = {n: {c["t"]: c for c in pts} for n, pts in data.items()}
            yield ["date", "timestamp", *names]
            for t in all_t:
                yield [_date(agg.local_iso(t)), t, *[_num(by[n][t][value.value]) if t in by[n] else "" for n in names]]

    def rows(chunk: int = 2000) -> Iterator[str]:
        """Emite el CSV en bloques de `chunk` filas: memoria constante, sin límite de filas."""
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=sep, lineterminator="\n")
        pending = 0
        for rec in records():
            w.writerow(rec)
            pending += 1
            if pending >= chunk:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
                pending = 0
        if pending:
            yield buf.getvalue()

    name = filename or f"ves_rates_{interval.value}_{agg.to_local(ts_from).date()}_{agg.to_local(ts_to - 1).date()}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/stream", summary="Server-Sent Events con el último valor de cada fuente (para gráfica en vivo)")
async def stream(request: Request, sources: str | None = None, every: int = Query(15, ge=2, le=3600)):
    names = _resolve_sources(sources) if sources else list(REGISTRY)

    async def gen() -> AsyncIterator[str]:
        last_sent: dict[str, int] = {}
        while True:
            if await request.is_disconnected():
                break
            ticks = await asyncio.to_thread(get_db().latest, names)
            fresh = [t for t in ticks if last_sent.get(t.source) != t.ts]
            if fresh:
                payload = [
                    {"source": t.source, "t": t.ts, "date": agg.local_iso(t.ts), "price": t.price, "extra": t.extra}
                    for t in fresh
                ]
                yield f"event: rates\ndata: {json.dumps(payload)}\n\n"
                last_sent.update({t.source: t.ts for t in fresh})
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(every)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/collect", summary="Forzar una recolección inmediata de todas las fuentes")
async def collect_now():
    ticks = await collector.collect_once()
    return {"collected": [{"source": t.source, "price": t.price, "date": agg.local_iso(t.ts)} for t in ticks],
            "status": collector.last_status}


@router.get("/collector", summary="Estado del recolector")
async def collector_status():
    return collector.status()


@router.post("/import", response_model=ImportResult, summary="Importar histórico desde CSV")
async def import_csv(
    file: UploadFile,
    source: str | None = Query(None, description="Fuente destino si el CSV no trae columna `source`"),
    replace: bool = Query(False, description="Borrar antes los ticks de la(s) fuente(s) en el rango importado"),
):
    """CSV con cabecera. Columnas aceptadas (insensible a mayúsculas):
    - `date` (YYYY-MM-DD, ISO 8601, DD/MM/YYYY) **o** `timestamp` (epoch en s o ms)
    - `price` (o `close`, `rate`, `promedio`); acepta `1.234,56` y `1,5`
    - `source` (opcional si se pasa `?source=`)
    - `buy`, `sell` (opcionales → se guardan en `extra`)

    Las filas inválidas no abortan la importación: se informan en `rejected`/`rejected_sample`.
    """
    content = await _read_csv_text(file)
    reader = csv.DictReader(io.StringIO(content), dialect=sniff_dialect(content[:2048]))
    if not reader.fieldnames:
        raise HTTPException(422, "CSV vacío o sin cabecera")
    headers = {c.lower().strip() for c in reader.fieldnames if c}
    if not headers & set(TS_KEYS) or not headers & set(PRICE_KEYS):
        raise HTTPException(422, f"Faltan columnas: se requiere (date|timestamp) y price. Cabecera: {reader.fieldnames}")
    if not headers & set(SOURCE_KEYS) and not source:
        raise HTTPException(422, "Indique `?source=` o incluya una columna `source`")
    if source and source not in REGISTRY:
        raise HTTPException(422, f"Fuente desconocida {source!r}. Disponibles: {sorted(REGISTRY)}")

    try:
        parsed = rows_to_ticks(reader, default_source=source, allowed_sources=REGISTRY)
    except csv.Error as e:
        raise HTTPException(422, f"CSV mal formado: {e}") from e

    db = get_db()
    write = db.replace_ticks if replace else db.insert_ticks
    inserted = await asyncio.to_thread(write, parsed.ticks)
    return ImportResult(
        received=len(parsed.ticks) + len(parsed.rejected),
        inserted=inserted,
        duplicates=len(parsed.ticks) - inserted,
        rejected=len(parsed.rejected),
        rejected_sample=parsed.rejected[:IMPORT_REJECTED_SAMPLE],
    )


async def _read_csv_text(file: UploadFile) -> str:
    """Lee el archivo por trozos con tope de tamaño (413) y lo decodifica como UTF-8 (422)."""
    limit = settings.import_max_bytes
    too_large = HTTPException(413, f"El archivo supera el máximo de {limit} bytes")
    if file.size is not None and file.size > limit:
        raise too_large
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise too_large
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(422, "El archivo debe estar codificado en UTF-8") from e
