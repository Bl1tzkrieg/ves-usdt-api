"""pytest -q  (usa una BD en memoria y fuentes simuladas; no toca la red)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.db import Database, Tick
from app.main import app
from app.sources.binance_p2p import BinanceP2PSource
from app.sources.dolarapi import BCVSource
from app.sources.yadio import YadioSource

DAY = 86400
T0 = 1_735_704_000  # 2025-01-01 00:00 -04:00


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("app.config.settings.collector_enabled", False)
    monkeypatch.setattr("app.config.settings.api_key", "test-key")
    mem = Database(":memory:")
    dbmod.set_db(mem)
    ticks = []
    for d in range(10):  # 10 días, 4 ticks/día para binance; BCV solo días hábiles (0..4 y 7..9)
        for h in (8, 12, 16, 20):
            ticks.append(Tick("binance_p2p", T0 + d * DAY + h * 3600, 100 + d + h / 100))
        if d % 7 < 5:
            ticks.append(Tick("bcv", T0 + d * DAY, 90 + d))
    mem.insert_ticks(ticks)
    with TestClient(app, headers={"X-API-Key": "test-key"}) as c:
        yield c
    dbmod.set_db(None)


def test_history_day_ohlc(client):
    r = client.get(
        "/api/v1/rates/history", params={"start": "2025-01-01", "end": "2025-01-03", "sources": "binance_p2p"}
    )
    assert r.status_code == 200
    pts = r.json()["series"][0]["points"]
    assert [p["date"][:10] for p in pts] == ["2025-01-01", "2025-01-02", "2025-01-03"]
    assert pts[0]["open"] == pytest.approx(100.08) and pts[0]["close"] == pytest.approx(100.20)
    assert pts[0]["count"] == 4


def test_fill_forward_with_seed(client):
    # rango que empieza en fin de semana (día 5): BCV no tiene tick, se rellena con el del día 4
    r = client.get(
        "/api/v1/rates/history",
        params={"start": "2025-01-06", "end": "2025-01-08", "sources": "bcv", "fill": "forward"},
    )
    pts = r.json()["series"][0]["points"]
    assert pts[0]["filled"] is True and pts[0]["close"] == 94
    assert pts[2]["filled"] is False and pts[2]["close"] == 97


def test_week_and_month_rollup(client):
    r = client.get(
        "/api/v1/rates/history", params={"days": 3650, "sources": "binance_p2p", "interval": "week"}
    )
    weeks = r.json()["series"][0]["points"]
    assert weeks[0]["date"][:10] == "2024-12-30"  # lunes
    assert sum(w["count"] for w in weeks) == 40
    r = client.get(
        "/api/v1/rates/history", params={"days": 3650, "sources": "binance_p2p", "interval": "month"}
    )
    assert len(r.json()["series"][0]["points"]) == 1


def test_wide_range_within_limit(client):
    r = client.get(
        "/api/v1/rates/history", params={"start": "2010-01-01", "end": "2025-12-31", "sources": "binance_p2p"}
    )
    assert r.status_code == 200 and len(r.json()["series"][0]["points"]) == 10


def test_csv_wide(client):
    r = client.get(
        "/api/v1/rates/history.csv",
        params={
            "start": "2025-01-01",
            "end": "2025-01-02",
            "sources": "binance_p2p,bcv",
            "layout": "wide",
            "sep": ";",
        },
    )
    lines = r.text.strip().splitlines()
    assert lines[0] == "date;timestamp;binance_p2p;bcv"
    assert lines[1].startswith("2025-01-01;") and lines[1].endswith(";100,2;90")  # sep=';' => coma decimal
    assert "attachment" in r.headers["content-disposition"]
    r = client.get(
        "/api/v1/rates/history.csv",
        params={"start": "2025-01-01", "end": "2025-01-01", "sources": "binance_p2p", "layout": "wide"},
    )
    assert r.text.strip().splitlines()[1].endswith(",100.2")  # sep=',' => punto decimal


def test_fill_forward_without_previous_omits_rows(client):
    # el rango empieza antes del primer tick: no hay nada que repetir, no se emiten filas vacías
    r = client.get(
        "/api/v1/rates/history",
        params={"start": "2024-12-25", "end": "2025-01-02", "sources": "bcv", "fill": "forward"},
    )
    pts = r.json()["series"][0]["points"]
    assert [p["date"][:10] for p in pts] == ["2025-01-01", "2025-01-02"]
    r = client.get(
        "/api/v1/rates/history",
        params={"start": "2024-12-30", "end": "2025-01-01", "sources": "bcv", "fill": "null"},
    )
    assert [p["close"] for p in r.json()["series"][0]["points"]] == [None, None, 90]


def test_validation_errors(client):
    assert (
        client.get("/api/v1/rates/history", params={"start": "2025-02-01", "end": "2025-01-01"}).status_code
        == 422
    )
    assert client.get("/api/v1/rates/history", params={"sources": "nope"}).status_code == 422


def _import(client, body, **params):
    if isinstance(body, str):
        body = body.encode()
    return client.post("/api/v1/rates/import", params=params, files={"file": ("h.csv", body, "text/csv")})


def test_import_csv(client):
    r = _import(client, "fecha;precio\n2020-01-01;5,5\n2020-01-02;6\n", source="bcv")
    assert r.json() == {"received": 2, "inserted": 2, "duplicates": 0, "rejected": 0, "rejected_sample": []}
    r = client.get(
        "/api/v1/rates/history", params={"start": "2020-01-01", "end": "2020-01-02", "sources": "bcv"}
    )
    assert [p["close"] for p in r.json()["series"][0]["points"]] == [5.5, 6]
    # reimportar: todo duplicado
    r = _import(client, "fecha;precio\n2020-01-01;5,5\n2020-01-02;6\n", source="bcv")
    assert (r.json()["inserted"], r.json()["duplicates"]) == (0, 2)


def test_import_thousands_and_rejected_rows(client):
    r = _import(client, "fecha;precio\n2020-01-01;1.234,56\n2020-01-02;abc\n", source="bcv")
    assert r.status_code == 200
    body = r.json()
    assert (body["received"], body["inserted"], body["rejected"]) == (2, 1, 1)
    assert body["rejected_sample"][0]["row"] == 2
    assert get_price(client, "bcv", 1_577_851_200) == 1234.56


def get_price(client, source, ts):
    row = (
        dbmod.get_db()
        ._conn.execute("SELECT price FROM ticks WHERE source=? AND ts=?", (source, ts))
        .fetchone()
    )
    return row[0] if row else None


def test_import_millisecond_timestamps_stored_as_seconds(client):
    r = _import(client, "timestamp,price\n1577851200000,10\n", source="bcv")
    assert r.json()["inserted"] == 1
    assert get_price(client, "bcv", 1_577_851_200) == 10


def test_import_date_time_columns_and_empty_timestamp(client):
    r = _import(client, "date,time,price\n2020-01-01,12:00,5\n", source="bcv")
    assert r.json()["inserted"] == 1 and get_price(client, "bcv", 1_577_851_200) == 5
    r = _import(client, "timestamp,date,price\n,2020-01-02,6\n", source="bcv")
    assert r.json()["inserted"] == 1 and get_price(client, "bcv", 1_577_851_200 + 86400) == 6


def test_import_ambiguous_price_tab_delimited_uses_dot(client):
    # tabulador: `history.csv?sep=%09` escribe decimal `.`, así que se reimporta con `.` como decimal
    r = _import(client, "fecha\tprecio\n2020-01-01\t1.234\n", source="bcv").json()
    assert (r["inserted"], r["rejected"]) == (1, 0)
    assert get_price(client, "bcv", 1_577_851_200) == 1.234


def test_import_ambiguous_price_resolved_by_delimiter(client):
    r = _import(client, 'fecha,precio\n2020-01-01,"1.234"\n', source="bcv").json()
    assert (r["inserted"], r["rejected"]) == (1, 0)
    assert get_price(client, "bcv", 1_577_851_200) == 1.234
    r = _import(client, "fecha;precio\n2020-01-02;1.234\n", source="bcv").json()
    assert (r["inserted"], r["rejected"]) == (1, 0)
    assert get_price(client, "bcv", 1_577_851_200 + 86400) == 1234


def test_import_rejected_sample_capped_at_10(client):
    body = "fecha,precio\n" + "".join(f"2020-01-01,x{i}\n" for i in range(15))
    r = _import(client, body, source="bcv").json()
    assert r["rejected"] == 15 and len(r["rejected_sample"]) == 10


def test_import_too_large_413(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.import_max_bytes", 1000)
    r = _import(client, "fecha,precio\n" + "2020-01-01,5\n" * 200, source="bcv")
    assert r.status_code == 413


def test_import_non_utf8_422(client):
    r = _import(client, b"fecha,precio\n2020-01-01,5\xff\xfe\n", source="bcv")
    assert r.status_code == 422


def test_import_missing_columns_and_source_422(client):
    assert _import(client, "a,b\n1,2\n", source="bcv").status_code == 422
    assert _import(client, "fecha,precio\n2020-01-01,5\n").status_code == 422
    assert _import(client, "fecha,precio\n2020-01-01,5\n", source="nope").status_code == 422


def test_import_replace_keeps_ticks_outside_range(client):
    _import(client, "fecha,precio\n2020-01-01,1\n2020-01-10,2\n2020-01-20,3\n", source="bcv")
    r = _import(client, "fecha,precio\n2020-01-05,9\n2020-01-12,9\n", source="bcv", replace="true")
    assert r.json()["inserted"] == 2
    rows = (
        dbmod.get_db()
        ._conn.execute("SELECT ts, price FROM ticks WHERE source='bcv' AND ts < ? ORDER BY ts", (T0,))
        .fetchall()
    )
    assert [r[1] for r in rows] == [
        1,
        9,
        9,
        3,
    ]  # 01-01 y 01-20 fuera del rango se conservan; 01-10 reemplazado


# ---------------------------------------------------------------- fuentes (HTTP simulado)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_binance_source_parses_median():
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        base = 100 if body["tradeType"] == "BUY" else 98
        return httpx.Response(200, json={"data": [{"adv": {"price": str(base + i)}} for i in range(5)]})

    tick = asyncio.run(BinanceP2PSource().fetch(_client(handler)))
    assert tick.extra["buy"] == 102 and tick.extra["sell"] == 100 and tick.price == 101


def test_yadio_and_bcv_sources():
    def handler(req: httpx.Request):
        if "yadio" in req.url.host:
            return httpx.Response(200, json={"rate": 938.9, "timestamp": 1788954013210})
        return httpx.Response(
            200, json={"promedio": 832.49, "fechaActualizacion": "2026-09-11T00:00:00-04:00"}
        )

    y = asyncio.run(YadioSource().fetch(_client(handler)))
    b = asyncio.run(BCVSource().fetch(_client(handler)))
    assert y.price == 938.9 and y.ts == 1788954013
    assert b.price == 832.49 and b.ts == 1789099200


def test_import_replace_with_rejected_rows_422_and_writes_nothing(client):
    _import(client, "fecha,precio\n2020-01-01,1\n2020-01-02,2\n2020-01-03,3\n", source="bcv")
    # la fila rechazada (01-02) cae dentro del rango [01-01, 01-03] de las aceptadas
    r = _import(
        client, "fecha,precio\n2020-01-01,7\n2020-01-02,abc\n2020-01-03,9\n", source="bcv", replace="true"
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["rejected"] == 1 and detail["rejected_sample"][0]["row"] == 2
    rows = (
        dbmod.get_db()
        ._conn.execute("SELECT price FROM ticks WHERE source='bcv' AND ts < ? ORDER BY ts", (T0,))
        .fetchall()
    )
    assert [r[0] for r in rows] == [1, 2, 3]


def test_import_without_replace_still_accepts_partial(client):
    r = _import(client, "fecha,precio\n2020-01-01,7\n2020-01-02,abc\n", source="bcv")
    assert r.status_code == 200 and (r.json()["inserted"], r.json()["rejected"]) == (1, 1)


@pytest.mark.parametrize("sep", [";", ","])
def test_history_csv_round_trip_with_replace(client, sep):
    # precios con exactamente 3 decimales: ambiguos sin la pista del delimitador
    db = dbmod.get_db()
    db.insert_ticks([Tick("yadio", T0 + d * DAY + 3600, 36.512 + d) for d in range(5)])
    db.insert_ticks([Tick("yadio", T0 + 5 * DAY + 3600, 1234.125)])
    params = {"start": "2025-01-01", "end": "2025-01-06", "sources": "yadio", "sep": sep}
    before = client.get(
        "/api/v1/rates/history", params={k: v for k, v in params.items() if k != "sep"}
    ).json()
    exported = client.get("/api/v1/rates/history.csv", params=params)
    assert exported.status_code == 200
    r = _import(client, exported.content, source="yadio", replace="true")
    assert r.status_code == 200, r.text
    assert r.json()["rejected"] == 0
    after = client.get("/api/v1/rates/history", params={k: v for k, v in params.items() if k != "sep"}).json()
    closes = [p["close"] for p in after["series"][0]["points"]]
    assert closes == [p["close"] for p in before["series"][0]["points"]]
    assert closes == pytest.approx([36.512, 37.512, 38.512, 39.512, 40.512, 1234.125])


def test_latest_and_sources(client):
    latest = client.get("/api/v1/rates/latest").json()
    assert {p["source"] for p in latest} == {"binance_p2p", "bcv"}
    binance = next(p for p in latest if p["source"] == "binance_p2p")
    assert binance["price"] == pytest.approx(109.20)  # día 9, 20:00
    sources = client.get("/api/v1/rates/sources").json()
    by_name = {s["name"]: s for s in sources}
    assert {"binance_p2p", "bcv"} <= set(by_name)
    assert by_name["binance_p2p"]["label"]


def test_stream_unknown_source_422(client):
    assert client.get("/api/v1/rates/stream", params={"sources": "nope"}).status_code == 422


def test_stream_first_event_has_latest_rates(client):
    """Se consume el generador directamente: TestClient no puede cerrar un stream infinito."""
    from app.routers.stream import stream

    class _Connected:
        async def is_disconnected(self) -> bool:
            return False

    async def first_chunk() -> str:
        resp = await stream(_Connected(), sources="binance_p2p", every=2)
        assert resp.media_type == "text/event-stream"
        gen = resp.body_iterator
        try:
            return await anext(gen)
        finally:
            await gen.aclose()

    chunk = asyncio.run(first_chunk())
    head, data = chunk.rstrip("\n").split("\n", 1)
    assert head == "event: rates"
    payload = json.loads(data[len("data: ") :])
    assert [p["source"] for p in payload] == ["binance_p2p"]
    assert payload[0]["price"] == pytest.approx(109.20)


def test_repeated_sources_are_deduplicated(client):
    r = client.get(
        "/api/v1/rates/history", params={"start": "2025-01-01", "end": "2025-01-02", "sources": "bcv,bcv"}
    )
    assert r.status_code == 200 and [s["source"] for s in r.json()["series"]] == ["bcv"]
    r = client.get(
        "/api/v1/rates/history.csv",
        params={"start": "2025-01-01", "end": "2025-01-02", "sources": "bcv,bcv", "layout": "wide"},
    )
    assert r.text.splitlines()[0] == "date,timestamp,bcv"
