# Plan: endurecimiento, calidad, rediseño, exportaciones y n8n

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dejar la API USDT/VES lista para exponerse en red (autenticada y acotada), con código limpio y probado, una página de gráficas más completa, exportación a Excel y PDF, y un flujo n8n que mantiene el histórico al día y avisa de movimientos bruscos.

**Architecture:** Se conserva el diseño actual (ticks en SQLite, agregación al consultar, fuentes enchufables, recolector en segundo plano). Los cambios son: una capa de validación en la frontera de escritura (`Tick`, parser compartido), un módulo de seguridad (API key + guardas de rango), la división de `routers/rates.py` por responsabilidad, un endpoint `/stats` que alimenta la nueva página, y módulos de exportación independientes (`xlsx_export.py`, `pdf_report.py`).

**Tech Stack:** Python 3.11, FastAPI, SQLite, httpx, pytest, openpyxl (xlsx), reportlab + matplotlib (PDF), Chart.js 4 vendorizado, n8n (JSON importable).

**Spec:** Decisiones tomadas con el usuario en conversación (2026-11-06) + hallazgos de la auditoría finecomb y la revisión clean-code (resumidos en cada tarea). Proyecto de una sola persona; sin integración con Odoo.

## Global Constraints

- Python ≥ 3.11; dependencias con piso: `fastapi>=0.116`, `starlette>=0.47.2`, `python-multipart>=0.0.18`.
- Zona horaria de corte: `RATES_TZ_OFFSET_HOURS=-4`, sin DST (ya existe en `app/aggregation.py:LOCAL_TZ`).
- Todo nombre público nuevo en inglés; mensajes de error y docstrings en español (convención ya existente).
- Cada tarea termina con `pytest -q` en verde y un commit con autor `Bl1tzkrieg <djwprueba@gmail.com>`, sin líneas `Co-Authored-By` ni `Claude-Session`.
- El repositorio Git vive en la carpeta del usuario; el trabajo se hace en `/home/claude/ves-usdt-api` y se sincroniza al terminar cada tarea (commit de archivos → `git add -A && git commit` en la máquina del usuario).
- Nada se publica fuera de la máquina del usuario.

## Review Focus

1. CSV importado con precio `1.234,56` o timestamp en milisegundos (Yadio) → hoy 500 / fila guardada en el año 58000; debe aceptarse o rechazarse con informe por fila. (Tarea A2)
2. `GET /history?interval=hour&fill=null&start=0&end=<lejano>` → hoy decenas de millones de velas; debe responder 422 por rango/buckets excesivos. (Tarea A4)
3. `POST /import?replace=true` desde cualquier página web del mismo segmento de red (multipart es "simple request") → hoy borra el histórico; debe exigir `X-API-Key`. (Tarea A3)
4. Fila importada con `timestamp=99999999999999` → hoy rompe `/latest`, `/sources`, `/stream` para siempre; `Tick` debe rechazarla. (Tarea A1)
5. BCV con `fechaActualizacion` inválida → hoy cae a `now()` en silencio y duplica filas cada 5 min; debe fallar como `SourceError`. (Tarea A4)

---

## Fase A — Seguridad y endurecimiento (rama `hardening`)

### Task A1: Validación de `Tick` y reemplazo transaccional

**Files:**
- Modify: `app/db.py` (`Tick`, nuevo `Database.replace_ticks`)
- Test: `tests/test_db.py` (nuevo)

**Interfaces:**
- Produces: `Tick.__post_init__` lanza `ValueError` si no `0 < ts < 4_102_444_800` o no `math.isfinite(price) and price > 0`; `Database.replace_ticks(ticks: list[Tick]) -> int` borra `[min ts, max ts]` por fuente e inserta en una sola transacción.

- [ ] **Step 1: Escribir pruebas que fallan**

```python
@pytest.mark.parametrize("ts,price", [(0, 1.0), (4_102_444_800, 1.0), (1, 0.0), (1, -1.0), (1, float("inf")), (1, float("nan"))])
def test_tick_rejects_invalid(ts, price):
    with pytest.raises(ValueError):
        Tick("bcv", ts, price)

def test_replace_ticks_is_atomic(monkeypatch):
    db = Database(":memory:"); db.insert_ticks([Tick("bcv", 100, 1.0), Tick("bcv", 200, 2.0)])
    bad = [Tick("bcv", 150, 3.0), Tick("bcv", 160, 4.0)]
    monkeypatch.setattr(db._conn, "executemany", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    with pytest.raises(sqlite3.OperationalError):
        db.replace_ticks(bad)
    assert [t.ts for t in db.raw("bcv", 0, 10**9)] == [100, 200]  # nada borrado
```

- [ ] **Step 2: Ejecutar** `pytest tests/test_db.py -q` → FAIL (ValueError no lanzado; `replace_ticks` no existe)
- [ ] **Step 3: Implementar** `__post_init__` en `Tick` (constante `MAX_TS = 4_102_444_800`) y `replace_ticks` en `Database` (DELETE + executemany dentro de `with self._lock, self._conn:`).
- [ ] **Step 4: Ejecutar** `pytest -q` → todo PASS (ajustar `test_import_csv` si construye ticks inválidos).
- [ ] **Step 5: Commit** `feat(db): validate ticks at the write boundary and make replace atomic`

### Task A2: Parser compartido de ticks y reescritura de `/import`

**Files:**
- Create: `app/tick_parsing.py`
- Modify: `app/routers/rates.py` (`import_csv`), `app/models.py` (`ImportResult`), `scripts/backfill.py` (usa el módulo compartido), `app/config.py` (`import_max_bytes: int = 10_000_000`)
- Test: `tests/test_tick_parsing.py` (nuevo), `tests/test_api.py` (ampliar `test_import_csv`)

**Interfaces:**
- Produces: `parse_ts(value) -> int | None` (epoch s/ms, ISO 8601, `YYYY-MM-DD`, `DD-MM-YYYY`, `DD/MM/YYYY`, `MM/DD/YYYY` solo vía `fmt=`), `parse_price(value) -> float | None` (acepta `1.234,56`, `1,5`, `Bs 950`), `sniff_dialect(sample: str) -> type[csv.Dialect]`, `rows_to_ticks(rows: Iterable[Mapping[str, Any]], *, default_source: str | None, ts_key: str | None = None, price_key: str | None = None, allowed_sources: Collection[str]) -> ParseResult` con `ParseResult(ticks: list[Tick], rejected: list[dict])` (cada rechazado: `{"row": n, "reason": str}`).
- `ImportResult(received: int, inserted: int, duplicates: int, rejected: int, rejected_sample: list[dict])` (máx. 10 muestras).

- [ ] **Step 1: Pruebas** parametrizadas de `parse_ts`/`parse_price` (ms vs s, `1.234,56` → 1234.56, `abc` → None) y de `rows_to_ticks` (fila sin precio → rechazada con motivo; fuente desconocida → rechazada; `buy`/`sell` → `extra`). En `test_api.py`: importar `fecha;precio\n2020-01-01;1.234,56\n2020-01-02;abc` → `inserted=1, rejected=1`; subir 11 MB → 413; subir bytes no UTF-8 → 422; `replace=true` conserva ticks fuera del rango.
- [ ] **Step 2: Ejecutar** → FAIL (módulo inexistente).
- [ ] **Step 3: Implementar** `app/tick_parsing.py` moviendo la lógica de `scripts/backfill.py`; `import_csv` queda: validar tamaño (`file.size` o lectura por trozos de 1 MiB con tope) → decode `utf-8-sig` con `UnicodeDecodeError → 422` → `csv.DictReader` → `rows_to_ticks` → `replace_ticks` o `insert_ticks`. `backfill.py` importa del módulo y elimina sus copias.
- [ ] **Step 4: Ejecutar** `pytest -q` → PASS. Probar también `python -m scripts.backfill csv --source bcv --file <csv de prueba> --dry-run`.
- [ ] **Step 5: Commit** `refactor(import): share tick parsing between API and backfill; report rejected rows`

### Task A3: API key, CORS, cabeceras y guarda de concurrencia del recolector

**Files:**
- Create: `app/security.py`
- Modify: `app/config.py` (`api_key: str = ""`, `cors_origins: list[str] = []`), `app/main.py` (middlewares), `app/routers/rates.py` (`dependencies=[Depends(require_api_key)]` en `/import` y `/collect`), `app/collector.py` (lock + `min_gap_seconds`), `.env.example`
- Test: `tests/test_security.py` (nuevo)

**Interfaces:**
- Produces: `require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None`: si `settings.api_key == ""` → 503 `"API key no configurada (RATES_API_KEY)"`; si falta o no coincide (`hmac.compare_digest`) → 401. `SecurityHeadersMiddleware` añade `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'`. `Collector.collect_once()` lanza `CollectInProgress` si ya corre y devuelve `[]` si `time.time() - last_run < min_gap_seconds` (30); el endpoint convierte `CollectInProgress` en 409.

- [ ] **Step 1: Pruebas**: `/import` sin clave → 401 cuando `api_key="k"`; con `X-API-Key: k` → 200; con `api_key=""` → 503; `/collect` igual; `GET /history` sigue abierto; respuesta de `/` trae `x-content-type-options: nosniff`; dos `collect_once` concurrentes → el segundo 409 (simular con fuentes que esperan un `asyncio.Event`).
- [ ] **Step 2: Ejecutar** → FAIL.
- [ ] **Step 3: Implementar.** Página: `index.html` no necesita clave (solo GET).
- [ ] **Step 4: Ejecutar** `pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(security): API key on write endpoints, strict CORS, security headers, collector lock`

### Task A4: Guardas de rango y tamaño, entradas malformadas, despliegue

**Files:**
- Modify: `app/config.py` (`max_range_days: int = 7300`, `max_fill_buckets: int = 100_000`, `raw_max_points: int = 20_000`), `app/routers/rates.py`, `app/sources/dolarapi.py`, `app/sources/yadio.py`, `app/collector.py` (`status()` usa `not self._task.done()`), `scripts/seed_demo.py` (rehúsa si la BD no está vacía salvo `--force`), `Dockerfile` (usuario no root + `HEALTHCHECK`), `requirements.txt` (pisos)
- Test: `tests/test_limits.py` (nuevo), `tests/test_api.py`

**Interfaces:**
- Produces: `_resolve_range` → 422 si `(ts_to - ts_from) > max_range_days*86400`; `_build_series` → 422 si `fill != none` y buckets > `max_fill_buckets`; `max_points` efectivo = `min(max_points or raw_max_points, raw_max_points)`; `filename` saneado con `re.sub(r"[^A-Za-z0-9._-]+", "_", …)[:120]`; `sep`/`decimal` con `min_length=1`; `_parse_when` usa `raw.isascii() and raw.isdigit()`; `SourceError` trunca cuerpos a 200 caracteres; `dolarapi` con fecha inválida → `SourceError`, naive → `LOCAL_TZ`.

- [ ] **Step 1: Pruebas**: `days=8000` → 422; `interval=hour&fill=null` sobre 20 años → 422; `interval=raw` con 25 000 ticks devuelve ≤ 20 000; `filename=a";x=y` → cabecera `filename="a_x_y"`; `start=²` → 422; `sep=` → 422; `BCVSource` con `fechaActualizacion="x"` → `SourceError`; `seed_demo` sobre BD con datos → `SystemExit`.
- [ ] **Step 2 → 4:** FAIL → implementar → PASS.
- [ ] **Step 5: Commit** `fix: bound query ranges and payloads; reject malformed input with 422; harden Docker image`

### Task A5: Revisión de la rama `hardening`

- [ ] Dispatch del revisor (`code-reviewer` / `clean-code-reviewer`) sobre `git diff main..hardening`; aplicar lo que sea correcto (skill `receiving-code-review`).
- [ ] `pytest -q` verde → merge a `main` (skill `finishing-a-development-branch`).

## Fase B — Calidad de código y documentación (rama `refactor`)

### Task B1: Dividir `routers/rates.py` por responsabilidad

**Files:**
- Create: `app/series.py` (`parse_instant`, `resolve_range`, `build_series`, `tick_to_point`, constantes `SECONDS_PER_DAY`, `SECONDS_PER_HOUR`, `DEFAULT_RANGE_DAYS = 30`), `app/csv_export.py` (`format_number(v, decimal_sep)`, `raw_rows`, `long_rows`, `wide_rows`, `csv_chunks(rows, sep, chunk_rows=2000)`), `app/sse.py` (`sse_event(name, data)`, `SSE_KEEPALIVE`), `app/routers/history.py` (`/sources`, `/latest`, `/history`, `/history.csv`), `app/routers/stream.py`, `app/routers/admin.py` (`/collect`, `/collector`, `/import`), `app/routers/common.py` (`HistoryQuery` como dependencia compartida)
- Delete: `app/routers/rates.py`
- Modify: `app/main.py` (incluir los routers), `app/aggregation.py` (`Value` → `CandleField`; borrar `pick`)
- Test: los existentes deben pasar sin cambios de comportamiento; añadir `tests/test_csv_export.py` (`format_number(951.7375, ",") == "951,7375"`, `wide_rows` alinea por timestamp)

**Interfaces:**
- Produces: `class HistoryQuery(BaseModel)` con `start, end, days, sources, interval, fill, max_points` y método `resolve() -> tuple[int, int, list[str]]`; `build_series(source, ts_from, ts_to, interval, fill, max_points) -> list[dict]`; `parse_instant(raw: str | None, *, inclusive_day_end: bool) -> int | None` lanza `ValueError` (el router lo convierte a 422 con un `exception_handler` único).

- [ ] **Step 1:** Mover sin cambiar comportamiento, un módulo por commit si ayuda. `pytest -q` verde después de cada movimiento.
- [ ] **Step 2:** Renombres (`Value → CandleField`, `records/rows → csv_rows/csv_chunks`, `_parse_when → parse_instant`), constantes en vez de `86400`/`3600`, borrar código muerto.
- [ ] **Step 3: Commit** `refactor: split rates router into history/stream/admin; extract series, csv_export and sse modules`

### Task B2: Pruebas faltantes, `ruff` y arreglos menores de `index.html`

**Files:**
- Create: `pyproject.toml` (sección `[tool.ruff]`, line-length 110, reglas `E,F,I,B,UP`), `tests/test_collector.py`, `tests/test_aggregation.py`
- Modify: `static/index.html` (`getJson` con comprobación de `res.ok`, `pointValue` compartido, `ALL_TIME_START`, Map reutilizado en la tabla), `requirements-dev.txt` (`pytest`, `ruff`)

- [ ] **Step 1: Pruebas**: `Collector.collect_once` con una fuente que falla (guarda las otras, `last_status` refleja el error); `rollup` promedio ponderado; `downsample` conserva primero y último; `/latest`, `/sources`, primer evento de `/stream`; `parse_instant` con epoch y con ISO con zona.
- [ ] **Step 2:** `ruff check . --fix && ruff format .` sin errores.
- [ ] **Step 3: Commit** `test: cover collector, aggregation and stream; add ruff config`

### Task B3: README y CLAUDE.md

**Files:**
- Modify: `README.md` (reescribir con el skill `unslop`: qué es, cómo correr, cómo cargar histórico, endpoints, seguridad —clave de API—, exportaciones, n8n, decisiones de diseño)
- Create: `CLAUDE.md` (comandos: `pytest -q`, `ruff check .`, `uvicorn app.main:app --reload`; convenciones: nombres en inglés / mensajes en español, TDD, commits por tarea, autor; mapa de módulos)

- [ ] **Step 1:** Escribir ambos. Revisar con `unslop` (sin listas de relleno, sin "robust/seamless", voz directa).
- [ ] **Step 2: Commit** `docs: rewrite README; add CLAUDE.md with project conventions`
- [ ] **Step 3:** Revisión de rama + merge a `main`.

## Fase C — Rediseño de la página (rama `ui`)

### Task C1: Endpoint `/stats`

**Files:**
- Create: `app/stats.py`, `app/routers/stats.py`
- Modify: `app/models.py`, `app/main.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Produces: `GET /api/v1/rates/stats?sources=&reference=bcv` → `list[SourceStats]`, `SourceStats(source, label, unit, latest: float, latest_date: str, change: dict[str, Change | None])` con claves `"24h"`, `"7d"`, `"30d"`, `Change(abs: float, pct: float, from_price: float, from_date: str)` y `spread_vs_reference_pct: float | None` (= `(latest/ref_latest - 1)*100`, `None` para la propia referencia o sin dato). "Precio hace 24h" = último tick con `ts <= latest_ts - 86400` (función `Database.last_before`, ya existe).
- Produces también: `GET /api/v1/rates/daily-change?source=&days=` → lista de `{date, pct}` (variación cierre-a-cierre diaria, calculada sobre `build_series(interval=day)`).

- [ ] **Step 1: Pruebas** con el fixture de 10 días: `change["24h"].pct` exacto para binance_p2p; `spread_vs_reference_pct` binance vs bcv del día 0 ≈ 11.33; sin datos → `change[k] is None`.
- [ ] **Step 2 → 4:** FAIL → implementar → PASS.
- [ ] **Step 5: Commit** `feat(api): /stats with 24h/7d/30d changes and spread vs BCV; /daily-change`

### Task C2: Nueva página

**Files:**
- Modify: `static/index.html` (reescritura), `static/app.js` y `static/styles.css` (separar; el HTML queda legible)
- Verificación: captura con Playwright en claro y oscuro, 1280 px y 400 px

**Decisiones (skills `frontend-design` + `dataviz`):**
- Tokens de color del skill dataviz (ya en uso); series: binance azul, yadio naranja, bcv aqua, paralelo amarillo; nunca dos ejes Y.
- Estructura: cabecera con 4 tiles (precio, delta 24h con flecha y color de estado solo acompañado de texto, mini sparkline de 7 días en SVG inline), barra de controles en una fila (rango, fechas, intervalo, valor, huecos, fuentes), panel principal (serie temporal), dos paneles secundarios lado a lado (barras de variación diaria % de la fuente principal; línea de brecha USDT vs BCV %), fila de acciones (CSV, XLSX, PDF, tabla), tabla plegable.
- Hover: crosshair + tooltip con todas las series (Chart.js `interaction.mode='index'`); leyenda siempre visible con ≥2 series; números con `Intl.NumberFormat('es-VE')`, `tabular-nums` en tabla y tiles.
- Estado vacío explícito cuando una fuente no tiene datos en el rango ("Sin datos en este rango — corre el backfill").
- SSE sigue refrescando tiles y gráfica.

- [ ] **Step 1:** Implementar; **Step 2:** `python scripts/screenshot.py` (nuevo, Playwright) genera `docs/screenshots/{light,dark}.png`; mirar y corregir colisiones/overflow.
- [ ] **Step 3: Commit** `feat(ui): redesign dashboard with stat tiles, daily change and BCV spread panels`
- [ ] **Step 4:** Revisión + merge.

## Fase D — Exportaciones (rama `exports`)

### Task D1: `GET /api/v1/rates/history.xlsx`

**Files:**
- Create: `app/xlsx_export.py`, Modify: `app/routers/history.py`, `requirements.txt` (`openpyxl>=3.1`)
- Test: `tests/test_xlsx.py` (abrir con openpyxl: hojas, cabeceras, formato numérico, existe un gráfico)

**Interfaces:**
- Produces: `build_workbook(data: dict[str, list[dict]], names: list[str], interval: Interval, field: CandleField) -> bytes`. Hoja `Resumen` (wide: fecha + una columna por fuente, formato `#,##0.0000`, fecha como fecha real de Excel, cabecera congelada, autofiltro, anchos) + una hoja por fuente con OHLC + gráfico `LineChart` nativo en `Resumen`. Mismos parámetros de consulta que `history.csv`. Leer `anthropic-skills:xlsx` SKILL.md antes de implementar.

- [ ] Test → FAIL → implementar → PASS → Commit `feat(export): native Excel export with chart`

### Task D2: `GET /api/v1/rates/report.pdf?month=YYYY-MM&sources=`

**Files:**
- Create: `app/pdf_report.py`, Modify: `app/routers/history.py`, `requirements.txt` (`reportlab>=4`, `matplotlib>=3.8`)
- Test: `tests/test_pdf.py` (bytes empiezan por `%PDF`, páginas ≥ 1, texto contiene el mes)

**Interfaces:**
- Produces: `build_monthly_report(month: date, data: dict[str, list[dict]], stats: list[SourceStats]) -> bytes`: portada con mes y fuentes, tabla resumen (apertura, cierre, mín, máx, variación %, brecha media vs BCV), gráfico PNG (matplotlib, `Agg`, misma paleta) y tabla diaria. Leer `anthropic-skills:pdf` SKILL.md antes de implementar.

- [ ] Test → FAIL → implementar → PASS → Commit `feat(export): monthly PDF report` → revisión + merge.

## Fase E — Automatización con n8n (rama `n8n`)

### Task E1: Endpoint de backfill para n8n

**Files:**
- Modify: `app/routers/admin.py`, `scripts/backfill.py` (extraer `fetch_yadio_history(days: int, currency: str = "VES") -> list[Tick]` a `app/backfill_sources.py` para que lo use el endpoint)
- Test: `tests/test_admin.py` con `httpx.MockTransport`

**Interfaces:**
- Produces: `POST /api/v1/rates/backfill/yadio?days=365` (protegido por API key) → `{"received": n, "inserted": m}`.

- [ ] Test → FAIL → implementar → PASS → Commit `feat(api): protected Yadio backfill endpoint for schedulers`

### Task E2: Flujo n8n

**Files:**
- Create: `n8n/ves-rates-workflow.json`, `n8n/README.md`, `n8n/test-plan.md`

**Decisiones (skill `n8n-workflow-check`):**
- Nodo Schedule 06:00 (America/Caracas) → HTTP `POST /collect` y `POST /backfill/yadio?days=7` con cabecera `X-API-Key` desde una credencial n8n "Header Auth".
- Nodo Schedule cada hora → HTTP `GET /stats` → nodo IF `abs(change["24h"].pct) >= {{ $env.RATES_ALERT_PCT ?? 3 }}` → Telegram (credencial) y Email; mensaje con fuente, precio, variación y enlace a la página. Nodo de deduplicación (Static Data) para no repetir la misma alerta en la misma hora.
- Plan de prueba según el skill: entrada válida, entrada inválida (API caída → rama de error notifica), evento duplicado (misma variación dos veces → una sola alerta).

- [ ] Escribir JSON, validar que importa (estructura de nodos/conexiones), documentar variables (`RATES_BASE_URL`, `RATES_API_KEY`, `RATES_ALERT_PCT`), Commit `feat(n8n): daily backfill and hourly alert workflow` → merge.

---

## Orden y entregas

A (seguridad) → B (calidad) → C (UI) → D (exportaciones) → E (n8n). Cada fase termina en `main` con sus pruebas en verde; al final, una revisión de rama completa con `requesting-code-review`.
