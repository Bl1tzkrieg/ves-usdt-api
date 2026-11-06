"""Construcción de series de precios: interpretación de fechas/rangos y armado de velas o puntos raw."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from . import aggregation as agg
from .aggregation import Fill, Interval
from .config import settings
from .db import MAX_TS, Tick, get_db

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
DEFAULT_RANGE_DAYS = 30


class InvalidQuery(ValueError):
    """Parámetro de consulta inválido; la app lo responde como 422 con el mensaje como `detail`."""


def parse_instant(raw: str | None, *, inclusive_day_end: bool) -> int | None:
    """Acepta 'YYYY-MM-DD', ISO 8601 con hora, o epoch en segundos.

    Una fecha sin hora se interpreta en hora local; con `inclusive_day_end` es inclusiva
    (hasta fin del día). Fuera de (0, MAX_TS) lanza `InvalidQuery`.
    """
    if raw is None or raw == "":
        return None
    out_of_range = InvalidQuery(f"Fecha fuera de rango: {raw!r}. Debe estar entre 1970 y 2100.")
    try:
        ts = _parse_instant_unchecked(raw, inclusive_day_end=inclusive_day_end)
    except OverflowError as e:
        raise out_of_range from e
    if not 0 < ts < MAX_TS:
        raise out_of_range
    return ts


def _parse_instant_unchecked(raw: str, *, inclusive_day_end: bool) -> int:
    if raw.isascii() and raw.isdigit():
        return int(raw)
    try:
        d = date.fromisoformat(raw)
        if inclusive_day_end:
            d = d + timedelta(days=1)
        return agg.local_midnight(d)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidQuery(f"Fecha inválida: {raw!r}. Use YYYY-MM-DD, ISO 8601 o epoch.") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=agg.LOCAL_TZ)
    return int(dt.timestamp())


def resolve_range(start: str | None, end: str | None, days: int | None) -> tuple[int, int]:
    """Rango [inicio, fin) en epoch; por defecto termina ahora y abarca `days` (o 30) días."""
    ts_end = parse_instant(end, inclusive_day_end=True) or int(datetime.now().timestamp())
    ts_start = parse_instant(start, inclusive_day_end=False)
    if ts_start is None:
        ts_start = ts_end - (days or DEFAULT_RANGE_DAYS) * SECONDS_PER_DAY
    if ts_start >= ts_end:
        raise InvalidQuery("`start` debe ser anterior a `end`.")
    if ts_end - ts_start > settings.max_range_days * SECONDS_PER_DAY:
        raise InvalidQuery(f"El rango máximo es de {settings.max_range_days} días.")
    return ts_start, ts_end


def tick_to_point(tick: Tick) -> dict[str, Any]:
    """Punto de serie raw: `t`, `date` (hora local), `price`, `extra`."""
    return {"t": tick.ts, "date": agg.local_iso(tick.ts), "price": tick.price, "extra": tick.extra}


def build_series(
    source: str, ts_from: int, ts_to: int, interval: Interval, fill: Fill, max_points: int
) -> list[dict[str, Any]]:
    """Puntos raw (reducidos a `max_points`) o velas agregadas por `interval` con relleno `fill`."""
    db = get_db()
    if interval is Interval.raw:
        cap = min(max_points or settings.raw_max_points, settings.raw_max_points)
        return [tick_to_point(t) for t in agg.downsample(db.raw(source, ts_from, ts_to), cap)]
    bucket = SECONDS_PER_HOUR if interval is Interval.hour else SECONDS_PER_DAY
    if fill is not Fill.none and (ts_to - ts_from) // bucket > settings.max_fill_buckets:
        raise InvalidQuery(
            f"Rango demasiado grande para `fill={fill.value}`: máximo {settings.max_fill_buckets} cubetas."
        )
    candles = agg.candles_from_buckets(db.buckets(source, ts_from, ts_to, bucket, agg.OFFSET_SECONDS))
    candles = agg.rollup(candles, interval) if interval in (Interval.week, Interval.month) else candles
    seed = None
    if fill is Fill.forward:
        prev = db.last_before(source, ts_from)
        seed = prev.price if prev else None
    candles = agg.fill_gaps(candles, interval, ts_from, ts_to, fill, seed=seed)
    return [c.to_dict() for c in candles]
