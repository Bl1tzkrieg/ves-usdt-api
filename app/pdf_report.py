"""Informe mensual en PDF: portada, tabla resumen por fuente, gráfico de cierres y tabla diaria.

El gráfico se dibuja con matplotlib (backend Agg, sin pyplot ni estado global) a un PNG en memoria y
reportlab lo incrusta. Todo se calcula sobre velas diarias con relleno hacia adelante (`build_series`
con `fill=forward`), así los fines de semana del BCV muestran el último valor oficial vigente.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from statistics import fmean
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import aggregation as agg
from .aggregation import Interval
from .config import settings
from .csv_export import align_by_t
from .series import InvalidQuery
from .sources import REGISTRY
from .stats import pct_change

PDF_MEDIA_TYPE = "application/pdf"
REFERENCE_SOURCE = "bcv"
MONTH_NAMES = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)
MISSING = "—"
MIN_YEAR = 1970
# Misma paleta que la página (static/styles.css, tema claro)
SERIES_COLORS = {"binance_p2p": "#2a78d6", "yadio": "#eb6834", "bcv": "#1baf7a", "paralelo": "#eda100"}
OTHER_COLOR = "#898781"
TZ_LABEL = f"UTC{settings.tz_offset_hours:+d}"

_MONTH_RE = re.compile(r"(\d{4})-(\d{2})")
_INK = colors.HexColor("#1f1f1d")
_MUTED = colors.HexColor("#6b6a66")
_RULE = colors.HexColor("#d9d7d0")
_HEADER_BG = colors.HexColor("#f1efe8")
_PAGE_W, _PAGE_H = A4
_MARGIN = 1.8 * cm
_CONTENT_W = _PAGE_W - 2 * _MARGIN


@dataclass(frozen=True, slots=True)
class SourceStats:
    """Resumen del mes de una fuente. `None` donde no hay datos o no aplica."""

    source: str
    open: float | None
    close: float | None
    low: float | None
    high: float | None
    change_pct: float | None  # cierre contra apertura del mes
    gap_vs_reference_pct: float | None  # brecha media contra el BCV en los días con cotización propia


# ------------------------------------------------------------------ mes y rango


def parse_month(raw: str, today: date) -> date:
    """`YYYY-MM` → primer día del mes. 422 si el formato no es ese, el mes no existe o es futuro."""
    m = _MONTH_RE.fullmatch(raw or "")
    if not m or not 1 <= int(m.group(2)) <= 12 or int(m.group(1)) < MIN_YEAR:
        raise InvalidQuery(f"Mes inválido: {raw!r}. Use YYYY-MM, por ejemplo 2026-10.")
    month = date(int(m.group(1)), int(m.group(2)), 1)
    if month > today.replace(day=1):
        raise InvalidQuery(f"El mes {raw} está en el futuro: no hay datos que informar.")
    return month


def month_bounds(month: date, now_ts: int) -> tuple[int, int]:
    """[inicio, fin) del mes en hora local; el mes en curso se corta al final de hoy.

    Sin el corte, el relleno hacia adelante inventaría cotizaciones para los días que faltan del mes.
    """
    start = agg.local_midnight(month)
    end = agg.local_midnight(agg.next_period(month, Interval.month))
    today_end = agg.local_midnight(agg.next_period(agg.to_local(now_ts).date(), Interval.day))
    return start, min(end, today_end)


def month_title(month: date) -> str:
    return f"{MONTH_NAMES[month.month - 1]} {month.year}"


# ------------------------------------------------------------------ resumen


def summarize(
    data: Mapping[str, Sequence[dict[str, Any]]], reference: Sequence[dict[str, Any]] | None
) -> list[SourceStats]:
    """Un `SourceStats` por fuente, en el orden de `data`.

    Apertura, cierre, mínimo y máximo salen solo de velas reales (no rellenas). La brecha media compara
    el cierre de cada día con cotización propia contra el cierre de `reference` ese día (relleno incluido:
    el sábado rige la tasa oficial del viernes).
    """
    ref_close = {c["t"]: c["close"] for c in reference or () if c["close"] is not None}
    return [_source_stats(name, candles, ref_close) for name, candles in data.items()]


def _source_stats(name: str, candles: Sequence[dict[str, Any]], ref_close: dict[int, float]) -> SourceStats:
    real = [c for c in candles if not c["filled"] and c["close"] is not None]
    if not real:
        return SourceStats(name, None, None, None, None, None, None)
    gaps = [
        pct_change(ref_close[c["t"]], c["close"])
        for c in real
        if name != REFERENCE_SOURCE and c["t"] in ref_close
    ]
    return SourceStats(
        source=name,
        open=real[0]["open"],
        close=real[-1]["close"],
        low=min(c["low"] for c in real),
        high=max(c["high"] for c in real),
        change_pct=pct_change(real[0]["open"], real[-1]["close"]),
        gap_vs_reference_pct=fmean(gaps) if gaps else None,
    )


# ------------------------------------------------------------------ formato


def _fmt_number(v: float | None, decimals: int = 2) -> str:
    """Formato es-VE: punto de miles, coma decimal."""
    if v is None:
        return MISSING
    return f"{v:,.{decimals}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return MISSING
    sign = "+" if v > 0 else ""
    return f"{sign}{_fmt_number(v)} %"


def _label(name: str) -> str:
    src = REGISTRY.get(name)
    return src.label if src else name


def _short_label(name: str) -> str:
    """Como `shortLabel` de la página: sin el sufijo de par."""
    return re.sub(r"\s+(USDT|USD)/VES$", "", _label(name))


# ------------------------------------------------------------------ gráfico


def chart_points(data: Mapping[str, Sequence[dict[str, Any]]]) -> dict[str, list[tuple[datetime, float]]]:
    """(fecha local sin zona, cierre) por fuente para el gráfico.

    Se descarta la cola de velas rellenas: si una fuente dejó de cotizar a mitad de mes, la línea termina
    en su último dato real en vez de afirmar precios que nunca se observaron. Los huecos interiores
    (fines de semana del BCV) se mantienen para que la línea siga continua.
    """
    out: dict[str, list[tuple[datetime, float]]] = {}
    for name, candles in data.items():
        last_real = max((i for i, c in enumerate(candles) if not c["filled"]), default=-1)
        out[name] = [
            (agg.to_local(c["t"]).replace(tzinfo=None), c["close"])
            for c in candles[: last_real + 1]
            if c["close"] is not None
        ]
    return out


def _chart_png(data: Mapping[str, Sequence[dict[str, Any]]], month: date) -> bytes:
    """Cierres diarios de cada fuente, un solo eje Y, como PNG en memoria."""
    # Import perezoso: los workers que nunca arman un PDF no pagan los ~35 MB de matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import DateFormatter, DayLocator
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    fig = Figure(figsize=(7.2, 3.4), dpi=200)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    for name, points in chart_points(data).items():
        if not points:
            continue
        xs, ys = zip(*points, strict=True)
        color = SERIES_COLORS.get(name, OTHER_COLOR)
        ax.plot(xs, ys, color=color, linewidth=1.3, label=_short_label(name))
    last_day = agg.next_period(month, Interval.month) - timedelta(days=1)
    ax.set_xlim(datetime(month.year, month.month, 1), datetime(last_day.year, last_day.month, last_day.day))
    ax.xaxis.set_major_locator(DayLocator(bymonthday=(1, 5, 10, 15, 20, 25, last_day.day)))
    ax.xaxis.set_major_formatter(DateFormatter("%d"))
    ax.yaxis.set_major_formatter(FuncFormatter(_axis_formatter(ax)))
    ax.set_ylabel("Bs por unidad", fontsize=8, color="#555")
    ax.set_xlabel(f"Día de {MONTH_NAMES[month.month - 1].lower()} ({TZ_LABEL})", fontsize=8, color="#555")
    ax.tick_params(labelsize=7.5, colors="#555", length=3)
    ax.grid(axis="y", color="#e6e4dd", linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#bdbab2")
    # Leyenda sobre el área de trazado, para no tapar las líneas
    ax.legend(
        loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=7.5, frameon=False, ncols=4, borderaxespad=0.3
    )
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def _axis_formatter(ax):
    """Mismos decimales en todas las marcas del eje Y: los mínimos (hasta 2) que las dejan exactas.

    Se calcula al dibujar, con las marcas definitivas tras el autoescalado.
    """

    def fmt(v: float, _pos) -> str:
        ticks = ax.get_yticks()
        decimals = next((d for d in (0, 1) if all(abs(round(t, d) - t) < 1e-9 for t in ticks)), 2)
        return _fmt_number(v, decimals)

    return fmt


# ------------------------------------------------------------------ documento


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["Normal"], fontName="Helvetica", fontSize=9.5, leading=13)
    return {
        "kicker": ParagraphStyle("kicker", parent=body, fontSize=9, textColor=_MUTED, spaceAfter=2),
        "title": ParagraphStyle(
            "title", parent=body, fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=_INK
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=body,
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=16,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "body": body,
        "meta": ParagraphStyle("meta", parent=body, fontSize=9, textColor=_MUTED),
        "note": ParagraphStyle(
            "note", parent=body, fontSize=8, leading=10.5, textColor=_MUTED, spaceBefore=4
        ),
        "th": ParagraphStyle("th", parent=body, fontName="Helvetica-Bold", fontSize=8, leading=10),
        "th_r": ParagraphStyle(
            "th_r", parent=body, fontName="Helvetica-Bold", fontSize=8, leading=10, alignment=TA_RIGHT
        ),
    }


def _table_style(n_rows: int) -> list[tuple]:
    style: list[tuple] = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, _INK),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    style += [("LINEBELOW", (0, r), (-1, r), 0.25, _RULE) for r in range(1, n_rows - 1)]
    return style


def _summary_table(stats: Sequence[SourceStats], st: dict[str, ParagraphStyle]) -> Table:
    headers = ["Fuente", "Apertura", "Cierre", "Mínimo", "Máximo", "Variación", "Brecha media vs BCV"]
    rows: list[list[Any]] = [
        [Paragraph(h, st["th"] if i == 0 else st["th_r"]) for i, h in enumerate(headers)]
    ]
    for s in stats:
        rows.append(
            [
                _short_label(s.source),
                *(_fmt_number(v) for v in (s.open, s.close, s.low, s.high)),
                _fmt_pct(s.change_pct),
                _fmt_pct(s.gap_vs_reference_pct),
            ]
        )
    first = 3.4 * cm
    rest = (_CONTENT_W - first) / (len(headers) - 1)
    table = Table(rows, colWidths=[first] + [rest] * (len(headers) - 1), repeatRows=1)
    table.setStyle(TableStyle(_table_style(len(rows))))
    return table


def _daily_table(data: Mapping[str, Sequence[dict[str, Any]]], st: dict[str, ParagraphStyle]) -> Table:
    names = list(data)
    filled = {(c["t"], name) for name, candles in data.items() for c in candles if c["filled"]}
    rows: list[list[Any]] = [
        [Paragraph("Fecha", st["th"]), *(Paragraph(_short_label(n), st["th_r"]) for n in names)]
    ]
    style = _table_style(0)
    for t, values in align_by_t(data, "close"):
        r = len(rows)
        rows.append([agg.to_local(t).strftime("%d/%m/%Y"), *(_fmt_number(v) for v in values)])
        for col, name in enumerate(names, start=1):
            if (t, name) in filled:
                style.append(("TEXTCOLOR", (col, r), (col, r), _MUTED))
                style.append(("FONT", (col, r), (col, r), "Helvetica-Oblique", 8.5))
    style += [("LINEBELOW", (0, r), (-1, r), 0.25, _RULE) for r in range(1, len(rows) - 1)]
    first = 3.0 * cm
    rest = min(3.6 * cm, (_CONTENT_W - first) / max(1, len(names)))
    table = Table(rows, colWidths=[first] + [rest] * len(names), repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle(style))
    return table


def build_monthly_report(
    month: date,
    data: Mapping[str, Sequence[dict[str, Any]]],
    stats: Sequence[SourceStats],
    *,
    generated_at: datetime | None = None,
) -> bytes:
    """PDF del mes a partir de velas diarias por fuente (salida de `build_series`) y su `summarize`.

    Si ninguna fuente tiene datos reales en el mes, el informe lo dice y omite tablas y gráfico.
    """
    st = _styles()
    title = month_title(month)
    generated = (generated_at or datetime.now(agg.LOCAL_TZ)).astimezone(agg.LOCAL_TZ)
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title=f"Informe VES {title}",
        author="VES / USDT Rates API",
    )
    story: list[Any] = [
        Paragraph("Informe mensual · bolívar frente a USDT y USD", st["kicker"]),
        Paragraph(title, st["title"]),
        Spacer(1, 6),
        Paragraph(f"Fuentes: {', '.join(_label(n) for n in data)}", st["meta"]),
        Paragraph(
            f"Generado el {generated:%d/%m/%Y a las %H:%M} ({TZ_LABEL}). "
            f"Precios en bolívares por unidad; los días se cortan en {TZ_LABEL}.",
            st["meta"],
        ),
    ]
    has_data = any(s.close is not None for s in stats)
    if not has_data:
        story += [
            Spacer(1, 18),
            Paragraph("No hay datos de ninguna de las fuentes elegidas para este mes.", st["body"]),
        ]
    else:
        story += [
            Paragraph("Resumen del mes", st["h2"]),
            _summary_table(stats, st),
            Paragraph(
                "Apertura, cierre, mínimo y máximo usan solo los días con cotización propia. Variación: "
                "cierre contra apertura del mes. Brecha media: promedio diario de la diferencia contra "
                "el BCV oficial vigente ese día.",
                st["note"],
            ),
            Paragraph("Cierre diario", st["h2"]),
            Image(BytesIO(_chart_png(data, month)), width=_CONTENT_W, height=_CONTENT_W * 3.4 / 7.2),
            Paragraph("Cotización diaria (cierre)", st["h2"]),
            _daily_table(data, st),
            Paragraph(
                "En gris cursiva, días sin cotización propia: se repite el último valor conocido "
                f"(fines de semana y feriados del BCV, por ejemplo). {MISSING} = sin dato.",
                st["note"],
            ),
        ]

    def footer(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(_MUTED)
        canvas.drawString(_MARGIN, _MARGIN / 2, f"Informe VES · {title}")
        canvas.drawRightString(_PAGE_W - _MARGIN, _MARGIN / 2, f"Página {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
