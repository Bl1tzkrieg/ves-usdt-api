from __future__ import annotations

import time
from abc import ABC, abstractmethod

import httpx

from ..db import Tick


class RateSource(ABC):
    """Una fuente devuelve un tick (precio en VES por 1 USDT/USD) al momento de consultarla."""

    name: str  # identificador estable, usado en la BD y en la API
    label: str  # nombre legible
    unit: str  # "USDT" o "USD"
    description: str = ""

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> Tick: ...

    @staticmethod
    def now() -> int:
        return int(time.time())


class SourceError(RuntimeError):
    pass
