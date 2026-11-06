"""Pruebas de `/history.xlsx`: hojas, cabeceras, fechas reales, formatos y gráfico nativo."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.aggregation import CandleField, Interval
from app.xlsx_export import PRICE_FORMAT, build_workbook

XLSX = "/api/v1/rates/history.xlsx"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RANGE = {"start": "2025-01-01", "end": "2025-01-10", "sources": "binance_p2p,bcv"}


def _workbook(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == XLSX_TYPE
    return load_workbook(BytesIO(resp.content))


def _row(ws, n: int) -> list:
    return [c.value for c in ws[n]]


def test_xlsx_day_summary_sheet(client):
    wb = _workbook(client.get(XLSX, params=RANGE))
    assert wb.sheetnames == ["Resumen", "binance_p2p", "bcv"]
    ws = wb["Resumen"]
    assert _row(ws, 1) == ["date", "binance_p2p", "bcv"]
    assert all(c.font.bold for c in ws[1])
    assert isinstance(ws["A2"].value, datetime)
    assert ws["A2"].value == datetime(2025, 1, 1)  # medianoche local, sin zona
    assert ws["A2"].number_format == "yyyy-mm-dd"
    assert ws["B2"].value == pytest.approx(100.20)  # close del día 0
    assert ws["C2"].value == 90
    assert ws["B2"].number_format == PRICE_FORMAT == "#,##0.0000"
    assert ws.max_row == 11  # cabecera + 10 días
    assert ws["C7"].value is None  # BCV sin dato el sábado 2025-01-06
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:C11"
    assert ws.column_dimensions["A"].width >= 10
    assert len(ws._charts) == 1


def test_xlsx_per_source_sheet_has_ohlc(client):
    ws = _workbook(client.get(XLSX, params=RANGE))["binance_p2p"]
    assert _row(ws, 1) == ["date", "open", "high", "low", "close", "avg", "count"]
    date, o, h, lo, c, avg, count = _row(ws, 2)
    assert date == datetime(2025, 1, 1)
    assert (o, h, lo, c) == pytest.approx((100.08, 100.20, 100.08, 100.20))
    assert avg == pytest.approx(100.14)
    assert count == 4
    assert ws["B2"].number_format == PRICE_FORMAT
    assert ws.freeze_panes == "A2"


def test_xlsx_value_selects_candle_field(client):
    ws = _workbook(client.get(XLSX, params={**RANGE, "value": "avg"}))["Resumen"]
    assert ws["B2"].value == pytest.approx(100.14)


def test_xlsx_raw_interval(client):
    params = {"start": "2025-01-01", "end": "2025-01-01", "sources": "binance_p2p", "interval": "raw"}
    wb = _workbook(client.get(XLSX, params=params))
    summary = wb["Resumen"]
    assert _row(summary, 1) == ["date", "binance_p2p"]
    assert summary["A2"].value == datetime(2025, 1, 1, 8)
    assert summary["A2"].number_format == "yyyy-mm-dd hh:mm:ss"
    assert summary["B2"].value == pytest.approx(100.08)
    assert summary.max_row == 5
    assert len(summary._charts) == 1
    ws = wb["binance_p2p"]
    assert _row(ws, 1) == ["date", "price", "buy", "sell"]
    assert _row(ws, 2)[:2] == [datetime(2025, 1, 1, 8), pytest.approx(100.08)]


def test_xlsx_unknown_source_is_422(client):
    assert client.get(XLSX, params={"sources": "nope"}).status_code == 422


def test_xlsx_range_cap_is_422(client):
    assert client.get(XLSX, params={"days": 100_000}).status_code == 422


def test_xlsx_filename(client):
    r = client.get(XLSX, params=RANGE)
    assert r.headers["content-disposition"] == (
        'attachment; filename="ves_rates_day_2025-01-01_2025-01-10.xlsx"'
    )
    r = client.get(XLSX, params={**RANGE, "filename": 'mi "reporte"/../x'})
    assert r.headers["content-disposition"] == 'attachment; filename="mi_reporte_.._x.xlsx"'


def test_xlsx_route_is_in_openapi(client):
    # static/app.js activa el botón Excel solo si la ruta aparece en el esquema
    assert XLSX in client.get("/openapi.json").json()["paths"]


def test_build_workbook_without_data_has_headers_and_no_chart():
    wb = load_workbook(BytesIO(build_workbook({"bcv": []}, ["bcv"], Interval.day, CandleField.close)))
    assert wb.sheetnames == ["Resumen", "bcv"]
    assert _row(wb["Resumen"], 1) == ["date", "bcv"]
    assert wb["Resumen"]._charts == []
