from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SourceInfo(BaseModel):
    name: str
    label: str
    unit: str
    description: str
    ticks: int = 0
    first: str | None = None
    last: str | None = None


class LatestPoint(BaseModel):
    source: str
    t: int
    date: str
    price: float
    extra: dict[str, Any] | None = None


class CandleOut(BaseModel):
    t: int
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    avg: float | None
    count: int
    filled: bool = False


class RawPoint(BaseModel):
    t: int
    date: str
    price: float
    extra: dict[str, Any] | None = None


class Series(BaseModel):
    source: str
    label: str
    unit: str
    points: list[CandleOut] | list[RawPoint]


class HistoryResponse(BaseModel):
    start: str
    end: str
    interval: str
    fill: str
    timezone: str
    series: list[Series]


class ImportResult(BaseModel):
    received: int
    inserted: int
    ignored: int
