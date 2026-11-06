"""Parseo tolerante de fechas, precios y filas a `Tick`, compartido por `/import` y `scripts/backfill`."""
from __future__ import annotations

import csv
import math
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from .aggregation import LOCAL_TZ
from .db import Tick

TS_KEYS = ("timestamp", "ts", "date", "fecha", "datetime", "time", "t", "last_update", "x")
PRICE_KEYS = ("price", "rate", "close", "precio", "promedio", "value", "avg", "y")
SOURCE_KEYS = ("source", "fuente")
EXTRA_KEYS = ("buy", "sell")

# Solo formatos DD/MM: MM/DD/YYYY es ambiguo y debe pedirse con `fmt=`.
_DAY_FIRST_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%Y, %I:%M %p", "%d/%m/%Y, %I:%M %p")
_MS_THRESHOLD = 1e11
# Epoch numérico mínimo aceptado en filas importadas (2000-01-01 UTC): por debajo suele ser un
# serial de Excel o una fecha compacta mal leída, no un instante real.
MIN_PLAUSIBLE_TS = 946_684_800


@dataclass
class ParseResult:
    ticks: list[Tick] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def parse_ts(value: Any, *, fmt: str | None = None) -> int | None:
    """Epoch (s o ms), ISO 8601, `YYYY-MM-DD`, `YYYYMMDD`, `DD-MM-YYYY` o `DD/MM/YYYY` → epoch en segundos.

    Sin zona horaria se asume la local. Con `fmt` solo se usa ese formato `strptime`.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(number / 1000) if number > _MS_THRESHOLD else int(number)
    text = str(value).strip()
    if not text:
        return None
    if _is_compact_date(text):
        try:
            return int(datetime.strptime(text, "%Y%m%d").replace(tzinfo=LOCAL_TZ).timestamp())
        except ValueError:
            return None
    if _is_numeric_text(text):
        return parse_ts(float(text))
    if fmt:
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=LOCAL_TZ).timestamp())
        except ValueError:
            return None
    for candidate in _DAY_FIRST_FORMATS:
        try:
            return int(datetime.strptime(text, candidate).replace(tzinfo=LOCAL_TZ).timestamp())
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return int(parsed.timestamp())


def _is_compact_date(text: str) -> bool:
    return len(text) == 8 and text.isascii() and text.isdigit()


def _is_numeric_text(text: str) -> bool:
    return text.replace(".", "", 1).isdigit()


def _parse_row_ts(value: Any) -> int | None:
    """`parse_ts` para filas importadas: un epoch numérico anterior a 2000 lanza ValueError."""
    ts = parse_ts(value)
    if ts is None or ts >= MIN_PLAUSIBLE_TS:
        return ts
    text = str(value).strip()
    if isinstance(value, (int, float)) or (_is_numeric_text(text) and not _is_compact_date(text)):
        raise ValueError(f"timestamp fuera de rango plausible: {value!r}")
    return ts


_CURRENCY_PREFIX = re.compile(r"(?i)^\s*(bs\.?\s*[sd]?\.?|ves)\s*")
_GROUPING_TAIL = re.compile(r"[.,]\d{3}$")


def parse_price(value: Any, *, decimal_hint: str | None = None) -> float | None:
    """Número o texto (`1.234,56`, `1,234.56`, `1,5`, `Bs. 950`) → float finito, o None.

    Un texto con un solo tipo de separador que parece agrupación de miles (`1.234`, `1,234`,
    `1.234.567`) es ambiguo: sin `decimal_hint` devuelve None en vez de adivinar. Con
    `decimal_hint` (`,` o `.`) ese separador es el decimal y el otro, agrupación de miles.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = re.sub(r"\s+", "", _CURRENCY_PREFIX.sub("", str(value)))
        has_comma, has_dot = "," in text, "." in text
        if has_comma and has_dot:
            # el último separador es el decimal
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif has_comma or has_dot:
            sep = "," if has_comma else "."
            integer_part = text.split(sep)[0].lstrip("+-")
            if text.count(sep) > 1 or (_GROUPING_TAIL.search(text) and integer_part != "0"):
                resolved = _resolve_ambiguous(text, sep, decimal_hint)
                if resolved is None:
                    return None
                text = resolved
            text = text.replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _resolve_ambiguous(text: str, sep: str, decimal_hint: str | None) -> str | None:
    """Usa la pista decimal para decidir un texto ambiguo; None si no hay pista o no encaja."""
    if decimal_hint is None:
        return None
    if sep == decimal_hint:
        return text if text.count(sep) == 1 else None
    return text.replace(sep, "") if re.fullmatch(rf"[+-]?\d{{1,3}}(?:{re.escape(sep)}\d{{3}})+", text) else None


def decimal_hint_for(delimiter: str) -> str | None:
    """Separador decimal implícito por el delimitador del CSV (así escribe `history.csv`).

    `;` ⇒ decimal `,`; `,` y tabulador ⇒ decimal `.`; cualquier otro ⇒ sin pista.
    """
    return {";": ",", ",": ".", "\t": "."}.get(delimiter)


def sniff_dialect(sample: str) -> type[csv.Dialect]:
    """Detecta el delimitador (`,` `;` tab); si no puede, usa `csv.excel`."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def find_list(data: Any, list_key: str | None) -> list:
    """Localiza la lista de puntos dentro de una respuesta JSON (ruta `a.b` o autodetección)."""
    if list_key:
        for part in list_key.split("."):
            data = data[part]
        return data
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v:
                return v
        for v in data.values():
            if isinstance(v, dict):
                try:
                    return find_list(v, None)
                except ValueError:
                    pass
    raise ValueError("No encontré una lista de puntos; use --list-key")


def _pick_parsed(
    row: Mapping[str, Any], explicit: str | None, candidates: tuple[str, ...], parse: Callable[[Any], Any], label: str
) -> Any:
    """Primer valor no vacío de las columnas candidatas que se deje interpretar; ValueError si ninguno."""
    keys = (explicit.lower().strip(),) if explicit else tuple(k for k in candidates if k in row)
    first_raw = None
    for key in keys:
        raw = row.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        parsed = parse(raw)
        if parsed is not None:
            return parsed
        if first_raw is None:
            first_raw = raw
    if first_raw is None:
        raise ValueError(f"{label} ausente")
    raise ValueError(f"{label} inválido: {first_raw!r}")


def _row_to_tick(
    item: Any,
    default_source: str | None,
    ts_key: str | None,
    price_key: str | None,
    allowed_sources: Collection[str],
    decimal_hint: str | None = None,
) -> Tick:
    """Construye un Tick o lanza ValueError con el motivo (en español)."""
    price_of = partial(parse_price, decimal_hint=decimal_hint)
    source = default_source
    extra: dict[str, float] = {}
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        ts, price = _parse_row_ts(item[0]), price_of(item[1])
        if ts is None:
            raise ValueError(f"fecha inválida o ausente: {item[0]!r}")
        if price is None:
            raise ValueError(f"precio inválido o ausente: {item[1]!r}")
    elif isinstance(item, Mapping):
        row = {k.lower().strip(): v for k, v in item.items() if isinstance(k, str)}
        ts = _pick_parsed(row, ts_key, TS_KEYS, _parse_row_ts, "fecha")
        price = _pick_parsed(row, price_key, PRICE_KEYS, price_of, "precio")
        raw_source = next((row[k] for k in SOURCE_KEYS if row.get(k)), None)
        source = raw_source or default_source
        for key in EXTRA_KEYS:
            value = price_of(row.get(key))
            if value is not None and value > 0:
                extra[key] = value
    else:
        raise ValueError("fila con formato no reconocido")

    if not source:
        raise ValueError("sin fuente (columna `source` o parámetro)")
    if source not in allowed_sources:
        raise ValueError(f"fuente desconocida: {source!r}")
    return Tick(source=str(source), ts=ts, price=price, extra=extra or None)


def rows_to_ticks(
    rows: Iterable[Any],
    *,
    default_source: str | None,
    ts_key: str | None = None,
    price_key: str | None = None,
    allowed_sources: Collection[str],
    decimal_hint: str | None = None,
) -> ParseResult:
    """Convierte filas (dicts de CSV/JSON o pares `[ts, precio]`) en ticks; las malas van a `rejected`.

    Cada rechazo es `{"row": n, "reason": str}` con `n` empezando en 1. `decimal_hint` resuelve
    precios ambiguos (ver `parse_price`).
    """
    result = ParseResult()
    for n, item in enumerate(rows, start=1):
        try:
            result.ticks.append(_row_to_tick(item, default_source, ts_key, price_key, allowed_sources, decimal_hint))
        except ValueError as e:
            result.rejected.append({"row": n, "reason": str(e)})
    return result
