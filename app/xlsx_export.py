"""Exportación a Excel (.xlsx) nativo: hoja `Resumen` con una columna por fuente y gráfico, más una hoja
por fuente con el detalle (OHLC o, en raw, precio/compra/venta)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from . import aggregation as agg
from .aggregation import CandleField, Interval
from .config import settings
from .csv_export import Series, align_by_t

if TYPE_CHECKING:  # módulo privado de openpyxl: solo para anotar, nunca en tiempo de ejecución
    from openpyxl.worksheet._write_only import WriteOnlyWorksheet as Sheet

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SUMMARY_SHEET = "Resumen"
PRICE_FORMAT = "#,##0.0000"
COUNT_FORMAT = "0"
SHEET_NAME_MAX_LEN = 31  # límite de Excel
HEADER_FONT = Font(bold=True)
MAX_CHART_ROWS = 5000  # más filas y Excel se arrastra al redibujar el gráfico; los datos siguen completos

CANDLE_FIELDS = ("open", "high", "low", "close", "avg")
RAW_EXTRA_FIELDS = ("buy", "sell")


def build_workbook(data: Series, names: list[str], interval: Interval, field: CandleField) -> bytes:
    """Libro .xlsx en memoria a partir de la salida de `series.build_series` por fuente.

    `Resumen`: fecha + una columna por fuente (en raw, el precio; si no, el campo `field` de la vela),
    alineadas por `t`, con cabecera `fuente (campo)`; sin gráfico si pasa de `MAX_CHART_ROWS` filas.
    Luego una hoja por fuente. Fechas como fecha real de Excel en hora local, sin zona.
    Modo `write_only`: las filas se vuelcan al escribirse, sin un objeto por celda en memoria.
    """
    wb = Workbook(write_only=True)
    summary = wb.create_sheet(SUMMARY_SHEET)
    ordered = {n: data[n] for n in names}
    raw = interval is Interval.raw
    n_rows = _write_summary(summary, ordered, interval, "price" if raw else field.value)
    if 0 < n_rows <= MAX_CHART_ROWS:
        _add_chart(summary, len(names), n_rows + 1, interval, field)
    for name in names:
        ws = wb.create_sheet(_sheet_name(name))
        if raw:
            _write_raw_sheet(ws, ordered[name])
        else:
            _write_candle_sheet(ws, ordered[name], interval)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet_name(source: str) -> str:
    return source[:SHEET_NAME_MAX_LEN]


def _date_format(interval: Interval) -> str:
    if interval is Interval.raw:
        return "yyyy-mm-dd hh:mm:ss"
    return "yyyy-mm-dd hh:mm" if interval is Interval.hour else "yyyy-mm-dd"


def _excel_date(ts: int) -> datetime:
    """Excel no admite zona horaria: se guarda la hora local (`LOCAL_TZ`) sin `tzinfo`."""
    return agg.to_local(ts).replace(tzinfo=None)


def _write_table(
    ws: Sheet,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    interval: Interval,
    formats: Sequence[str],
) -> None:
    """Cabecera en negrita y congelada, autofiltro, anchos; `formats` aplica a las columnas B en adelante.

    En `write_only` la vista, los anchos y el autofiltro se fijan antes de la primera fila.
    """
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    ws.column_dimensions["A"].width = 12 if interval in (Interval.day, Interval.week, Interval.month) else 20
    for i, header in enumerate(headers[1:], start=2):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 4)
    ws.append([_cell(ws, h, font=HEADER_FONT) for h in headers])
    date_format = _date_format(interval)
    for row in rows:
        ws.append(
            [
                _cell(ws, row[0], number_format=date_format),
                *(_cell(ws, v, number_format=fmt) for v, fmt in zip(row[1:], formats, strict=True)),
            ]
        )


def _cell(ws: Sheet, value: Any, *, number_format: str | None = None, font: Font | None = None):
    cell = WriteOnlyCell(ws, value=value)
    if number_format:
        cell.number_format = number_format
    if font:
        cell.font = font
    return cell


def _write_summary(ws: Sheet, data: Series, interval: Interval, key: str) -> int:
    """Escribe `Resumen` y devuelve cuántas filas de datos tiene."""
    rows = [[_excel_date(t), *values] for t, values in align_by_t(data, key)]
    _write_table(
        ws, ["date", *(f"{name} ({key})" for name in data)], rows, interval, [PRICE_FORMAT] * len(data)
    )
    return len(rows)


def _write_candle_sheet(ws: Sheet, candles: Sequence[dict[str, Any]], interval: Interval) -> None:
    rows = [[_excel_date(c["t"]), *(c[k] for k in CANDLE_FIELDS), c["count"]] for c in candles]
    formats = [PRICE_FORMAT] * len(CANDLE_FIELDS) + [COUNT_FORMAT]
    _write_table(ws, ["date", *CANDLE_FIELDS, "count"], rows, interval, formats)


def _write_raw_sheet(ws: Sheet, points: Sequence[dict[str, Any]]) -> None:
    rows = [
        [_excel_date(p["t"]), p["price"], *((p.get("extra") or {}).get(k) for k in RAW_EXTRA_FIELDS)]
        for p in points
    ]
    formats = [PRICE_FORMAT] * (1 + len(RAW_EXTRA_FIELDS))
    _write_table(ws, ["date", "price", *RAW_EXTRA_FIELDS], rows, Interval.raw, formats)


def _add_chart(ws: Sheet, n_sources: int, last_row: int, interval: Interval, field: CandleField) -> None:
    """Gráfico de líneas nativo sobre las columnas de `Resumen` (sin imágenes: Excel lo recalcula)."""
    chart = LineChart()
    what = "precio" if interval is Interval.raw else f"{field.value} por {interval.value}"
    chart.title = f"VES por unidad: {what} (UTC{settings.tz_offset_hours:+d})"
    chart.y_axis.title = "Bs"
    chart.y_axis.number_format = "#,##0"
    chart.x_axis.number_format = _date_format(interval)
    # openpyxl ≥ 3.1 marca los ejes como borrados por defecto; sin esto Excel no los dibuja
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.display_blanks = "span"  # une la línea sobre huecos (fines de semana del BCV, ticks no alineados)
    chart.height, chart.width = 10, 24
    chart.add_data(
        Reference(ws, min_col=2, max_col=1 + n_sources, min_row=1, max_row=last_row), titles_from_data=True
    )
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last_row))
    ws.add_chart(chart, f"{get_column_letter(n_sources + 3)}2")
