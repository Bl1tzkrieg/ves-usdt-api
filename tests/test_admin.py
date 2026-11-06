"""POST /backfill/yadio: histórico de Yadio para planificadores (n8n), con transporte simulado."""

from __future__ import annotations

import httpx
import pytest

from app import db as dbmod
from app.routers import admin

URL = "/api/v1/rates/backfill/yadio"
ITEMS = [
    {"date": "01/01/2020", "rate": 10.5},
    {"date": "01/02/2020", "rate": 11.0},
    {"date": "01/03/2020", "rate": 11.5},
]


@pytest.fixture()
def upstream(monkeypatch):
    """Instala un transporte simulado; devuelve el estado para ajustar la respuesta y ver peticiones."""
    state = {"status": 200, "body": ITEMS, "paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["paths"].append(request.url.path)
        return httpx.Response(state["status"], json=state["body"])

    monkeypatch.setattr(
        admin, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return state


def test_backfill_inserts_ticks(client, upstream):
    r = client.post(URL)
    assert r.status_code == 200
    assert r.json() == {"received": 3, "inserted": 3, "skipped": 0}
    assert len(dbmod.get_db().raw("yadio", 0, 4_000_000_000)) == 3
    assert upstream["paths"] == ["/hist/365/VES"]


def test_backfill_second_call_ignores_duplicates(client, upstream):
    client.post(URL)
    r = client.post(URL)
    assert r.json() == {"received": 3, "inserted": 0, "skipped": 0}


def test_backfill_requires_api_key(client, upstream):
    r = client.post(URL, headers={"X-API-Key": ""})
    assert r.status_code == 401
    assert upstream["paths"] == []


def test_backfill_upstream_error_is_502_with_short_detail(client, upstream):
    upstream["status"] = 500
    upstream["body"] = {"error": "x" * 1000}
    r = client.post(URL)
    assert r.status_code == 502
    assert len(r.json()["detail"]) < 400


def test_backfill_unexpected_shape_is_502(client, upstream):
    upstream["body"] = {"oops": 1}
    assert client.post(URL).status_code == 502


def test_backfill_clamps_days(client, upstream):
    client.post(URL + "?days=9999")
    client.post(URL + "?days=0")
    assert upstream["paths"] == ["/hist/365/VES", "/hist/1/VES"]


def test_backfill_skips_bad_items(client, upstream):
    upstream["body"] = [
        *ITEMS,
        {"date": "nope", "rate": 1},
        {"date": "01/05/2020"},
        {"date": "01/06/2020", "rate": -3},
    ]
    r = client.post(URL)
    assert r.json() == {"received": 3, "inserted": 3, "skipped": 3}
