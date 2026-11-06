"""Pruebas de `/history.xlsx`: hojas, cabeceras, fechas reales, formatos y gráfico nativo."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.aggregation import CandleField, Interval
from app.xlsx_export import MAX_CHART_ROWS, PRICE_FORMAT, build_workbook

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
    assert _row(ws, 1) == ["date", "binance_p2p (close)", "bcv (close)"]
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
    assert _row(ws, 1) == ["date", "binance_p2p (avg)", "bcv (avg)"]
    assert ws["B2"].value == pytest.approx(100.14)


def test_xlsx_raw_interval(client):
    params = {"start": "2025-01-01", "end": "2025-01-01", "sources": "binance_p2p", "interval": "raw"}
    wb = _workbook(client.get(XLSX, params=params))
    summary = wb["Resumen"]
    assert _row(summary, 1) == ["date", "binance_p2p (price)"]
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
    assert _row(wb["Resumen"], 1) == ["date", "bcv (close)"]
    assert wb["Resumen"]._charts == []


def test_build_workbook_skips_chart_above_row_cap():
    def points(n: int) -> list[dict]:
        return [{"t": 1_735_704_000 + i, "price": 100.0, "extra": None} for i in range(n)]

    at_cap = build_workbook({"bcv": points(MAX_CHART_ROWS)}, ["bcv"], Interval.raw, CandleField.close)
    assert len(load_workbook(BytesIO(at_cap))["Resumen"]._charts) == 1
    above = build_workbook({"bcv": points(MAX_CHART_ROWS + 1)}, ["bcv"], Interval.raw, CandleField.close)
    ws = load_workbook(BytesIO(above))["Resumen"]
    assert ws._charts == []
    assert ws.max_row == MAX_CHART_ROWS + 2


def test_xlsx_over_cell_cap_is_422(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "export_max_cells", 1_000)
    r = client.get(XLSX, params={**RANGE, "interval": "raw"})  # ~44 puntos: cabe
    assert r.status_code == 200
    r = client.get(XLSX, params={**RANGE, "interval": "hour", "fill": "forward"})  # 240 horas x 2 x 7
    assert r.status_code == 422
    assert "history.csv" in r.text
    assert "el tope es 1.000." in r.json()["detail"]  # separador de miles venezolano


def test_heavy_builds_are_serialized(client, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import app.routers.history as hist

    lock, state = threading.Lock(), {"now": 0, "peak": 0}
    real = hist.build_workbook

    def slow(*args):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.15)
        try:
            return real(*args)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(hist, "build_workbook", slow)
    with ThreadPoolExecutor(3) as pool:
        codes = [
            f.result().status_code for f in [pool.submit(client.get, XLSX, params=RANGE) for _ in range(3)]
        ]
    assert codes == [200, 200, 200]
    assert state["peak"] == 1
