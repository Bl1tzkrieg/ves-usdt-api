"""Exportación CSV: formato numérico, filas wide alineadas y emisión por bloques."""

from app.aggregation import CandleField, Interval
from app.csv_export import csv_chunks, export_filename, format_number, wide_rows

DAY = 86400
T0 = 1_735_704_000  # 2025-01-01 00:00 -04:00


def candle(t: int, close: float) -> dict:
    return {
        "t": t,
        "date": "",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "avg": close,
        "count": 1,
        "filled": False,
    }


def test_format_number():
    assert format_number(951.7375, ",") == "951,7375"
    assert format_number(951.7375, ".") == "951.7375"
    assert format_number(36.0, ",") == "36"
    assert format_number(None, ",") == ""


def test_wide_rows_aligns_by_timestamp():
    data = {
        "binance_p2p": [candle(T0, 1.5), candle(T0 + DAY, 2.0)],
        "bcv": [candle(T0 + DAY, 3.25), candle(T0 + 2 * DAY, 4.0)],
    }
    rows = list(wide_rows(data, Interval.day, CandleField.close, ","))
    assert rows == [
        ["date", "timestamp", "binance_p2p", "bcv"],
        ["2025-01-01", T0, "1,5", ""],
        ["2025-01-02", T0 + DAY, "2", "3,25"],
        ["2025-01-03", T0 + 2 * DAY, "", "4"],
    ]


def test_csv_chunks_splits_by_row_count():
    chunks = list(csv_chunks(([i, i] for i in range(5)), ";", chunk_rows=2))
    assert chunks == ["0;0\n1;1\n", "2;2\n3;3\n", "4;4\n"]


def test_export_filename_sanitized_or_derived():
    assert export_filename("my file/../x.csv", Interval.day, T0, T0 + DAY) == "my_file_.._x.csv"
    assert export_filename("../", Interval.week, T0, T0 + DAY) == "ves_rates_week_2025-01-01_2025-01-01.csv"
