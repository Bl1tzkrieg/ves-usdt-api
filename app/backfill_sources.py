"""Histórico externo para cargas masivas (hoy Yadio), compartido por el endpoint y el script."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from .aggregation import LOCAL_TZ
from .db import Database, Tick

YADIO_HIST_URL = "https://api.yadio.io/hist/{days}/{currency}"
YADIO_MAX_DAYS = 365
_SNIPPET = 200


class BackfillError(RuntimeError):
    """El origen respondió con error HTTP o con una forma inesperada."""


@dataclass
class BackfillResult:
    """`ticks`: puntos válidos; `skipped`: cuántos puntos del origen no se pudieron interpretar."""

    ticks: list[Tick] = field(default_factory=list)
    skipped: int = 0


def clamp_days(days: int) -> int:
    return min(max(days, 1), YADIO_MAX_DAYS)


async def fetch_yadio_history(
    client: httpx.AsyncClient, days: int, currency: str = "VES", source: str = "yadio"
) -> BackfillResult:
    """GET https://api.yadio.io/hist/{days}/{currency} (days 1..365, se recorta).

    Respuesta: [{"date": "09/14/2026", "rate": 940.01, ...}, ...]. `date` es MM/DD/YYYY y se guarda
    a la medianoche local (`LOCAL_TZ`). Los puntos inválidos se omiten y se cuentan en `skipped`.
    Lanza `BackfillError` ante error HTTP, de red o respuesta que no sea una lista.
    """
    url = YADIO_HIST_URL.format(days=clamp_days(days), currency=currency)
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        raise BackfillError(f"Yadio respondió {e.response.status_code}: {e.response.text[:_SNIPPET]}") from e
    except (httpx.HTTPError, ValueError) as e:
        raise BackfillError(f"Yadio no disponible: {str(e)[:_SNIPPET]}") from e
    if not isinstance(data, list):
        raise BackfillError(f"Yadio respondió con una forma inesperada: {str(data)[:_SNIPPET]}")
    result = BackfillResult()
    for item in data:
        try:
            day = datetime.strptime(item["date"], "%m/%d/%Y").replace(tzinfo=LOCAL_TZ)
            result.ticks.append(Tick(source=source, ts=int(day.timestamp()), price=float(item["rate"])))
        except (KeyError, ValueError, TypeError):
            result.skipped += 1
    return result


@dataclass
class GapFill:
    """`ticks`: los que caen en días sin datos; `ignored_days`: días locales que ya tenían ticks."""

    ticks: list[Tick]
    ignored_days: int


def keep_missing_days(db: Database, ticks: list[Tick]) -> GapFill:
    """Descarta los ticks diarios cuyo día local ya tiene algún tick de la misma fuente.

    El histórico trae un punto por día a la medianoche; si el recolector ya cubrió ese día, ese punto
    (parcial o de cierre) se sumaría a sus velas y falsearía apertura, mínimo y máximo.
    Una consulta por fuente sobre el rango de los ticks recibidos.
    """
    by_source: dict[str, list[Tick]] = defaultdict(list)
    for t in ticks:
        by_source[t.source].append(t)
    kept: list[Tick] = []
    ignored: set[tuple[str, date]] = set()
    for source, group in by_source.items():
        lo = min(t.ts for t in group)
        hi = max(t.ts for t in group)
        # Se amplía un día por lado para abarcar los días locales completos de los extremos.
        covered = db.days_with_ticks(source, lo - 86400, hi + 86400)
        for t in group:
            day = datetime.fromtimestamp(t.ts, LOCAL_TZ).date()
            if day in covered:
                ignored.add((source, day))
            else:
                kept.append(t)
    return GapFill(ticks=kept, ignored_days=len(ignored))
