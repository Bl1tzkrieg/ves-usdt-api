# VES / USDT Rates API

API en **FastAPI** para el histórico del tipo de cambio Bolívar (VES) frente a **USDT** (Binance P2P, Yadio) y **USD** (BCV oficial, paralelo), con rango de fechas libre, agregación por hora/día/semana/mes, exportación CSV y stream en vivo para gráficas.

## Cómo funciona

Ninguna API pública ofrece histórico USDT/VES sin límites, así que el histórico **se construye localmente**:

1. Un **recolector** en segundo plano consulta cada fuente cada 5 min (configurable) y guarda *ticks* `(fuente, timestamp, precio)` en SQLite.
2. Los endpoints agregan esos ticks al vuelo para cualquier rango: el costo depende de cuántos ticks hay en el rango, no de cuántos días se piden. No hay tope de días.
3. El histórico previo al despliegue se carga con `python -m scripts.backfill` (desde una API JSON, pyDolarVenezuela o un CSV; ver `--help`) o con `POST /api/v1/rates/import` (CSV).

Fuentes incluidas (`app/sources/`):

| `source`      | Qué es | Origen |
|---------------|--------|--------|
| `binance_p2p` | USDT/VES, punto medio entre mediana de compra y venta (guarda `buy`/`sell` en `extra`) | `p2p.binance.com` (POST adv/search) |
| `yadio`       | USDT/VES mercado libre | `api.yadio.io/rate/VES/USD` |
| `bcv`         | USD/VES oficial BCV | `ve.dolarapi.com/v1/dolares/oficial` |
| `paralelo`    | USD/VES promedio paralelo | `ve.dolarapi.com/v1/dolares/paralelo` |

Agregar una fuente = una clase que implemente `RateSource.fetch()` y registrarla en `app/sources/__init__.py`.

## Arranque

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # opcional
uvicorn app.main:app --reload   # http://localhost:8000  (docs en /docs)
```

Para probar sin esperar al recolector: `python -m scripts.seed_demo --days 730` genera datos sintéticos.

Docker: `docker build -t ves-rates . && docker run -p 8000:8000 -v rates-data:/data ves-rates`

## Endpoints

### `GET /api/v1/rates/history` — histórico JSON

| Parámetro | Descripción |
|-----------|-------------|
| `start`, `end` | `YYYY-MM-DD` (hora local, `end` inclusivo), ISO 8601 con hora, o epoch. Por defecto `end`=ahora. |
| `days` | Alternativa a `start`: N días hacia atrás desde `end`. **Sin tope.** |
| `sources` | Lista separada por coma. Por defecto `binance_p2p`. |
| `interval` | `raw` \| `hour` \| `day` (defecto) \| `week` (lunes) \| `month` |
| `fill` | `none` (defecto) \| `forward` (repite el último valor; útil para BCV en fines de semana) \| `null` |
| `max_points` | Solo `raw`: reduce a ~N puntos para graficar. |

```bash
curl "localhost:8000/api/v1/rates/history?start=2024-01-01&end=2026-09-14&sources=binance_p2p,bcv&interval=week"
```

Cada punto trae `t` (epoch), `date` (ISO local), `open`, `high`, `low`, `close`, `avg`, `count`, `filled`.

### `GET /api/v1/rates/history.csv` — el mismo histórico como CSV

Mismos parámetros, más `layout=long|wide`, `value=close|avg|open|high|low` (solo *wide*), `sep=;` (Excel en español) y `filename`. Se emite por *streaming*, así que un CSV de años con ticks cada 5 minutos no consume memoria ni tiene límite de filas.

```bash
curl -o tasas.csv "localhost:8000/api/v1/rates/history.csv?days=1095&sources=binance_p2p,yadio,bcv&layout=wide&fill=forward&sep=;"
```

### Otros

- `GET /api/v1/rates/latest` — último valor por fuente.
- `GET /api/v1/rates/stream` — **Server-Sent Events** (`event: rates`) con el último valor cuando cambia; parámetro `every` (segundos).
- `GET /api/v1/rates/sources` — catálogo con conteo de ticks y rango disponible.
- `POST /api/v1/rates/collect` — fuerza una recolección ahora.
- `POST /api/v1/rates/import?source=bcv&replace=false` — importa CSV (`date|timestamp`, `price`, opcional `source`, `buy`, `sell`; acepta `;` y coma decimal).
- `GET /health`

## Gráfica en vivo

`http://localhost:8000/` sirve `static/index.html`: Chart.js (vendorizado en `static/`), rangos rápidos o fechas libres, intervalo, tipo de valor, relleno de huecos, selección de fuentes, tabla y descarga CSV. Se actualiza solo vía SSE cuando el recolector guarda un tick nuevo.

## Configuración (`RATES_*`)

Ver `.env.example`. Lo relevante: `RATES_DB_PATH`, `RATES_TZ_OFFSET_HOURS` (-4 Venezuela), `RATES_COLLECTOR_INTERVAL_SECONDS`, `RATES_COLLECTOR_SOURCES`.

## Pruebas

```bash
pip install pytest && pytest -q
```

## Notas

- Los días/semanas/meses se cortan en hora de Venezuela (UTC-4, sin horario de verano), no en UTC.
- Binance P2P no tiene API oficial pública; el endpoint usado es el que consume su propio frontend y puede cambiar sin aviso. Yadio y DolarApi sirven de respaldo; el recolector sigue funcionando si una fuente falla (queda en `last_status`).
- El BCV publica una tasa por día hábil; el tick usa la fecha de publicación (`UNIQUE(source, ts)` evita duplicados).
