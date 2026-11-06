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


def test_write_error_is_exposed_in_status_and_cleared_on_success(mem_db, monkeypatch):
    c = Collector(sources=[])
    c.sources = [_Ok()]

    def broken(ticks):
        raise PermissionError("attempt to write a readonly database")

    monkeypatch.setattr(mem_db, "insert_ticks", broken)
    with pytest.raises(PermissionError):
        asyncio.run(c.collect_once())
    assert "readonly database" in c.status()["last_error"]

    monkeypatch.undo()
    dbmod.set_db(mem_db)
    c.last_run = None
    asyncio.run(c.collect_once())
    assert c.status()["last_error"] is None


def test_health_exposes_collector_last_error(client, monkeypatch):
    from app.collector import collector

    monkeypatch.setattr(collector, "last_error", "attempt to write a readonly database")
    assert client.get("/health").json()["collector"]["last_error"] == "attempt to write a readonly database"
