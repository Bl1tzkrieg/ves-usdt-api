"""Exportación CSV de series: filas raw/long/wide, formato numérico y emisión por bloques."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal

from . import aggregation as agg
from .aggregation import CandleField, Interval

Series = Mapping[str, Sequence[dict[str, Any]]]  # fuente -> puntos (salida de `series.build_series`)

Layout = Literal["long", "wide"]

FILENAME_MAX_LEN = 120


def format_number(v: float | None, decimal_sep: str) -> str:
    """Hasta 6 decimales sin ceros sobrantes; `None` ⇒ celda vacía."""
    if v is None:
        return ""
    txt = f"{v:.6f}".rstrip("0").rstrip(".")
    return txt.replace(".", decimal_sep) if decimal_sep != "." else txt


def _row_date(iso: str, interval: Interval) -> str:
    """Hora completa para raw/hour; solo la fecha para day/week/month."""
    return iso if interval in (Interval.hour, Interval.raw) else iso[:10]


def raw_rows(data: Series, decimal_sep: str) -> Iterator[list]:
    yield ["source", "date", "timestamp", "price", "buy", "sell"]
    for source, points in data.items():
        for p in points:
            ex = p.get("extra") or {}
            yield [
                source,
                p["date"],
                p["t"],
                format_number(p["price"], decimal_sep),
                format_number(ex.get("buy"), decimal_sep),
                format_number(ex.get("sell"), decimal_sep),
            ]


def long_rows(data: Series, interval: Interval, decimal_sep: str) -> Iterator[list]:
    """Una fila por fuente y cubeta, con OHLC."""
    yield ["source", "date", "timestamp", "open", "high", "low", "close", "avg", "count", "filled"]
    for source, candles in data.items():
        for c in candles:
            yield [
                source,
                _row_date(c["date"], interval),
                c["t"],
                *(format_number(c[k], decimal_sep) for k in ("open", "high", "low", "close", "avg")),
                c["count"],
                int(c["filled"]),
            ]


def align_by_t(data: Series, key: str) -> Iterator[tuple[int, list[Any]]]:
    """(t, [valor de `key` por fuente, en el orden de `data`]) para cada `t` presente en alguna fuente.

    Los `t` salen ordenados; `None` donde una fuente no tiene punto en ese `t`.
    """
    all_t = sorted({p["t"] for points in data.values() for p in points})
    by_t = [{p["t"]: p[key] for p in points} for points in data.values()]
    for t in all_t:
        yield t, [values.get(t) for values in by_t]


def wide_rows(data: Series, interval: Interval, field: CandleField, decimal_sep: str) -> Iterator[list]:
    """Una columna por fuente, alineadas por inicio de cubeta; celda vacía si falta la cubeta."""
    yield ["date", "timestamp", *data]
    for t, values in align_by_t(data, field.value):
        yield [
            _row_date(agg.local_iso(t), interval),
            t,
            *(format_number(v, decimal_sep) for v in values),
        ]


def csv_rows(
    data: Series, interval: Interval, layout: Layout, field: CandleField, decimal_sep: str
) -> Iterator[list]:
    """Filas (cabecera incluida) según el intervalo y el `layout` (long | wide)."""
    if interval is Interval.raw:
        return raw_rows(data, decimal_sep)
    if layout == "long":
        return long_rows(data, interval, decimal_sep)
    return wide_rows(data, interval, field, decimal_sep)


def csv_chunks(rows: Iterable[list], sep: str, chunk_rows: int = 2000) -> Iterator[str]:
    """Emite el CSV en bloques de `chunk_rows` filas: memoria constante, sin límite de filas."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\n")
    pending = 0
    for row in rows:
        w.writerow(row)
        pending += 1
        if pending >= chunk_rows:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
            pending = 0
    if pending:
        yield buf.getvalue()


def export_filename(
    requested: str | None, interval: Interval, ts_from: int, ts_to: int, ext: str = "csv"
) -> str:
    """Nombre saneado (solo `A-Za-z0-9._-`); si no queda nada útil, uno derivado del rango.

    El nombre derivado lleva la extensión `ext`; el pedido se respeta tal cual (saneado).
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", requested or "")[:FILENAME_MAX_LEN]
    if re.search(r"[A-Za-z0-9]", safe):
        return safe
    first, last = agg.to_local(ts_from).date(), agg.to_local(ts_to - 1).date()
    return f"ves_rates_{interval.value}_{first}_{last}.{ext}"
