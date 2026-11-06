"""Recolector en segundo plano: consulta cada fuente periódicamente y guarda los ticks.

El histórico se construye con lo que el recolector acumula (más lo que se importe por CSV),
así que no depende de que ninguna API externa ofrezca histórico ni imponga límites de rango.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import settings
from .db import Tick, get_db
from .sources import REGISTRY, RateSource

log = logging.getLogger("rates.collector")


class Collector:
    def __init__(self, sources: list[str] | None = None, interval: int | None = None) -> None:
        names = sources or settings.collector_sources
        self.sources: list[RateSource] = [REGISTRY[n] for n in names if n in REGISTRY]
        self.interval = interval or settings.collector_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_run: float | None = None
        self.last_status: dict[str, str] = {}

    async def collect_once(self) -> list[Tick]:
        """Consulta todas las fuentes en paralelo y persiste lo obtenido. Devuelve los ticks nuevos."""
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            results = await asyncio.gather(*(s.fetch(client) for s in self.sources), return_exceptions=True)
        ticks: list[Tick] = []
        for src, res in zip(self.sources, results):
            if isinstance(res, BaseException):
                self.last_status[src.name] = f"error: {res}"
                log.warning("Fuente %s falló: %s", src.name, res)
            else:
                self.last_status[src.name] = "ok"
                ticks.append(res)
        if ticks:
            inserted = await asyncio.to_thread(get_db().insert_ticks, ticks)
            log.info("Recolectados %d ticks (%d nuevos)", len(ticks), inserted)
        self.last_run = time.time()
        return ticks

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.collect_once()
            except Exception:  # noqa: BLE001 — el recolector nunca debe morir
                log.exception("Error inesperado en el recolector")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="rates-collector")

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            await self._task
            self._task = None

    def status(self) -> dict:
        return {
            "enabled": self._task is not None,
            "interval_seconds": self.interval,
            "sources": [s.name for s in self.sources],
            "last_run": self.last_run,
            "last_status": self.last_status,
        }


collector = Collector()
