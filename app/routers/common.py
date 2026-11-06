"""Parámetros compartidos por los routers de tasas: prefijo, fuentes y la consulta de histórico."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..aggregation import Fill, Interval
from ..series import InvalidQuery, resolve_range
from ..sources import DEFAULT_SOURCE, REGISTRY

RATES_PREFIX = "/api/v1/rates"
RATES_TAGS: list[str] = ["rates"]


def resolve_sources(raw: str | None) -> list[str]:
    """Nombres de fuente separados por coma (por defecto `DEFAULT_SOURCE`); 422 si alguno no existe."""
    names = [s.strip() for s in (raw or DEFAULT_SOURCE).split(",") if s.strip()]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise InvalidQuery(f"Fuente(s) desconocida(s): {unknown}. Disponibles: {sorted(REGISTRY)}")
    return names


def resolve_sources_or_all(raw: str | None) -> list[str]:
    """Como `resolve_sources`, pero vacío significa todas las fuentes registradas."""
    return resolve_sources(raw) if raw else list(REGISTRY)


class HistoryQuery(BaseModel):
    """Consulta de histórico compartida por `/history` y `/history.csv` (parámetros de query)."""

    start: str | None = Field(
        None, description="Inicio: YYYY-MM-DD, ISO 8601 o epoch. Por defecto `end - days`."
    )
    end: str | None = Field(None, description="Fin (inclusivo si es fecha). Por defecto ahora.")
    days: int | None = Field(
        None,
        ge=1,
        description="Alternativa a `start`: días hacia atrás desde `end`. Máximo `max_range_days`.",
    )
    sources: str | None = Field(
        None, description=f"Fuentes separadas por coma. Por defecto `{DEFAULT_SOURCE}`. Ej: binance_p2p,bcv"
    )
    interval: Interval = Field(Interval.day, description="raw | hour | day | week | month")
    fill: Fill = Field(Fill.none, description="Relleno de cubetas vacías: none | forward | null")
    max_points: int = Field(
        0,
        ge=0,
        description="Solo interval=raw: reduce a ~N puntos (0 = el tope del servidor, `raw_max_points`)",
    )

    def resolve(self) -> tuple[int, int, list[str]]:
        """(inicio, fin, fuentes) validados; lanza `InvalidQuery` (422)."""
        ts_from, ts_to = resolve_range(self.start, self.end, self.days)
        return ts_from, ts_to, resolve_sources(self.sources)
