"""Pruebas de `/report.pdf`: validación de `month`, cabeceras, texto del informe y resumen mensual."""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

import pytest
from pypdf import PdfReader

from app import aggregation as agg
from app.pdf_report import (
    SourceStats,
    build_monthly_report,
    chart_points,
    month_bounds,
    parse_month,
    summarize,
)
from app.series import InvalidQuery

PDF = "/api/v1/rates/report.pdf"


def _text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    assert len(reader.pages) >= 1
    return "\n".join(page.extract_text() for page in reader.pages)


def _pdf(resp) -> str:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    return _text(resp.content)


def test_pdf_report_month_with_data(client):
    r = client.get(PDF, params={"month": "2025-01", "sources": "binance_p2p,bcv"})
    text = _pdf(r)
    assert r.headers["content-disposition"] == 'attachment; filename="ves_report_2025-01.pdf"'
    assert "Enero 2025" in text
    assert "Binance P2P" in text and "BCV oficial" in text
    assert "Yadio" not in text
    for header in ("Apertura", "Cierre", "Mínimo", "Máximo", "Variación", "Brecha media"):
        assert header in text
    assert "100,08" in text  # apertura de Binance: primer tick del 1 de enero
    assert "109,20" in text  # cierre y máximo: último tick del día 10
    # Tabla diaria: el BCV no cotiza los días 6 y 7, se repite el cierre del día 5 (94)
    tokens = text.split()
    i = tokens.index("06/01/2025")
    assert tokens[i + 1 : i + 3] == ["105,20", "94,00"]
    # Solo hay datos hasta el día 10: la tabla diaria sigue con el último valor, sin huecos raros
    # La tabla diaria rellena hasta fin de mes (en gris cursiva); el gráfico se corta en el último dato real
    assert "31/01/2025" in tokens


def test_pdf_report_default_sources_are_all_registered(client):
    text = _pdf(client.get(PDF, params={"month": "2025-01"}))
    for label in ("Binance P2P", "Yadio", "BCV oficial", "Paralelo"):
        assert label in text


def test_pdf_report_empty_month_still_returns_pdf(client):
    r = client.get(PDF, params={"month": "2024-06"})
    text = _pdf(r)
    assert "Junio 2024" in text
    assert "No hay datos" in text
    assert r.headers["content-disposition"] == 'attachment; filename="ves_report_2024-06.pdf"'


def test_pdf_report_current_month_is_accepted(client):
    today = agg.to_local(int(datetime.now().timestamp())).date()
    assert client.get(PDF, params={"month": f"{today:%Y-%m}"}).status_code == 200


@pytest.mark.parametrize(
    "month", ["2025-13", "2025-00", "2025-1", "25-01", "enero", "", "2025-01-01", "1969-12"]
)
def test_pdf_report_invalid_month_is_422(client, month):
    assert client.get(PDF, params={"month": month}).status_code == 422


def test_pdf_report_requires_month(client):
    assert client.get(PDF).status_code == 422


def test_pdf_report_future_month_is_422(client):
    today = agg.to_local(int(datetime.now().timestamp())).date()
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    r = client.get(PDF, params={"month": f"{nxt:%Y-%m}"})
    assert r.status_code == 422
    assert "futuro" in r.json()["detail"]


def test_pdf_report_unknown_source_is_422(client):
    assert client.get(PDF, params={"month": "2025-01", "sources": "nope"}).status_code == 422


def test_pdf_route_is_in_openapi(client):
    # static/app.js activa el botón PDF solo si la ruta aparece en el esquema
    assert PDF in client.get("/openapi.json").json()["paths"]


def test_parse_month():
    today = date(2026, 11, 6)
    assert parse_month("2026-10", today) == date(2026, 10, 1)
    assert parse_month("2026-11", today) == date(2026, 11, 1)
    with pytest.raises(InvalidQuery):
        parse_month("2026-12", today)
    with pytest.raises(InvalidQuery):
        parse_month("2026-1", today)


def test_month_bounds_clip_current_month_to_today():
    month = date(2026, 11, 1)
    now = agg.local_midnight(date(2026, 11, 6)) + 3600
    assert month_bounds(month, now) == (agg.local_midnight(month), agg.local_midnight(date(2026, 11, 7)))
    past = date(2026, 2, 1)
    assert month_bounds(past, now) == (agg.local_midnight(past), agg.local_midnight(date(2026, 3, 1)))


def _candle(day: int, o: float, h: float, lo: float, c: float, filled: bool = False) -> dict:
    t = agg.local_midnight(date(2025, 1, day))
    return {"t": t, "open": o, "high": h, "low": lo, "close": c, "filled": filled}


def test_summarize_uses_real_candles_and_mean_gap_vs_reference():
    usdt = [
        _candle(1, 110, 112, 108, 111, filled=True),
        _candle(2, 120, 130, 115, 125),
        _candle(3, 125, 140, 124, 132),
    ]
    bcv = [
        _candle(1, 100, 100, 100, 100),
        _candle(2, 100, 100, 100, 100),
        _candle(3, 110, 110, 110, 110, True),
    ]
    stats = summarize({"binance_p2p": usdt, "bcv": bcv}, reference=bcv)
    usdt_stats, bcv_stats = stats
    assert usdt_stats == SourceStats(
        source="binance_p2p",
        open=120,
        close=132,
        low=115,
        high=140,
        change_pct=pytest.approx(10.0),
        gap_vs_reference_pct=pytest.approx((25 + 20) / 2),  # días 2 y 3; el 1 es relleno
    )
    assert bcv_stats.gap_vs_reference_pct is None  # la referencia contra sí misma no aplica
    assert bcv_stats.open == 100 and bcv_stats.close == 100


def test_summarize_without_data_or_reference():
    (empty,) = summarize({"yadio": []}, reference=None)
    assert empty == SourceStats("yadio", None, None, None, None, None, None)
    (no_ref,) = summarize({"yadio": [_candle(2, 1, 2, 1, 2)]}, reference=None)
    assert no_ref.gap_vs_reference_pct is None and no_ref.change_pct == pytest.approx(100.0)


def test_build_monthly_report_without_data_mentions_month_and_dashes():
    content = build_monthly_report(date(2026, 10, 1), {"bcv": []}, summarize({"bcv": []}, reference=None))
    assert content.startswith(b"%PDF")
    text = _text(content)
    assert "Octubre 2026" in text and "No hay datos" in text


def test_chart_points_drop_trailing_fill_but_keep_interior_gaps():
    stops_early = [
        _candle(1, 1, 1, 1, 10),
        _candle(2, 1, 1, 1, 10, filled=True),  # hueco interior (fin de semana): se conserva
        _candle(3, 1, 1, 1, 12),
        _candle(4, 1, 1, 1, 12, filled=True),  # cola rellena: la fuente dejó de cotizar
        _candle(5, 1, 1, 1, 12, filled=True),
    ]
    only_fill = [_candle(1, 1, 1, 1, 5, filled=True)]
    points = chart_points({"yadio": stops_early, "bcv": only_fill})
    assert [x.day for x, _ in points["yadio"]] == [1, 2, 3]
    assert [y for _, y in points["yadio"]] == [10, 10, 12]
    assert points["bcv"] == []
