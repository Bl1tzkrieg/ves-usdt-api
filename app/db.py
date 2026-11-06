"""Acceso a SQLite. Una sola tabla de ticks; los agregados se calculan al consultar.

Un *tick* es una observación puntual: (fuente, timestamp UTC en segundos, precio VES por 1 USD/USDT).
Con esto se puede reconstruir cualquier agregación (hora, día, semana, mes) sobre cualquier rango
sin límite de días: el costo es proporcional a la cantidad de ticks en el rango, no al rango en sí.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id      INTEGER PRIMARY KEY,
    source  TEXT    NOT NULL,
    ts      INTEGER NOT NULL,           -- epoch UTC (segundos)
    price   REAL    NOT NULL,           -- VES por 1 unidad (USDT o USD)
    extra   TEXT,                       -- JSON opcional (compra/venta, nº de anuncios, etc.)
    UNIQUE (source, ts)
);
CREATE INDEX IF NOT EXISTS ix_ticks_source_ts ON ticks (source, ts);
"""


MAX_TS = 4_102_444_800  # 2100-01-01 UTC: cota superior razonable para un epoch en segundos


@dataclass(slots=True)
class Tick:
    source: str
    ts: int
    price: float
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not 0 < self.ts < MAX_TS:
            raise ValueError(f"Timestamp fuera de rango (0, {MAX_TS}): {self.ts!r}")
        if not (math.isfinite(self.price) and self.price > 0):
            raise ValueError(f"Precio inválido (debe ser finito y mayor que 0): {self.price!r}")


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.db_path
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ escritura
    def insert_ticks(self, ticks: Iterable[Tick]) -> int:
        rows = [
            (t.source, int(t.ts), float(t.price), json.dumps(t.extra) if t.extra else None) for t in ticks
        ]
        if not rows:
            return 0
        with self._lock, self._conn:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO ticks (source, ts, price, extra) VALUES (?, ?, ?, ?)", rows
            )
            return cur.rowcount

    def replace_ticks(self, ticks: Sequence[Tick]) -> int:
        """Borra el rango [min ts, max ts] de cada fuente e inserta `ticks`, todo en una transacción."""
        rows = [
            (t.source, int(t.ts), float(t.price), json.dumps(t.extra) if t.extra else None) for t in ticks
        ]
        if not rows:
            return 0
        bounds: dict[str, tuple[int, int]] = {}
        for source, ts, _, _ in rows:
            lo, hi = bounds.get(source, (ts, ts))
            bounds[source] = (min(lo, ts), max(hi, ts))
        with self._lock, self._conn:
            for source, (lo, hi) in bounds.items():
                self._conn.execute(
                    "DELETE FROM ticks WHERE source = ? AND ts >= ? AND ts <= ?", (source, lo, hi)
                )
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO ticks (source, ts, price, extra) VALUES (?, ?, ?, ?)", rows
            )
            return cur.rowcount

    # ------------------------------------------------------------------ lectura
    def sources(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT source, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
                   FROM ticks GROUP BY source ORDER BY source"""
            )
            return [dict(r) for r in cur.fetchall()]

    def latest(self, sources: Sequence[str] | None = None) -> list[Tick]:
        sql = """SELECT t.source, t.ts, t.price, t.extra
                 FROM ticks t
                 JOIN (SELECT source, MAX(ts) AS ts FROM ticks {where} GROUP BY source) m
                   ON m.source = t.source AND m.ts = t.ts"""
        params: list[Any] = []
        where = ""
        if sources:
            placeholders = ",".join("?" * len(sources))
            where = f"WHERE source IN ({placeholders})"
            params.extend(sources)
        with self._lock:
            cur = self._conn.execute(sql.format(where=where), params)
            return [_row_to_tick(r) for r in cur.fetchall()]

    def raw(self, source: str, ts_from: int, ts_to: int) -> list[Tick]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT source, ts, price, extra FROM ticks
                   WHERE source = ? AND ts >= ? AND ts < ? ORDER BY ts""",
                (source, ts_from, ts_to),
            )
            return [_row_to_tick(r) for r in cur.fetchall()]

    def last_before(self, source: str, ts: int) -> Tick | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT source, ts, price, extra FROM ticks "
                "WHERE source = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                (source, ts),
            )
            r = cur.fetchone()
            return _row_to_tick(r) if r else None

    def buckets(
        self, source: str, ts_from: int, ts_to: int, bucket_seconds: int, offset_seconds: int
    ) -> list[dict[str, Any]]:
        """Agrega ticks en cubetas de tamaño fijo (hora, día) alineadas a la zona horaria local.

        La cubeta se identifica por su inicio en epoch UTC. Devuelve OHLC + promedio + conteo.
        Semana y mes se arman en Python a partir de las cubetas diarias (ver aggregation.py).
        """
        sql = """
        WITH t AS (
            SELECT ts, price,
                   ((ts + :off) / :size) * :size - :off AS bucket
            FROM ticks
            WHERE source = :source AND ts >= :from AND ts < :to
        ),
        ranked AS (
            SELECT bucket, price,
                   ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY ts ASC)  AS rn_first,
                   ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY ts DESC) AS rn_last
            FROM t
        ),
        stats AS (
            SELECT bucket, MIN(price) AS low, MAX(price) AS high, AVG(price) AS avg, COUNT(*) AS n
            FROM t GROUP BY bucket
        )
        SELECT s.bucket, s.low, s.high, s.avg, s.n,
               (SELECT price FROM ranked r WHERE r.bucket = s.bucket AND r.rn_first = 1) AS open,
               (SELECT price FROM ranked r WHERE r.bucket = s.bucket AND r.rn_last  = 1) AS close
        FROM stats s ORDER BY s.bucket
        """
        with self._lock:
            cur = self._conn.execute(
                sql,
                {
                    "source": source,
                    "from": ts_from,
                    "to": ts_to,
                    "size": bucket_seconds,
                    "off": offset_seconds,
                },
            )
            return [dict(r) for r in cur.fetchall()]


def _row_to_tick(r: sqlite3.Row) -> Tick:
    extra = json.loads(r["extra"]) if r["extra"] else None
    return Tick(source=r["source"], ts=int(r["ts"]), price=float(r["price"]), extra=extra)


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database | None) -> None:
    """Para pruebas: inyectar una base en memoria."""
    global _db
    _db = db
