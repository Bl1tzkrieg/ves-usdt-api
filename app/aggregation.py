"""Agregación temporal de la serie de precios.

Intervalos soportados: raw, hour, day, week (lunes–domingo), month.
Todo se recorta en la zona horaria local configurada (Venezuela, UTC-4), de modo que
"un día" sea el día venezolano y no el día UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable

from .config import settings
from .db import Tick

LOCAL_TZ = timezone(timedelta(hours=settings.tz_offset_hours))
OFFSET_SECONDS = settings.tz_offset_hours * 3600


class Interval(str, Enum):
    raw = "raw"
    hour = "hour"
    day = "day"
    week = "week"
    month = "month"


class Value(str, Enum):
    close = "close"
    open = "open"
    avg = "avg"
    high = "high"
    low = "low"


class Fill(str, Enum):
    none = "none"       # solo cubetas con datos
    forward = "forward"  # repetir último valor conocido en cubetas vacías (útil para BCV en fines de semana)
    null = "null"        # emitir la cubeta con valores null


@dataclass(slots=True)
class Candle:
    t: int          # inicio de la cubeta, epoch UTC (segundos)
    date: str       # inicio de la cubeta en hora local, ISO 8601
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    avg: float | None
    count: int
    filled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ utilidades de tiempo

def to_local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)


def local_iso(ts: int) -> str:
    return to_local(ts).isoformat()


def local_midnight(d: date) -> int:
    """Epoch UTC de la medianoche local de la fecha `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ).timestamp())


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # lunes


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_period(d: date, interval: Interval) -> date:
    if interval is Interval.day:
        return d + timedelta(days=1)
    if interval is Interval.week:
        return d + timedelta(days=7)
    if interval is Interval.month:
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    raise ValueError(interval)


def period_start(d: date, interval: Interval) -> date:
    if interval is Interval.day:
        return d
    if interval is Interval.week:
        return week_start(d)
    if interval is Interval.month:
        return month_start(d)
    raise ValueError(interval)


# ------------------------------------------------------------------ agregación

def _candle_from_row(row: dict[str, Any]) -> Candle:
    return Candle(
        t=int(row["bucket"]),
        date=local_iso(int(row["bucket"])),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        avg=row["avg"],
        count=int(row["n"]),
    )


def candles_from_buckets(rows: Iterable[dict[str, Any]]) -> list[Candle]:
    return [_candle_from_row(r) for r in rows]


def rollup(daily: list[Candle], interval: Interval) -> list[Candle]:
    """Convierte velas diarias en semanales o mensuales (promedio ponderado por conteo)."""
    if interval is Interval.day or not daily:
        return daily
    out: list[Candle] = []
    current_key: date | None = None
    acc: list[Candle] = []

    def flush() -> None:
        if not acc or current_key is None:
            return
        n = sum(c.count for c in acc)
        wavg = sum((c.avg or 0) * c.count for c in acc) / n if n else None
        start_ts = local_midnight(current_key)
        out.append(
            Candle(
                t=start_ts,
                date=local_iso(start_ts),
                open=acc[0].open,
                high=max(c.high for c in acc if c.high is not None),
                low=min(c.low for c in acc if c.low is not None),
                close=acc[-1].close,
                avg=wavg,
                count=n,
            )
        )

    for c in daily:
        key = period_start(to_local(c.t).date(), interval)
        if key != current_key:
            flush()
            current_key, acc = key, []
        acc.append(c)
    flush()
    return out


def fill_gaps(
    candles: list[Candle], interval: Interval, start_ts: int, end_ts: int, mode: Fill,
    seed: float | None = None,
) -> list[Candle]:
    """Rellena cubetas vacías entre start y end (hora local) según `mode`.

    `seed` es el último precio conocido antes de `start_ts`, para que el relleno hacia adelante
    no empiece vacío (p. ej. BCV si el rango arranca un sábado).
    """
    if mode is Fill.none or interval is Interval.raw:
        return candles
    by_t = {c.t: c for c in candles}
    out: list[Candle] = []
    last: Candle | None = None
    if seed is not None:
        last = Candle(t=0, date="", open=seed, high=seed, low=seed, close=seed, avg=seed, count=0)

    if interval is Interval.hour:
        step = 3600
        t = ((start_ts + OFFSET_SECONDS) // step) * step - OFFSET_SECONDS
        while t < end_ts:
            _emit(out, by_t.get(t), t, last, mode)
            if t in by_t:
                last = by_t[t]
            t += step
        return out

    d = period_start(to_local(start_ts).date(), interval)
    end_d = to_local(end_ts - 1).date()
    while d <= end_d:
        t = local_midnight(d)
        _emit(out, by_t.get(t), t, last, mode)
        if t in by_t:
            last = by_t[t]
        d = next_period(d, interval)
    return out


def _emit(out: list[Candle], found: Candle | None, t: int, last: Candle | None, mode: Fill) -> None:
    if found is not None:
        out.append(found)
    elif mode is Fill.forward and last is not None:
        v = last.close
        out.append(Candle(t=t, date=local_iso(t), open=v, high=v, low=v, close=v, avg=v, count=0, filled=True))
    elif mode is Fill.null:
        out.append(Candle(t=t, date=local_iso(t), open=None, high=None, low=None, close=None, avg=None, count=0, filled=True))
    # Fill.forward sin ningún valor previo conocido: no hay nada que repetir, se omite la cubeta.


def downsample(ticks: list[Tick], max_points: int) -> list[Tick]:
    """Reduce una serie raw a ~max_points conservando el primero y el último (para gráficas)."""
    if max_points <= 0 or len(ticks) <= max_points:
        return ticks
    if max_points == 1:
        return [ticks[-1]]
    # max_points índices equidistantes de 0 a len-1 (ambos incluidos); distintos porque el paso es > 1
    step = (len(ticks) - 1) / (max_points - 1)
    return [ticks[round(i * step)] for i in range(max_points - 1)] + [ticks[-1]]


def pick(candle: Candle, value: Value) -> float | None:
    return getattr(candle, value.value)
