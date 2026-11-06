"""Pruebas de la capa de base de datos: validación de Tick y reemplazo transaccional."""
from __future__ import annotations

import sqlite3

import pytest

from app.db import Database, Tick


@pytest.mark.parametrize(
    "ts,price",
    [(0, 1.0), (4_102_444_800, 1.0), (1, 0.0), (1, -1.0), (1, float("inf")), (1, float("nan"))],
)
def test_tick_rejects_invalid(ts, price):
    with pytest.raises(ValueError):
        Tick("bcv", ts, price)


def test_tick_accepts_valid():
    assert Tick("bcv", 1_700_000_000, 36.5).price == 36.5


def test_replace_ticks_replaces_range_per_source():
    db = Database(":memory:")
    db.insert_ticks([Tick("bcv", 100, 1.0), Tick("bcv", 200, 2.0), Tick("bcv", 300, 3.0), Tick("yadio", 150, 9.0)])
    inserted = db.replace_ticks([Tick("bcv", 100, 5.0), Tick("bcv", 200, 6.0)])
    assert inserted == 2
    assert [(t.ts, t.price) for t in db.raw("bcv", 0, 10**9)] == [(100, 5.0), (200, 6.0), (300, 3.0)]
    assert [t.ts for t in db.raw("yadio", 0, 10**9)] == [150]


def test_replace_ticks_is_atomic():
    db = Database(":memory:")
    db.insert_ticks([Tick("bcv", 100, 1.0), Tick("bcv", 200, 2.0)])
    bad = [Tick("bcv", 100, 3.0), Tick("bcv", 200, 4.0)]  # el DELETE cubre las filas existentes

    class FailingConn:
        """Envuelve la conexión: executemany falla, el resto delega."""

        def __init__(self, conn):
            self._c = conn

        def executemany(self, *a, **k):
            raise sqlite3.OperationalError("disk full")

        def __enter__(self):
            return self._c.__enter__()

        def __exit__(self, *exc):
            return self._c.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._c, name)

    real = db._conn
    db._conn = FailingConn(real)
    with pytest.raises(sqlite3.OperationalError):
        db.replace_ticks(bad)
    db._conn = real
    assert [(t.ts, t.price) for t in db.raw("bcv", 0, 10**9)] == [(100, 1.0), (200, 2.0)]
