"""Parser compartido de ticks (API /import y scripts/backfill)."""
from __future__ import annotations

import csv

import pytest

from app.tick_parsing import parse_price, parse_ts, rows_to_ticks, sniff_dialect

# 2020-01-01 00:00 -04:00
JAN1 = 1_577_851_200


@pytest.mark.parametrize(
    "value, expected",
    [
        (1_577_851_200, 1_577_851_200),
        (1_577_851_200_000, 1_577_851_200),  # milisegundos
        ("1577851200000", 1_577_851_200),
        ("1577851200.5", 1_577_851_200),
        ("2020-01-01", JAN1),
        ("2020-01-01T00:00:00-04:00", JAN1),
        ("2020-01-01T04:00:00Z", JAN1),
        ("2020-01-01 00:00:00", JAN1),  # sin zona: hora local
        ("01-01-2020", JAN1),
        ("01/01/2020", JAN1),
        ("abc", None),
        ("", None),
        (None, None),
        (float("nan"), None),
        ("13/01/2020", JAN1 + 12 * 86400),  # DD/MM
        ("01/13/2020", None),  # MM/DD no se adivina
    ],
)
def test_parse_ts(value, expected):
    assert parse_ts(value) == expected


def test_parse_ts_mm_dd_only_with_explicit_fmt():
    assert parse_ts("01/13/2020", fmt="%m/%d/%Y") == JAN1 + 12 * 86400
    assert parse_ts("13/01/2020", fmt="%m/%d/%Y") is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1,5", 1.5),
        ("5.5", 5.5),
        ("Bs 950", 950.0),
        ("Bs. 950", 950.0),
        ("Bs.950", 950.0),
        ("1 234,56", 1234.56),
        ("1\xa0234,56", 1234.56),
        ("0.125", 0.125),
        # ambiguos (agrupación de miles vs decimal): no se adivina
        ("1.234", None),
        ("1,234", None),
        ("Bs 1.234", None),
        ("1.234.567", None),
        ("1,234,567", None),
        (12, 12.0),
        ("abc", None),
        ("", None),
        (None, None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_parse_price(value, expected):
    assert parse_price(value) == expected


def test_sniff_dialect_semicolon():
    assert sniff_dialect("fecha;precio\n2020-01-01;5,5\n").delimiter == ";"
    assert issubclass(sniff_dialect("sin delimitador"), csv.Dialect)


def test_rows_to_ticks_ok_and_extra():
    res = rows_to_ticks(
        [{"Date": "2020-01-01", "Price": "1.234,56", "buy": "1,5", "sell": "2"}],
        default_source="bcv",
        allowed_sources={"bcv"},
    )
    assert res.rejected == []
    (t,) = res.ticks
    assert (t.source, t.ts, t.price, t.extra) == ("bcv", JAN1, 1234.56, {"buy": 1.5, "sell": 2.0})


def test_rows_to_ticks_rejects_with_reason_and_row_number():
    res = rows_to_ticks(
        [
            {"fecha": "2020-01-01", "precio": "5"},
            {"fecha": "2020-01-02", "precio": ""},  # sin precio
            {"fecha": "nunca", "precio": "5"},  # fecha mala
            {"fecha": "2020-01-04", "precio": "0"},  # precio no positivo
            {"fecha": "2020-01-05", "precio": "nan"},
            {"timestamp": "99999999999999999", "precio": "5"},  # ts absurdo
        ],
        default_source="bcv",
        allowed_sources={"bcv"},
    )
    assert len(res.ticks) == 1
    assert [r["row"] for r in res.rejected] == [2, 3, 4, 5, 6]
    assert all(isinstance(r["reason"], str) and r["reason"] for r in res.rejected)


def test_rows_to_ticks_unknown_source_and_missing_source():
    res = rows_to_ticks(
        [
            {"fecha": "2020-01-01", "precio": "5", "source": "nope"},
            {"fecha": "2020-01-01", "precio": "5", "source": "bcv"},
            {"fecha": "2020-01-01", "precio": "5"},
        ],
        default_source=None,
        allowed_sources={"bcv"},
    )
    assert [t.source for t in res.ticks] == ["bcv"]
    assert [r["row"] for r in res.rejected] == [1, 3]
    assert "nope" in res.rejected[0]["reason"]


def test_rows_to_ticks_explicit_keys_and_pairs():
    res = rows_to_ticks(
        [{"when": "2020-01-01", "val": "7"}, [JAN1 + 86400, 8], ["x"]],
        default_source="bcv",
        ts_key="when",
        price_key="val",
        allowed_sources={"bcv"},
    )
    # el par [ts, precio] no depende de las claves explícitas
    assert [(t.ts, t.price) for t in res.ticks] == [(JAN1, 7.0), (JAN1 + 86400, 8.0)]
    assert [r["row"] for r in res.rejected] == [3]


def test_rows_to_ticks_ambiguous_price_rejected_with_reason():
    res = rows_to_ticks([{"fecha": "2020-01-01", "precio": "1.234"}], default_source="bcv", allowed_sources={"bcv"})
    assert res.ticks == []
    assert res.rejected[0]["row"] == 1 and "precio" in res.rejected[0]["reason"]


def test_rows_to_ticks_prefers_date_over_time_and_falls_back_on_empty():
    res = rows_to_ticks(
        [
            {"date": "2020-01-01", "time": "garbage", "price": "5"},
            {"timestamp": "", "date": "2020-01-02", "price": "6"},
            {"timestamp": "nope", "date": "2020-01-03", "price": "7"},
            {"timestamp": "", "date": "", "price": "8"},
        ],
        default_source="bcv",
        allowed_sources={"bcv"},
    )
    assert [t.ts for t in res.ticks] == [JAN1, JAN1 + 86400, JAN1 + 2 * 86400]
    assert [r["row"] for r in res.rejected] == [4]
    assert "ausente" in res.rejected[0]["reason"]
