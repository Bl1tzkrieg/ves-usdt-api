"""Collector.collect_once: una fuente que falla no impide guardar las demás."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import db as dbmod
from app.collector import Collector
from app.db import Database, Tick
from app.sources.base import RateSource


class _Ok(RateSource):
    name, label, unit = "ok_src", "Ok", "USDT"

    async def fetch(self, client: httpx.AsyncClient) -> Tick:
        return Tick(self.name, int(time.time()), 42.0)


class _Boom(RateSource):
    name, label, unit = "boom_src", "Boom", "USDT"

    async def fetch(self, client: httpx.AsyncClient) -> Tick:
        raise RuntimeError("fuente caída")


@pytest.fixture()
def mem_db():
    mem = Database(":memory:")
    dbmod.set_db(mem)
    yield mem
    dbmod.set_db(None)


def test_failing_source_does_not_block_the_others(mem_db):
    c = Collector(sources=[])
    c.sources = [_Boom(), _Ok()]
    ticks = asyncio.run(c.collect_once())
    assert [t.source for t in ticks] == ["ok_src"]
    assert [t.price for t in mem_db.raw("ok_src", 0, 10**10)] == [42.0]
    assert mem_db.raw("boom_src", 0, 10**10) == []
    assert c.last_status["ok_src"] == "ok"
    assert c.last_status["boom_src"].startswith("error:") and "fuente caída" in c.last_status["boom_src"]
    assert c.last_run is not None
