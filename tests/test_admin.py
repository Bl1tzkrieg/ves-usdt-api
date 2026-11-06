"""POST /backfill/yadio: histórico de Yadio para planificadores (n8n), con transporte simulado."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest

from app import db as dbmod
from app.aggregation import LOCAL_TZ
from app.db import Tick
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
    assert r.json() == {"received": 3, "inserted": 3, "skipped": 0, "ignored_days": 0}
    assert len(dbmod.get_db().raw("yadio", 0, 4_000_000_000)) == 3
    assert upstream["paths"] == ["/hist/365/VES"]


def test_backfill_second_call_ignores_duplicates(client, upstream):
    client.post(URL)
    r = client.post(URL)
    assert r.json() == {"received": 3, "inserted": 0, "skipped": 0, "ignored_days": 3}


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
    assert r.json() == {"received": 3, "inserted": 3, "skipped": 3, "ignored_days": 0}


def _local_ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=LOCAL_TZ).timestamp())


def test_backfill_only_fills_days_without_ticks(client, upstream):
    # Tick del recolector a las 23:30 locales del 2 de enero (3 de enero en UTC): cubre el día local 2.
    collected = Tick("yadio", _local_ts(2020, 1, 2, 23, 30), 11.7)
    dbmod.get_db().insert_ticks([collected])
    r = client.post(URL)
    assert r.json() == {"received": 3, "inserted": 2, "skipped": 0, "ignored_days": 1}
    stored = dbmod.get_db().raw("yadio", 0, 4_000_000_000)
    by_day = {datetime.fromtimestamp(t.ts, LOCAL_TZ).date(): t for t in stored}
    assert sorted(by_day) == [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
    assert len(stored) == 3
    assert by_day[date(2020, 1, 2)] == collected
    assert by_day[date(2020, 1, 1)].price == 10.5
    assert by_day[date(2020, 1, 3)].price == 11.5
