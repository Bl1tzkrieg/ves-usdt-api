"""Guardas de rango/tamaño, entradas malformadas y endurecimiento de fuentes."""

from __future__ import annotations

import asyncio
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.collector import Collector
from app.config import Settings
from app.db import Database, Tick
from app.main import app
from app.sources.base import SourceError
from app.sources.dolarapi import BCVSource
from app.sources.yadio import YadioSource

H = "/api/v1/rates/history"
T0 = 1_735_707_600


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("app.config.settings.collector_enabled", False)
    monkeypatch.setattr("app.config.settings.api_key", "k")
    mem = Database(":memory:")
    dbmod.set_db(mem)
    mem.insert_ticks([Tick("binance_p2p", T0 + i * 3600, 100 + i) for i in range(5)])
    with TestClient(app, headers={"X-API-Key": "k"}) as c:
        yield c
    dbmod.set_db(None)


def test_days_over_limit_422(client):
    assert client.get(H, params={"days": 8000}).status_code == 422
    assert client.get(H + ".csv", params={"days": 8000}).status_code == 422


def test_explicit_range_over_limit_422(client):
    r = client.get(H, params={"start": "1990-01-01", "end": "2026-01-01"})
    assert r.status_code == 422


def test_fill_hour_19_years_422(client):
    r = client.get(H, params={"start": "2006-01-01", "end": "2025-01-01", "interval": "hour", "fill": "null"})
    assert r.status_code == 422
    r = client.get(
        H + ".csv", params={"start": "2006-01-01", "end": "2025-01-01", "interval": "hour", "fill": "forward"}
    )
    assert r.status_code == 422


def test_fill_none_hour_wide_range_ok(client):
    r = client.get(H, params={"start": "2006-01-01", "end": "2025-01-01", "interval": "hour"})
    assert r.status_code == 200


def test_fill_week_within_limit_ok(client):
    r = client.get(H, params={"days": 3650, "interval": "week", "fill": "null"})
    assert r.status_code == 200


def test_raw_capped_by_raw_max_points(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.raw_max_points", 1000)
    dbmod.get_db().insert_ticks([Tick("yadio", T0 + i * 60, 50 + i * 0.001) for i in range(2500)])
    p = client.get(H, params={"start": T0, "end": T0 + 3 * 86400, "interval": "raw", "sources": "yadio"})
    pts = p.json()["series"][0]["points"]
    assert len(pts) == 1000
    assert pts[0]["t"] == T0 and pts[-1]["t"] == T0 + 2499 * 60
    # un max_points mayor al tope no lo supera
    p = client.get(
        H,
        params={
            "start": T0,
            "end": T0 + 3 * 86400,
            "interval": "raw",
            "sources": "yadio",
            "max_points": 5000,
        },
    )
    assert len(p.json()["series"][0]["points"]) == 1000


def test_downsample_exact_count_and_endpoints():
    from app.aggregation import downsample

    ticks = [Tick("yadio", T0 + i, 1.0) for i in range(25_000)]
    for m in (2, 3, 7, 20_000, 24_999):
        out = downsample(ticks, m)
        assert len(out) == m and out[0].ts == T0 and out[-1].ts == T0 + 24_999
        assert len({t.ts for t in out}) == m
    assert [t.ts for t in downsample(ticks, 1)] == [T0 + 24_999]
    assert downsample(ticks[:5], 10) == ticks[:5]


def test_raw_max_points_must_be_positive():
    with pytest.raises(ValueError):
        Settings(_env_file=None, raw_max_points=0)


def test_collect_not_skipped_when_all_sources_fail(client, monkeypatch):
    from app.collector import collector
    from app.sources.base import RateSource

    class Boom(RateSource):
        name, label, unit = "boom", "b", "USD"

        async def fetch(self, c):
            raise SourceError("caída")

    monkeypatch.setattr(collector, "sources", [Boom()])
    monkeypatch.setattr(collector, "last_run", None)
    r = client.post("/api/v1/rates/collect")
    assert r.status_code == 200 and r.json()["collected"] == [] and r.json()["skipped"] is False


def test_raw_default_cap_is_20000():
    assert Settings(_env_file=None).raw_max_points == 20_000


def test_filename_sanitised(client):
    r = client.get(H + ".csv", params={"days": 5, "filename": 'a";x=y'})
    assert r.headers["content-disposition"] == 'attachment; filename="a_x_y.csv"'


def test_filename_empty_after_sanitising_falls_back(client):
    r = client.get(H + ".csv", params={"days": 5, "filename": '"""'})
    assert 'filename="ves_rates_day_' in r.headers["content-disposition"]


def test_filename_length_capped(client):
    r = client.get(H + ".csv", params={"days": 5, "filename": "a" * 500})
    assert r.headers["content-disposition"] == f'attachment; filename="{"a" * 120}.csv"'


def test_unicode_digit_start_422(client):
    assert client.get(H, params={"start": "²"}).status_code == 422


def test_empty_sep_and_decimal_422(client):
    assert client.get(H + ".csv", params={"sep": ""}).status_code == 422
    assert client.get(H + ".csv", params={"decimal": ""}).status_code == 422


def _bcv(fecha):
    def handler(req):
        return httpx.Response(200, json={"promedio": 832.49, "fechaActualizacion": fecha})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("fecha", ["x", "", None])
def test_bcv_bad_or_missing_date_raises(fecha):
    with pytest.raises(SourceError):
        asyncio.run(BCVSource().fetch(_bcv(fecha)))


def test_bcv_naive_date_uses_local_tz():
    tick = asyncio.run(BCVSource().fetch(_bcv("2026-09-11T00:00:00")))
    assert tick.ts == 1789099200  # 2026-09-11 00:00 -04:00


def test_source_error_truncates_upstream_body():
    big = {"junk": "x" * 5000}

    def handler(req):
        return httpx.Response(200, json=big)

    for src in (YadioSource(), BCVSource()):
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(SourceError) as ei:
            asyncio.run(src.fetch(c))
        msg = str(ei.value)
        run = max(len(m) for m in __import__("re").findall(r"x+", msg))
        assert 100 < run <= 200


def test_collector_status_false_after_task_died():
    async def scenario():
        col = Collector(sources=[], interval=60)
        col.start()
        assert col.status()["enabled"] is True
        col._task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return col.status()["enabled"]

    assert asyncio.run(scenario()) is False


def test_collector_interval_min_30():
    with pytest.raises(ValueError):
        Settings(_env_file=None, collector_interval_seconds=5)


def test_collector_loop_logs_in_progress_at_info(caplog):
    import logging

    from app.collector import CollectInProgress

    async def scenario():
        col = Collector(sources=[], interval=60)

        async def busy():
            col._stop.set()
            raise CollectInProgress("x")

        col.collect_once = busy
        await col._loop()

    with caplog.at_level(logging.INFO, logger="rates.collector"):
        asyncio.run(scenario())
    recs = [
        r
        for r in caplog.records
        if "en curso" in r.getMessage().lower() or "progress" in r.getMessage().lower()
    ]
    assert recs and all(r.levelno == logging.INFO and r.exc_info is None for r in recs)


def test_collect_reports_skipped(client, monkeypatch):
    from app.collector import collector

    monkeypatch.setattr(collector, "last_run", __import__("time").time())
    r = client.post("/api/v1/rates/collect")
    assert r.status_code == 200 and r.json()["skipped"] is True and r.json()["collected"] == []


def test_seed_demo_refuses_non_empty_db(monkeypatch):
    mem = Database(":memory:")
    mem.insert_ticks([Tick("yadio", T0, 50.0)])
    dbmod.set_db(mem)
    from scripts import seed_demo

    monkeypatch.setattr(sys, "argv", ["seed_demo", "--days", "1"])
    try:
        with pytest.raises(SystemExit) as ei:
            seed_demo.main()
        assert ei.value.code not in (0, None)
        assert len(mem.sources()) == 1 and mem.sources()[0]["n"] == 1
        monkeypatch.setattr(sys, "argv", ["seed_demo", "--days", "1", "--force"])
        seed_demo.main()
        assert mem.sources()[0]["n"] > 1 or len(mem.sources()) > 1
    finally:
        dbmod.set_db(None)


def test_seed_demo_runs_on_empty_db(monkeypatch):
    dbmod.set_db(Database(":memory:"))
    from scripts import seed_demo

    monkeypatch.setattr(sys, "argv", ["seed_demo", "--days", "2"])
    try:
        seed_demo.main()
        assert dbmod.get_db().sources()
    finally:
        dbmod.set_db(None)


@pytest.mark.parametrize(
    "params",
    [
        {"end": "9999-12-31"},  # OverflowError al sumar el día inclusivo
        {"end": "99999999999999"},  # epoch fuera de rango para local_iso
        {"start": "2025-01-01", "end": "9999-12-31T00:00:00"},
        {"start": "0", "end": "2025-01-01"},
    ],
)
@pytest.mark.parametrize("path", [H, H + ".csv"])
def test_out_of_range_dates_422(client, path, params):
    r = client.get(path, params=params)
    assert r.status_code == 422
    assert "fecha fuera de rango" in r.json()["detail"].lower()
