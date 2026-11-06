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


@pytest.mark.parametrize("path", ["/api/v1/rates/latest", "/api/v1/rates/sources", "/api/v1/rates/collector", "/"])
def test_read_paths_open_without_key(client, path):
    assert TestClient(app).get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json"])
def test_docs_paths_skip_csp_but_keep_other_headers(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_index_keeps_full_csp(client):
    r = client.get("/")
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert r.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------ ImportGuardMiddleware (antes del multipart)

IMPORT = "/api/v1/rates/import"
HUGE = str(10**12)


@pytest.fixture()
def multipart_spy(monkeypatch):
    calls = []

    async def spy(self):
        calls.append(1)
        raise AssertionError("el cuerpo multipart no debía leerse")

    monkeypatch.setattr("starlette.formparsers.MultiPartParser.parse", spy)
    return calls


def _raw_post(client, headers, content=b"x"):
    return client.post(IMPORT, params={"source": "bcv"}, content=content,
                       headers={"Content-Type": "multipart/form-data; boundary=b", **headers})


def test_import_guard_401_without_key_before_body(client, multipart_spy):
    r = _raw_post(client, {"Content-Length": HUGE})
    assert r.status_code == 401 and r.json() == {"detail": "API key inválida o ausente"}
    r = _raw_post(client, {"Content-Length": HUGE, "X-API-Key": "mala"})
    assert r.status_code == 401
    assert multipart_spy == []


def test_import_guard_503_when_key_unset(client, multipart_spy, monkeypatch):
    monkeypatch.setattr("app.config.settings.api_key", "")
    r = _raw_post(client, {"Content-Length": HUGE, "X-API-Key": ""})
    assert r.status_code == 503 and "RATES_API_KEY" in r.json()["detail"]
    assert multipart_spy == []


def test_import_guard_413_with_key_over_cap(client, multipart_spy, monkeypatch):
    monkeypatch.setattr("app.config.settings.import_max_bytes", 1000)
    r = _raw_post(client, {"Content-Length": str(1000 + 64 * 1024 + 1), "X-API-Key": "k"})
    assert r.status_code == 413 and "detail" in r.json()
    assert multipart_spy == []


def test_import_guard_411_without_content_length(client, multipart_spy):
    def body():
        yield b"x"

    r = _raw_post(client, {"X-API-Key": "k"}, content=body())
    assert r.status_code == 411 and "detail" in r.json()
    assert multipart_spy == []


def test_import_guard_411_with_transfer_encoding(client, multipart_spy):
    # aunque traiga un Content-Length válido: con Transfer-Encoding el tamaño declarado no es fiable
    r = _raw_post(client, {"X-API-Key": "k", "Content-Length": "1", "Transfer-Encoding": "chunked"})
    assert r.status_code == 411 and "Transfer-Encoding" in r.json()["detail"]
    assert multipart_spy == []


def test_import_guard_asgi_never_reads_body(monkeypatch):
    from app.security import ImportGuardMiddleware

    monkeypatch.setattr("app.config.settings.api_key", "k")

    async def run(headers):
        sent = []

        async def app_(scope, receive, send):
            raise AssertionError("no debía llegar a la app")

        async def receive():
            raise AssertionError("el cuerpo no debía leerse")

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": IMPORT, "headers": headers}
        await ImportGuardMiddleware(app_)(scope, receive, send)
        return sent[0]["status"]

    assert asyncio.run(run([(b"x-api-key", b"k"), (b"content-length", b"abc")])) == 400
    assert asyncio.run(run([(b"x-api-key", b"k"), (b"content-length", HUGE.encode())])) == 413
    assert asyncio.run(run([(b"x-api-key", b"k")])) == 411
    assert asyncio.run(run([(b"x-api-key", b"k"), (b"transfer-encoding", b"chunked"), (b"content-length", b"10")])) == 411
    assert asyncio.run(run([(b"transfer-encoding", b"chunked")])) == 401
    assert asyncio.run(run([(b"content-length", b"10")])) == 401


def test_import_guard_ignores_other_routes(client):
    # GET de lectura sin clave ni Content-Length sigue funcionando
    assert client.get("/api/v1/rates/history").status_code == 200
