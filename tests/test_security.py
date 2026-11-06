"""Seguridad: API key en endpoints de escritura, cabeceras y guarda de concurrencia del recolector."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.collector import Collector, CollectInProgress
from app.db import Database, Tick
from app.main import app
from app.sources.base import RateSource

CSV = b"fecha,precio\n2020-01-01,5\n"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("app.config.settings.collector_enabled", False)
    monkeypatch.setattr("app.config.settings.api_key", "k")
    dbmod.set_db(Database(":memory:"))
    with TestClient(app) as c:
        yield c
    dbmod.set_db(None)


def _import(client, **kw):
    return client.post("/api/v1/rates/import", params={"source": "bcv"}, files={"file": ("h.csv", CSV, "text/csv")}, **kw)


def test_import_requires_key(client):
    assert _import(client).status_code == 401
    assert _import(client, headers={"X-API-Key": "mala"}).status_code == 401


def test_import_with_key_ok(client):
    assert _import(client, headers={"X-API-Key": "k"}).status_code == 200


def test_import_503_when_key_not_configured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.api_key", "")
    r = _import(client, headers={"X-API-Key": ""})
    assert r.status_code == 503
    assert "RATES_API_KEY" in r.json()["detail"]


def test_collect_requires_key(client, monkeypatch):
    assert client.post("/api/v1/rates/collect").status_code == 401
    monkeypatch.setattr("app.config.settings.api_key", "")
    assert client.post("/api/v1/rates/collect").status_code == 503


def test_collect_with_key_and_conflict(client, monkeypatch):
    async def fake(self):
        return []

    monkeypatch.setattr(Collector, "collect_once", fake)
    assert client.post("/api/v1/rates/collect", headers={"X-API-Key": "k"}).status_code == 200

    async def busy(self):
        raise CollectInProgress("en curso")

    monkeypatch.setattr(Collector, "collect_once", busy)
    assert client.post("/api/v1/rates/collect", headers={"X-API-Key": "k"}).status_code == 409


def test_read_endpoints_stay_open(client):
    assert client.get("/api/v1/rates/history").status_code == 200


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
    )


def test_cors_default_is_closed():
    from app.config import Settings

    assert Settings(_env_file=None).cors_origins == []


class _Slow(RateSource):
    name, label, unit = "slow", "Slow", "USDT"

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    async def fetch(self, client: httpx.AsyncClient) -> Tick:
        await self.gate.wait()
        return Tick("bcv", 1_700_000_000, 1.0)


def test_concurrent_collect_raises_in_progress(monkeypatch):
    monkeypatch.setattr("app.config.settings.collector_enabled", False)
    dbmod.set_db(Database(":memory:"))

    async def scenario():
        gate = asyncio.Event()
        col = Collector(sources=[])
        col.sources = [_Slow(gate)]
        first = asyncio.create_task(col.collect_once())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(CollectInProgress):
            await col.collect_once()
        gate.set()
        assert len(await first) == 1
        # justo después de una corrida, la siguiente se omite (min_gap_seconds)
        assert await col.collect_once() == []

    try:
        asyncio.run(scenario())
    finally:
        dbmod.set_db(None)
