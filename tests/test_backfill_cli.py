"""scripts/backfill.py modo `yadio`: solo rellena días locales sin datos, igual que el endpoint."""

from __future__ import annotations

import argparse
from datetime import datetime

import pytest

from app import db as dbmod
from app.aggregation import LOCAL_TZ
from app.backfill_sources import BackfillResult
from app.db import Database, Tick
from scripts import backfill


def _midnight(d: int) -> int:
    return int(datetime(2020, 1, d, tzinfo=LOCAL_TZ).timestamp())


@pytest.fixture()
def mem_db():
    mem = Database(":memory:")
    dbmod.set_db(mem)
    yield mem
    dbmod.set_db(None)


def test_cmd_yadio_skips_days_that_already_have_ticks(mem_db, monkeypatch, capsys):
    collected = Tick("yadio", _midnight(2) + 15 * 3600, 11.7)
    mem_db.insert_ticks([collected])

    async def fake_fetch(client, days, currency="VES", source="yadio"):
        return BackfillResult(ticks=[Tick(source, _midnight(d), 10.0 + d) for d in (1, 2, 3)])

    monkeypatch.setattr(backfill, "fetch_yadio_history", fake_fetch)
    args = argparse.Namespace(source="yadio", days=3, currency="VES", dry_run=False)
    backfill.cmd_yadio(args)

    stored = mem_db.raw("yadio", 0, 4_000_000_000)
    assert [t.ts for t in stored] == [_midnight(1), collected.ts, _midnight(3)]
    assert "1 días ya tenían datos de 'yadio'" in capsys.readouterr().out
