"""Llenar la base de datos con histórico real desde fuentes externas o archivos.

Modos:

  # 0) Yadio (USDT/VES, hasta 365 días) — sin registro
  python -m scripts.backfill yadio --source yadio --days 365

  # 1) Cualquier API JSON que devuelva una lista de puntos (Yadio, pydolarve, un export propio…)
  python -m scripts.backfill json --source yadio \
      --url "https://api.yadio.io/hist/..." \
      [--list-key history] [--ts-key timestamp] [--price-key rate] [--header "Authorization: Bearer XXX"]

  # 2) pyDolarVenezuela (BCV y monitores) — necesita token gratuito de https://pydolarve.org
  python -m scripts.backfill pydolarve --source bcv --page bcv --monitor usd --days 365 --token XXX

  # 3) CSV local (columnas date|fecha|timestamp y price|precio|close|rate|promedio)
  python -m scripts.backfill csv --source bcv --file bcv_2025.csv

  # Inspeccionar qué devuelve una URL antes de importar (no escribe nada)
  python -m scripts.backfill peek --url "https://..."

El script detecta solo la forma de los datos: lista de listas [ts, precio], lista de dicts, o un dict
con la lista dentro (--list-key). Fechas: epoch en s o ms, ISO 8601, YYYY-MM-DD o DD-MM-YYYY.
Si no logra interpretar algo, imprime una muestra de la respuesta para ajustar las claves.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from app.aggregation import LOCAL_TZ
from app.db import Tick, get_db
from app.sources import REGISTRY

TS_KEYS = ("timestamp", "ts", "time", "t", "date", "fecha", "datetime", "last_update", "x")
PRICE_KEYS = ("price", "rate", "close", "precio", "promedio", "value", "avg", "y")


# ---------------------------------------------------------------- parsing tolerante

def parse_ts(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return int(v / 1000) if v > 1e11 else int(v)  # ms vs s
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit():
        return parse_ts(float(s))
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%Y, %I:%M %p", "%d/%m/%Y, %I:%M %p"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=LOCAL_TZ).timestamp())
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return int(dt.timestamp())
    except ValueError:
        return None


def parse_price(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("Bs", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")  # 1.234,56
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def find_list(data: Any, list_key: str | None) -> list:
    if list_key:
        for part in list_key.split("."):
            data = data[part]
        return data
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # primera lista "grande" que encontremos
        for v in data.values():
            if isinstance(v, list) and v:
                return v
        for v in data.values():
            if isinstance(v, dict):
                try:
                    return find_list(v, None)
                except ValueError:
                    pass
    raise ValueError("No encontré una lista de puntos; use --list-key")


def to_ticks(items: Iterable[Any], source: str, ts_key: str | None, price_key: str | None) -> tuple[list[Tick], list[Any]]:
    ticks, bad = [], []
    for it in items:
        ts = price = None
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            ts, price = parse_ts(it[0]), parse_price(it[1])
        elif isinstance(it, dict):
            tk = ts_key or next((k for k in TS_KEYS if k in it), None)
            pk = price_key or next((k for k in PRICE_KEYS if k in it), None)
            ts = parse_ts(it.get(tk)) if tk else None
            price = parse_price(it.get(pk)) if pk else None
        if ts is None or price is None or price <= 0:
            bad.append(it)
            continue
        ticks.append(Tick(source=source, ts=ts, price=price))
    return ticks, bad


def save(ticks: list[Tick], source: str, dry: bool) -> None:
    if not ticks:
        print("Nada que importar.")
        return
    ticks.sort(key=lambda t: t.ts)
    first, last = datetime.fromtimestamp(ticks[0].ts, LOCAL_TZ), datetime.fromtimestamp(ticks[-1].ts, LOCAL_TZ)
    print(f"{len(ticks)} puntos para '{source}' entre {first:%Y-%m-%d} y {last:%Y-%m-%d}")
    if dry:
        print("(--dry-run: no se escribió nada)")
        return
    n = get_db().insert_ticks(ticks)
    print(f"Insertados {n} nuevos ({len(ticks) - n} ya existían)")


def check_source(name: str) -> None:
    if name not in REGISTRY:
        sys.exit(f"Fuente desconocida {name!r}. Disponibles: {sorted(REGISTRY)}")


def fetch_json(url: str, headers: list[str]) -> Any:
    h = {"User-Agent": "rates-backfill/1.0", "Accept": "application/json"}
    for raw in headers:
        k, _, v = raw.partition(":")
        h[k.strip()] = v.strip()
    r = httpx.get(url, headers=h, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- modos

def cmd_peek(a: argparse.Namespace) -> None:
    data = fetch_json(a.url, a.header)
    print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])


def cmd_json(a: argparse.Namespace) -> None:
    check_source(a.source)
    data = fetch_json(a.url, a.header)
    try:
        items = find_list(data, a.list_key)
    except (KeyError, ValueError) as e:
        print(f"Error: {e}\nMuestra de la respuesta:\n{json.dumps(data, ensure_ascii=False)[:1500]}")
        sys.exit(1)
    ticks, bad = to_ticks(items, a.source, a.ts_key, a.price_key)
    if bad:
        print(f"{len(bad)} puntos no interpretados; ejemplo: {json.dumps(bad[0], ensure_ascii=False)[:300]}")
        if not ticks:
            print("Indique --ts-key y --price-key con los nombres exactos de ese ejemplo.")
            sys.exit(1)
    save(ticks, a.source, a.dry_run)


def cmd_yadio(a: argparse.Namespace) -> None:
    """Yadio: GET https://api.yadio.io/hist/{days}/{currency}  (days 1..365)

    Respuesta: [{"date": "09/14/2026", "rate": 940.01, "avg24h": ..., "usdbtc": ...}, ...]
    `date` es MM/DD/YYYY y `rate` son VES por 1 USD (mercado libre, base USDT). Un punto por día;
    se guarda a la medianoche local de ese día.
    """
    check_source(a.source)
    days = min(max(a.days, 1), 365)
    if a.days > 365:
        print("Yadio limita el histórico a 365 días; se piden 365.")
    data = fetch_json(f"https://api.yadio.io/hist/{days}/{a.currency}", [])
    if not isinstance(data, list):
        print(f"Respuesta inesperada: {json.dumps(data, ensure_ascii=False)[:500]}")
        sys.exit(1)
    ticks, bad = [], []
    for it in data:
        try:
            d = datetime.strptime(it["date"], "%m/%d/%Y").replace(tzinfo=LOCAL_TZ)
            ticks.append(Tick(source=a.source, ts=int(d.timestamp()), price=float(it["rate"])))
        except (KeyError, ValueError, TypeError):
            bad.append(it)
    if bad:
        print(f"{len(bad)} puntos no interpretados; ejemplo: {json.dumps(bad[0], ensure_ascii=False)[:300]}")
    save(ticks, a.source, a.dry_run)


def cmd_pydolarve(a: argparse.Namespace) -> None:
    """Historia de pyDolarVenezuela por tramos (su API limita el rango por petición)."""
    check_source(a.source)
    end = date.today()
    start = end - timedelta(days=a.days)
    headers = [f"Authorization: Bearer {a.token}"] if a.token else []
    all_ticks: list[Tick] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=a.chunk_days), end)
        url = (f"{a.base}/api/v2/dollar/history?page={a.page}&monitor={a.monitor}"
               f"&start_date={cur:%d-%m-%Y}&end_date={nxt:%d-%m-%Y}&format_date=iso&rounded_price=false")
        try:
            data = fetch_json(url, headers)
            items = find_list(data, "history") if isinstance(data, dict) and "history" in data else find_list(data, None)
            ticks, bad = to_ticks(items, a.source, None, None)
            print(f"  {cur} → {nxt}: {len(ticks)} puntos" + (f", {len(bad)} sin interpretar" if bad else ""))
            all_ticks.extend(ticks)
        except Exception as e:  # noqa: BLE001
            print(f"  {cur} → {nxt}: error {e}")
        cur = nxt
    save(all_ticks, a.source, a.dry_run)


def cmd_csv(a: argparse.Namespace) -> None:
    check_source(a.source)
    with open(a.file, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(f, dialect=dialect))
    rows = [{k.lower().strip(): v for k, v in r.items() if k} for r in rows]
    ticks, bad = to_ticks(rows, a.source, a.ts_key, a.price_key)
    if bad:
        print(f"{len(bad)} filas no interpretadas; ejemplo: {bad[0]}")
    save(ticks, a.source, a.dry_run)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--source", required=True, help=f"Fuente destino: {sorted(REGISTRY)}")
        p.add_argument("--ts-key", help="Clave/columna de la fecha (autodetecta si se omite)")
        p.add_argument("--price-key", help="Clave/columna del precio (autodetecta si se omite)")
        p.add_argument("--dry-run", action="store_true", help="Mostrar qué se importaría sin escribir")

    p = sub.add_parser("peek", help="Ver la respuesta cruda de una URL")
    p.add_argument("--url", required=True)
    p.add_argument("--header", action="append", default=[], help='Ej: "Authorization: Bearer XXX"')
    p.set_defaults(fn=cmd_peek)

    p = sub.add_parser("json", help="Importar desde una URL JSON")
    common(p)
    p.add_argument("--url", required=True)
    p.add_argument("--list-key", help="Ruta a la lista dentro del JSON, ej. 'data.history'")
    p.add_argument("--header", action="append", default=[])
    p.set_defaults(fn=cmd_json)

    p = sub.add_parser("yadio", help="Importar hasta 365 días de Yadio (USDT/VES)")
    common(p)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--currency", default="VES")
    p.set_defaults(fn=cmd_yadio)

    p = sub.add_parser("pydolarve", help="Importar historia de pyDolarVenezuela")
    common(p)
    p.add_argument("--page", default="bcv", help="bcv | criptodolar | enparalelovzla | …")
    p.add_argument("--monitor", default="usd", help="usd | usdt | eur | …")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--chunk-days", type=int, default=30)
    p.add_argument("--token", default="", help="Token de https://pydolarve.org")
    p.add_argument("--base", default="https://pydolarve.org")
    p.set_defaults(fn=cmd_pydolarve)

    p = sub.add_parser("csv", help="Importar desde un CSV local")
    common(p)
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_csv)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
