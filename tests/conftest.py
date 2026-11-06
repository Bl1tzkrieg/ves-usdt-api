"""Fixtures compartidos: BD en memoria con 10 días de ticks simulados desde `T0`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.db import Database, Tick
from app.main import app

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
