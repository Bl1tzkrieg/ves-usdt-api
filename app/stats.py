"""Estadísticas de resumen: variación 24h/7d/30d, spread contra una referencia y variación diaria."""

from __future__ import annotations

from typing import Any

from . import aggregation as agg
from .aggregation import Fill, Interval
from .db import Database, Tick
from .series import SECONDS_PER_DAY, build_series

CHANGE_WINDOWS: dict[str, int] = {"24h": 1, "7d": 7, "30d": 30}


def price_ago(db: Database, latest: Tick, days: int) -> Tick | None:
    """Último tick de la fuente con `ts <= latest.ts - days * 86400`.

    Un tick exactamente `days` días antes del último cuenta (de ahí el `+ 1`, porque
    `Database.last_before` es estricto: `ts < límite`). `None` si no hay historia tan antigua.
    """
    return db.last_before(latest.source, latest.ts - days * SECONDS_PER_DAY + 1)


def change_between(old: Tick | None, new: Tick) -> dict[str, Any] | None:
    """Variación absoluta y porcentual de `old` a `new`; `None` sin precio anterior."""
    if old is None:
        return None
    return {
        "abs": new.price - old.price,
        "pct": pct_change(old.price, new.price),
        "from_price": old.price,
        "from_date": agg.local_iso(old.ts),
    }


def pct_change(old: float, new: float) -> float:
    """`(new / old - 1) * 100`. `Tick` garantiza precios positivos, así que `old` nunca es 0."""
    return (new / old - 1) * 100


def source_stats(db: Database, latest: Tick, reference: Tick | None) -> dict[str, Any]:
    """Último precio, variaciones por ventana y spread contra `reference` (`None` si es ella misma)."""
    spread = None
    if reference is not None and reference.source != latest.source:
        spread = pct_change(reference.price, latest.price)
    return {
        "source": latest.source,
        "latest": latest.price,
        "latest_date": agg.local_iso(latest.ts),
        "change": {k: change_between(price_ago(db, latest, d), latest) for k, d in CHANGE_WINDOWS.items()},
        "spread_vs_reference_pct": spread,
    }


def daily_changes(source: str, ts_from: int, ts_to: int) -> list[dict[str, Any]]:
    """Variación cierre-a-cierre entre días consecutivos con datos (velas diarias, `fill=none`).

    Los días sin datos se saltan: el primer día tras un hueco se compara con el último cierre
    disponible. El primer día del rango no tiene anterior y no aparece. `date` es YYYY-MM-DD local.
    """
    candles = build_series(source, ts_from, ts_to, Interval.day, Fill.none, 0)
    return [
        {"date": cur["date"][:10], "pct": pct_change(prev["close"], cur["close"])}
        for prev, cur in zip(candles, candles[1:], strict=False)
    ]
