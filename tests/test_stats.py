"""Pruebas de `/stats` y `/daily-change` con el fixture de 10 días de `conftest.py`."""

from __future__ import annotations

import pytest
from conftest import T0
from fastapi.testclient import TestClient

from app import db as dbmod
from app.db import Database, Tick
from app.main import app

STATS = "/api/v1/rates/stats"
DAILY = "/api/v1/rates/daily-change"


def _by_source(r) -> dict[str, dict]:
    assert r.status_code == 200, r.text
    return {s["source"]: s for s in r.json()}


def test_stats_binance_changes(client):
    stats = _by_source(client.get(STATS, params={"sources": "binance_p2p"}))
    b = stats["binance_p2p"]
    assert b["latest"] == pytest.approx(109.20)
    assert b["latest_date"] == "2025-01-10T20:00:00-04:00"
    c24 = b["change"]["24h"]
    assert c24["from_price"] == pytest.approx(108.20)
    assert c24["from_date"] == "2025-01-09T20:00:00-04:00"
    assert c24["abs"] == pytest.approx(1.0)
    assert c24["pct"] == pytest.approx((109.20 / 108.20 - 1) * 100)
    c7 = b["change"]["7d"]
    assert c7["from_price"] == pytest.approx(102.20)
    assert c7["pct"] == pytest.approx((109.20 / 102.20 - 1) * 100)
    assert b["change"]["30d"] is None  # solo hay 10 días de historia
    assert b["spread_vs_reference_pct"] == pytest.approx((109.20 / 99 - 1) * 100)


def test_stats_default_is_all_sources_and_reference_has_no_spread(client):
    stats = _by_source(client.get(STATS))
    assert set(stats) == {"binance_p2p", "bcv"}  # fuentes sin datos se omiten
    bcv = stats["bcv"]
    assert bcv["spread_vs_reference_pct"] is None
    assert bcv["latest"] == 99
    # día 9 menos 24h: el tick del día 8 está exactamente 24h antes y cuenta
    assert bcv["change"]["24h"]["from_price"] == 98
    assert bcv["change"]["24h"]["from_date"] == "2025-01-09T00:00:00-04:00"


def test_stats_tick_exactly_window_ago_counts(client):
    # binance día 9 20h − 7 días = día 2 20h exactos
    b = _by_source(client.get(STATS, params={"sources": "binance_p2p"}))["binance_p2p"]
    assert b["change"]["7d"]["from_date"] == "2025-01-03T20:00:00-04:00"


def test_stats_alternate_reference(client):
    stats = _by_source(client.get(STATS, params={"sources": "bcv,binance_p2p", "reference": "binance_p2p"}))
    assert stats["binance_p2p"]["spread_vs_reference_pct"] is None
    assert stats["bcv"]["spread_vs_reference_pct"] == pytest.approx((99 / 109.20 - 1) * 100)


def test_stats_reference_without_data_gives_none(client):
    stats = _by_source(client.get(STATS, params={"sources": "binance_p2p", "reference": "yadio"}))
    assert stats["binance_p2p"]["spread_vs_reference_pct"] is None


def test_stats_unknown_source_or_reference_422(client):
    assert client.get(STATS, params={"sources": "nope"}).status_code == 422
    assert client.get(STATS, params={"reference": "nope"}).status_code == 422
    assert client.get(STATS, params={"reference": "bcv,binance_p2p"}).status_code == 422


@pytest.fixture()
def day0_client(monkeypatch):
    """Solo el día 0: binance cierra en 100.20 y BCV está en 90."""
    monkeypatch.setattr("app.config.settings.collector_enabled", False)
    mem = Database(":memory:")
    dbmod.set_db(mem)
    ticks = [Tick("binance_p2p", T0 + h * 3600, 100 + h / 100) for h in (8, 12, 16, 20)]
    ticks.append(Tick("bcv", T0, 90))
    mem.insert_ticks(ticks)
    with TestClient(app) as c:
        yield c
    dbmod.set_db(None)


def test_stats_day0_spread_and_no_history(day0_client):
    stats = _by_source(day0_client.get(STATS))
    b = stats["binance_p2p"]
    assert b["spread_vs_reference_pct"] == pytest.approx(11.33, abs=0.005)
    assert b["change"] == {"24h": None, "7d": None, "30d": None}
    assert stats["bcv"]["change"] == {"24h": None, "7d": None, "30d": None}


def test_daily_change_binance(client):
    r = client.get(DAILY, params={"source": "binance_p2p", "days": 3650})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 9  # 10 cierres diarios → 9 variaciones
    assert rows[0]["date"] == "2025-01-02"
    assert rows[0]["pct"] == pytest.approx((101.20 / 100.20 - 1) * 100)
    assert rows[-1]["date"] == "2025-01-10"
    assert rows[-1]["pct"] == pytest.approx((109.20 / 108.20 - 1) * 100)


def test_daily_change_skips_days_without_data(client):
    rows = client.get(DAILY, params={"source": "bcv", "days": 3650}).json()
    assert [r["date"] for r in rows] == [
        "2025-01-02",
        "2025-01-03",
        "2025-01-04",
        "2025-01-05",
        "2025-01-08",
        "2025-01-09",
        "2025-01-10",
    ]
    assert rows[4]["pct"] == pytest.approx((97 / 94 - 1) * 100)  # día 7 contra día 4 (5 y 6 sin datos)


def test_daily_change_validation(client):
    assert client.get(DAILY, params={"source": "nope"}).status_code == 422
    assert client.get(DAILY, params={"source": "binance_p2p,bcv"}).status_code == 422
    assert client.get(DAILY, params={"days": 0}).status_code == 422
    assert client.get(DAILY, params={"days": 100000}).status_code == 422  # tope de rango


def test_daily_change_default_window_is_recent(client):
    r = client.get(DAILY)  # binance_p2p, últimos 30 días: el fixture es de 2025 → vacío
    assert r.status_code == 200 and r.json() == []
