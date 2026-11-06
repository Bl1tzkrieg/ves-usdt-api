# CLAUDE.md

API FastAPI con el histórico VES/USDT y VES/USD. Ver `README.md` para endpoints y configuración.

## Comandos

```bash
pytest -q                                   # toda la suite; BD en memoria, sin red
ruff check . && ruff format .               # lint y formato, línea de 110
uvicorn app.main:app --reload               # http://localhost:8000, Swagger en /docs
python -m scripts.seed_demo --days 30       # datos sintéticos; --force si la BD ya tiene datos
python -m scripts.backfill yadio --source yadio --days 365 --dry-run
python -m scripts.backfill csv --source bcv --file bcv.csv
```

`node` es opcional: sin él en el PATH, 5 pruebas de `tests/test_n8n_workflow.py` que ejecutan el nodo Code se saltan (`pytest -q` lo muestra como `skipped`). `node --check static/app.js` también lo necesita.

`POST /import`, `POST /collect` y `POST /backfill/yadio` necesitan `RATES_API_KEY` en el entorno y la cabecera `X-API-Key`.

## Convenciones

- Identificadores en inglés. Mensajes de error, docstrings y comentarios en español.
- TDD: la prueba que falla va primero, luego el código.
- Un commit por tarea del plan, con el mensaje que indica el plan. Autor `Bl1tzkrieg <djwprueba@gmail.com>`, sin líneas `Co-Authored-By` ni `Claude-Session`.
- Un refactor que no cambia comportamiento no toca las pruebas existentes: son el contrato.
- Cada tarea termina con `pytest -q` y `ruff check .` en verde.
- Días, semanas y meses se cortan en `LOCAL_TZ` de `app/aggregation.py`, UTC-4 sin horario de verano. No usar UTC para cortes.
- Toda escritura de ticks pasa por `Tick`, que valida timestamp y precio en `__post_init__`.

## Mapa de módulos

| Módulo | Qué hace |
|---|---|
| `app/main.py` | Crea la app, middlewares, routers, `/`, `/health` y el handler de `InvalidQuery` a 422 |
| `app/config.py` | `Settings` con prefijo `RATES_` |
| `app/db.py` | SQLite: tabla `ticks`, `Tick`, insertar, reemplazar y consultas de lectura |
| `app/aggregation.py` | `Interval`, `Fill`, `CandleField`, velas, rollup semanal y mensual, relleno de huecos, downsample |
| `app/series.py` | Interpreta fechas y rangos y arma series raw o de velas; lanza `InvalidQuery` |
| `app/csv_export.py` | Filas raw, long y wide, alineación por `t` (`align_by_t`), formato numérico, emisión por bloques, nombre de archivo |
| `app/xlsx_export.py` | `build_workbook`: libro .xlsx con hoja `Resumen` (una columna `fuente (campo)` por fuente y gráfico nativo hasta 5000 filas) y una hoja por fuente |
| `app/exports_guard.py` | `run_heavy`: semáforo que serializa las construcciones de XLSX y PDF |
| `app/pdf_report.py` | `parse_month`, `month_bounds`, `summarize` y `build_monthly_report`: informe mensual en PDF (reportlab + gráfico matplotlib Agg) |
| `app/tick_parsing.py` | Parser de fechas, precios y filas a `Tick`, compartido por `/import` y `scripts/backfill.py` |
| `app/backfill_sources.py` | `fetch_yadio_history`, `BackfillResult(ticks, skipped)` y `keep_missing_days` (solo días locales sin ticks): histórico de Yadio compartido por `/backfill/yadio` y `scripts/backfill.py` |
| `app/security.py` | `require_api_key`, `ImportGuardMiddleware` y cabeceras de seguridad con CSP |
| `app/stats.py` | Variaciones 24h/7d/30d, spread contra referencia y variación diaria |
| `app/sse.py` | Formato de eventos SSE |
| `app/collector.py` | Recolector en segundo plano con lock, separación mínima de 30 s y `last_error` de escritura (visible en `/health`) |
| `app/models.py` | Modelos Pydantic de respuesta |
| `app/routers/common.py` | Prefijo, resolución de fuentes y `HistoryQuery` |
| `app/routers/history.py` | `/sources`, `/latest`, `/history`, `/history.csv`, `/history.xlsx`, `/report.pdf` |
| `app/routers/stream.py` | `/stream` por SSE |
| `app/routers/stats.py` | `/stats`, `/daily-change` |
| `app/routers/admin.py` | `/collect`, `/collector`, `/import`, `/backfill/yadio` (para n8n) |
| `app/sources/` | Una clase `RateSource` por fuente y el `REGISTRY` |
| `scripts/backfill.py` | Carga histórica: modos `yadio`, `json`, `pydolarve`, `csv`, `peek` |
| `scripts/seed_demo.py` | Datos sintéticos para desarrollo |
| `scripts/screenshot.py` | Levanta la app con datos sintéticos y captura la página en claro, oscuro y móvil en `docs/screenshots/` |
| `n8n/` | Flujo n8n (backfill diario y alertas horarias), su README, el plan de prueba y un fixture manual sin red; `tests/test_n8n_workflow.py` lo valida |
| `tests/conftest.py` | Fixture `client` con BD en memoria y 10 días de ticks desde `T0`, compartido por las pruebas de API |
| `static/` | `index.html`, `styles.css`, `app.js` y Chart.js vendorizado. Nada de JS inline: la CSP lo bloquea |

## Planes

Los planes de implementación viven en `docs/superpowers/plans/`. El vigente es `2026-11-06-hardening-quality-ui-exports-n8n.md`, con fases A a E y una rama por fase.
