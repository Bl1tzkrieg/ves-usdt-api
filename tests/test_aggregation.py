"""rollup (promedio ponderado), downsample y parse_instant."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import aggregation as agg
from app.aggregation import Candle, Interval
from app.db import Tick
from app.series import InvalidQuery, parse_instant


def _day(d: str, *, o, h, lo, c, avg, n) -> Candle:
    ts = agg.local_midnight(datetime.fromisoformat(d).date())
    return Candle(t=ts, date=agg.local_iso(ts), open=o, high=h, low=lo, close=c, avg=avg, count=n)


def test_rollup_week_weighted_average():
    # lunes 2025-01-06 y martes 2025-01-07 caen en la misma semana
    daily = [
        _day("2025-01-06", o=10, h=12, lo=9, c=11, avg=10.0, n=1),
        _day("2025-01-07", o=11, h=15, lo=8, c=14, avg=20.0, n=3),
    ]
    [week] = agg.rollup(daily, Interval.week)
    assert week.avg == pytest.approx((10.0 * 1 + 20.0 * 3) / 4)  # no (10+20)/2
    assert (week.open, week.close, week.high, week.low, week.count) == (10, 14, 15, 8, 4)
    assert week.date[:10] == "2025-01-06"


def test_rollup_splits_weeks_and_day_is_identity():
    daily = [
        _day("2025-01-05", o=1, h=1, lo=1, c=1, avg=1.0, n=1),  # domingo: semana anterior
        _day("2025-01-06", o=2, h=2, lo=2, c=2, avg=2.0, n=1),
    ]
    assert [c.date[:10] for c in agg.rollup(daily, Interval.week)] == ["2024-12-30", "2025-01-06"]
    assert agg.rollup(daily, Interval.day) is daily


def test_downsample_keeps_first_and_last():
    ticks = [Tick("bcv", 1000 + i, 1.0 + i) for i in range(1000)]
    out = agg.downsample(ticks, 50)
    assert len(out) == 50
    assert out[0] is ticks[0] and out[-1] is ticks[-1]
    assert [t.ts for t in out] == sorted({t.ts for t in out})  # orden y sin repetidos


def test_downsample_short_series_untouched():
    ticks = [Tick("bcv", 1000 + i, 1.0) for i in range(5)]
    assert agg.downsample(ticks, 50) is ticks


def test_parse_instant_epoch():
    assert parse_instant("1735704000", inclusive_day_end=False) == 1_735_704_000


def test_parse_instant_iso_with_zone():
    expected = int(datetime(2025, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))).timestamp())
    assert parse_instant("2025-01-01T12:00:00+02:00", inclusive_day_end=False) == expected
    assert parse_instant("2025-01-01T10:00:00Z", inclusive_day_end=False) == expected


def test_parse_instant_date_is_local_and_inclusive_end():
    start = parse_instant("2025-01-01", inclusive_day_end=False)
    end = parse_instant("2025-01-01", inclusive_day_end=True)
    assert start == 1_735_704_000  # 00:00 -04:00
    assert end - start == 86400


def test_parse_instant_rejects_garbage():
    with pytest.raises(InvalidQuery):
        parse_instant("ayer", inclusive_day_end=False)
