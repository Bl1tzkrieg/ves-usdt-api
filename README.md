# VES / USDT Rates API

API en FastAPI con el histórico del bolívar frente al USDT (Binance P2P, Yadio) y al dólar (BCV oficial, paralelo). Consulta cualquier rango de fechas, agrega por hora, día, semana o mes, exporta CSV y empuja el último valor por Server-Sent Events a una gráfica en vivo.

Ninguna API pública da histórico USDT/VES completo y sin límites, así que este servicio lo construye. Un recolector consulta cada fuente cada 5 minutos y guarda cada observación como un tick `(source, ts, price)` en SQLite. Los endpoints agregan esos ticks al consultar, de modo que el costo depende de cuántos ticks caen en el rango y no de cuántos días se piden. El histórico anterior al despliegue se carga con `scripts/backfill.py` o con `POST /api/v1/rates/import`.

## Fuentes

| `source` | Qué mide | De dónde sale |
|---|---|---|
| `binance_p2p` | USDT/VES. Punto medio entre la mediana de compra y la de venta de los primeros `RATES_BINANCE_ROWS` anuncios; `extra` guarda `buy`, `sell`, `ads_buy`, `ads_sell` | `p2p.binance.com`, POST `adv/search` |
| `yadio` | USDT/VES, mercado libre | `api.yadio.io/rate/VES/USD` |
| `bcv` | USD/VES oficial del BCV | `ve.dolarapi.com/v1/dolares/oficial` |
| `paralelo` | USD/VES, promedio paralelo | `ve.dolarapi.com/v1/dolares/paralelo` |

`binance_p2p` es la fuente por defecto de `/history`. Binance no publica una API P2P oficial; el endpoint es el que usa su propia web y puede cambiar sin aviso. Si una fuente falla, el recolector sigue con las demás y anota el error en `last_status`.

BCV y paralelo usan como timestamp la `fechaActualizacion` que devuelve DolarApi. El BCV publica una tasa por día hábil y `UNIQUE(source, ts)` evita guardarla otra vez en cada ciclo del recolector. Si esa fecha falta o no se puede leer, la consulta falla con `SourceError` en vez de inventar una con la hora actual.

Para agregar una fuente, escriba una subclase de `RateSource` con `fetch()` y regístrela en `app/sources/__init__.py`.

## Arranque

Requiere Python 3.11 o superior.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # y fije RATES_API_KEY si va a importar o forzar recolecciones
uvicorn app.main:app --reload     # http://localhost:8000, Swagger en /docs
```

Para probar sin esperar al recolector, `python -m scripts.seed_demo` genera 730 días de ticks sintéticos (`--days N` para otro rango). Se niega a escribir si la base ya tiene datos, para no mezclar datos falsos con reales; `--force` lo obliga.

### Docker

```bash
docker build -t ves-rates .
docker run -p 8000:8000 -v rates-data:/data -e RATES_API_KEY=... ves-rates
```

El contenedor corre como el usuario `app` (uid 10001) y guarda la base en `/data/rates.db`. Con un volumen con nombre, como arriba, no hay que hacer nada. Si monta una carpeta del host, esa carpeta tiene que pertenecer al uid 10001 o SQLite no podrá escribir:

```bash
mkdir -p ./data && sudo chown 10001 ./data
docker run -p 8000:8000 -v "$PWD/data:/data" ves-rates
```

## Configuración

Todas las variables llevan el prefijo `RATES_` y se leen del entorno o de `.env`. Las listas van en JSON.

| Variable | Defecto | Para qué |
|---|---|---|
| `RATES_DB_PATH` | `data/rates.db` | Archivo SQLite |
| `RATES_TZ_OFFSET_HOURS` | `-4` | Desfase usado para cortar días, semanas y meses. Venezuela, sin horario de verano |
| `RATES_COLLECTOR_ENABLED` | `true` | Arranca el recolector con la app |
| `RATES_COLLECTOR_INTERVAL_SECONDS` | `300` | Segundos entre recolecciones. Mínimo 30 |
| `RATES_COLLECTOR_SOURCES` | `["binance_p2p","yadio","bcv","paralelo"]` | Fuentes que consulta el recolector |
| `RATES_HTTP_TIMEOUT_SECONDS` | `15.0` | Timeout de cada consulta a una fuente |
| `RATES_BINANCE_ROWS` | `10` | Anuncios por lado para la mediana de Binance |
| `RATES_RAW_MAX_POINTS` | `20000` | Tope de puntos en `interval=raw`. Mínimo 1 |
| `RATES_MAX_RANGE_DAYS` | `7300` | Rango máximo de una consulta, unos 20 años |
| `RATES_MAX_FILL_BUCKETS` | `100000` | Cubetas máximas cuando `fill` no es `none` |
| `RATES_IMPORT_MAX_BYTES` | `10000000` | Tamaño máximo del CSV de `/import` |
| `RATES_CORS_ORIGINS` | `[]` | Orígenes CORS permitidos. Vacío significa solo mismo origen |
| `RATES_API_KEY` | vacío | Clave para `POST /import` y `POST /collect`. Vacía, ambos responden 503 |

## Endpoints

Todo vive bajo `/api/v1/rates`, salvo `/health` y la página en `/`. Swagger en `/docs` tiene los esquemas completos.

### `GET /history`

Devuelve una serie por fuente. Parámetros:

| Parámetro | Valores |
|---|---|
| `start`, `end` | `YYYY-MM-DD` en hora local, ISO 8601 con hora, o epoch en segundos. Una fecha sin hora en `end` incluye ese día completo. `end` por defecto es ahora |
| `days` | Alternativa a `start`: días hacia atrás desde `end`. Sin `start` ni `days` se piden 30 días |
| `sources` | Lista separada por comas. Por defecto `binance_p2p` |
| `interval` | `raw`, `hour`, `day` (defecto), `week` (empieza el lunes), `month` |
| `fill` | `none` (defecto) omite cubetas vacías; `forward` repite el último valor conocido, útil para el BCV en fines de semana; `null` emite la cubeta con valores `null` |
| `max_points` | Solo `raw`. Reduce la serie a unos N puntos para graficar. `0` usa `RATES_RAW_MAX_POINTS`, y pedir más que ese tope no lo supera |

```bash
curl "localhost:8000/api/v1/rates/history?start=2025-01-01&end=2026-09-14&sources=binance_p2p,bcv&interval=week"
```

Cada vela trae `t` en epoch, `date` en ISO con hora local, `open`, `high`, `low`, `close`, `avg`, `count` y `filled`. Con `interval=raw` cada punto trae `t`, `date`, `price` y `extra`.

La consulta responde 422 cuando:

- una fuente no existe;
- una fecha no se puede leer o cae fuera de 1970 a 2100 sin incluir los extremos, así que `start=0` se rechaza;
- `start` no es anterior a `end`, o el rango supera `RATES_MAX_RANGE_DAYS`;
- `fill` es `forward` o `null` y el rango tiene más de `RATES_MAX_FILL_BUCKETS` cubetas horarias o diarias. Un año por hora son unas 8.760 cubetas, así que `days=5000&interval=hour&fill=null` no pasa;
- `days` es menor que 1 o `max_points` es negativo.

### `GET /history.csv`

La misma consulta como CSV descargable. Se emite por bloques de 2.000 filas, así que años de ticks cada 5 minutos no se cargan en memoria. Además de los parámetros de `/history` acepta:

| Parámetro | Valores |
|---|---|
| `layout` | `long` (defecto): una fila por fuente y cubeta con OHLC. `wide`: una columna por fuente |
| `value` | Solo `wide`: `close` (defecto), `open`, `high`, `low`, `avg` |
| `sep` | Un carácter. `;` para Excel en español |
| `decimal` | Un carácter. Por defecto `,` si `sep=;`, si no `.`. Igual a `sep` da 422 |
| `filename` | Se sanea a `A-Za-z0-9._-`; si no queda nada útil se arma uno con el intervalo y el rango |

Con `interval=raw` el `layout` no aplica: sale una fila por tick con `source, date, timestamp, price, buy, sell`.

```bash
curl -o tasas.csv "localhost:8000/api/v1/rates/history.csv?days=1095&sources=binance_p2p,yadio,bcv&layout=wide&fill=forward&sep=;"
```

### Lectura y estado

`GET /latest` da el último tick de cada fuente; `sources` filtra y vacío significa todas. `GET /sources` lista el catálogo con cantidad de ticks, primero y último. `GET /collector` muestra si el recolector corre, su intervalo y el resultado de la última corrida por fuente. `GET /health` responde `{"status": "ok", ...}` con el mismo estado del recolector.

`GET /stream` es un stream SSE. Cada `every` segundos (defecto 15, entre 2 y 3600) revisa el último tick de `sources` y emite `event: rates` solo con las fuentes que cambiaron; si nada cambió manda un keepalive.

### Escritura, con clave

`POST /collect` y `POST /import` exigen la cabecera `X-API-Key` igual a `RATES_API_KEY`. Sin clave configurada responden 503; con una clave ausente o distinta, 401.

`POST /collect` consulta todas las fuentes ahora. Si ya hay una recolección en curso responde 409. Si la última terminó hace menos de 30 segundos no consulta nada y devuelve `"skipped": true`, lo que impide usar el endpoint para martillar las APIs externas. La respuesta trae `skipped`, `collected` con lo obtenido y `status` por fuente.

```bash
curl -X POST -H "X-API-Key: $RATES_API_KEY" localhost:8000/api/v1/rates/collect
```

`POST /import` recibe un CSV en el campo multipart `file`, con cabecera, en UTF-8. Parámetros: `source` fija la fuente cuando el CSV no trae columna `source`; `replace=true` borra antes, por cada fuente, los ticks entre la primera y la última fecha del archivo.

```bash
curl -X POST -H "X-API-Key: $RATES_API_KEY" \
     -F "file=@bcv_2025.csv" "localhost:8000/api/v1/rates/import?source=bcv"
```

Columnas reconocidas, sin distinguir mayúsculas:

- fecha: `timestamp`, `ts`, `date`, `fecha`, `datetime`, `time`, `t`, `last_update`, `x`. Si hay varias se usa la primera que se deje leer;
- precio: `price`, `rate`, `close`, `precio`, `promedio`, `value`, `avg`, `y`;
- fuente: `source` o `fuente`, opcional si se pasa `?source=`;
- `buy` y `sell`, opcionales, que se guardan en `extra`.

Las fechas pueden ser epoch en segundos o milisegundos, ISO 8601, `YYYY-MM-DD`, `YYYYMMDD`, `DD-MM-YYYY` o `DD/MM/YYYY`. Sin zona horaria se toman en hora local. `MM/DD/YYYY` no se acepta porque choca con `DD/MM`. Un epoch numérico anterior a 2000 se rechaza: casi siempre es un serial de Excel mal leído, no una fecha.

Los precios aceptan `1.234,56`, `1,234.56`, `1,5` y prefijos `Bs.` o `VES`. Con los dos separadores presentes, el último es el decimal. Con uno solo, un valor como `1.234` o `36,512` es ambiguo, y ahí decide el delimitador del CSV: con `;` el decimal es `,`, con `,` o tabulador el decimal es `.`. Así es como escribe `history.csv`, de modo que sus propias exportaciones se reimportan sin tocarlas. Una exportación con un `decimal=` distinto del predeterminado no hace ese viaje de ida y vuelta. Un valor que no encaja con esa regla, como `1.234.567` en un CSV separado por comas, se rechaza en vez de adivinar. Precios cero, negativos o no finitos también se rechazan.

Una fila mala no aborta la importación. La respuesta es:

```json
{"received": 120, "inserted": 115, "duplicates": 3, "rejected": 2,
 "rejected_sample": [{"row": 7, "reason": "precio inválido: '1.234.567'"}]}
```

`received` cuenta todas las filas de datos, `inserted` las nuevas, `duplicates` las válidas que ya existían, `rejected` las que no se pudieron leer y `rejected_sample` las primeras 10 con su motivo. `row` cuenta desde 1 sin la cabecera.

Con `replace=true` la regla cambia: si hay una sola fila rechazada responde 422, con `rejected` y `rejected_sample` dentro de `detail`, y no escribe nada. Una fila rechazada dentro del rango se borraría sin reemplazo, y prefiero que el import falle a perder datos en silencio.

Antes de leer el cuerpo, un middleware valida la clave y el tamaño. Exige `Content-Length` y responde 411 si falta o si la petición viene con `Transfer-Encoding`. Rechaza con 413 lo que supere `RATES_IMPORT_MAX_BYTES` más 64 KiB de margen para el multipart. Sin esto FastAPI escribiría a disco el archivo completo antes de comprobar la clave.

## Cargar histórico

`scripts/backfill.py` escribe directo en `RATES_DB_PATH`, sin pasar por la API. Todos los modos que importan aceptan `--source` (obligatorio) y `--dry-run`. `--ts-key` y `--price-key` solo se aplican a los modos `json` y `csv`; `yadio` y `pydolarve` los ignoran.

```bash
# Yadio, USDT/VES, un punto diario, hasta 365 días
python -m scripts.backfill yadio --source yadio --days 365

# Cualquier URL que devuelva JSON con una lista de puntos
python -m scripts.backfill json --source yadio --url "https://..." --list-key data.history

# pyDolarVenezuela, por tramos de 30 días; necesita token de pydolarve.org
python -m scripts.backfill pydolarve --source bcv --page bcv --monitor usd --days 365 --token XXX

# CSV local, mismas reglas de columnas y precios que /import
python -m scripts.backfill csv --source bcv --file bcv_2025.csv

# Ver qué devuelve una URL antes de importar
python -m scripts.backfill peek --url "https://..."
```

En el modo `json` el script detecta por su cuenta dónde está la lista de puntos, sea una lista de pares `[ts, precio]`, una lista de objetos o un objeto que la contiene. Si no logra interpretar los puntos, imprime uno de ejemplo para que indique `--ts-key` y `--price-key`. `json` y `peek` aceptan `--header "Authorization: Bearer XXX"`, repetible.

## La página

`http://localhost:8000/` sirve `static/index.html`: Chart.js vendorizado en `static/`, rangos rápidos o fechas libres, intervalo, valor, relleno, selección de fuentes, tabla y descarga CSV en `long` o `wide`. Se actualiza sola con `/stream`.

Todas las respuestas llevan `Content-Security-Policy` con `script-src 'self'`, además de `X-Content-Type-Options`, `X-Frame-Options: DENY` y `Referrer-Policy: no-referrer`. Por eso el JavaScript de la página está en `static/app.js` y no en un `<script>` inline. `/docs`, `/redoc` y `/openapi.json` quedan sin CSP porque Swagger y ReDoc cargan desde un CDN.

## Decisiones de diseño

Se guardan ticks y no velas. Cualquier agregación sale de los mismos datos, y cambiar la zona horaria o el intervalo no obliga a recalcular nada guardado.

Días, semanas y meses se cortan en UTC-4 fijo. Venezuela no tiene horario de verano, y cortar en UTC partiría cada día a las 8 de la noche.

Las cotas impiden que una sola petición tumbe el servicio. Sin ellas, `start=0&interval=hour&fill=null` pediría decenas de millones de velas, y una fila importada con `timestamp=99999999999999` dejaría rotos `/latest`, `/sources` y `/stream` hasta borrarla a mano. Por eso `Tick` rechaza timestamps fuera de 1970 a 2100 y precios que no sean finitos y positivos.

La clave de API protege las escrituras porque un POST multipart es una "simple request" para el navegador: sin clave, cualquier página abierta en la misma red podría mandar `replace=true` y borrar el histórico.

## Desarrollo

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
ruff check . && ruff format .
```

Las convenciones del proyecto están en `CLAUDE.md` y los planes de trabajo en `docs/superpowers/plans/`.

## Cambios respecto a la versión anterior

Si tenía un cliente contra la versión inicial, esto rompe:

- `POST /import` y `POST /collect` exigen `X-API-Key`. Sin `RATES_API_KEY` configurada responden 503.
- `ImportResult` cambió. `ignored` ahora se llama `duplicates`. `received` antes contaba solo las filas válidas y ahora cuenta todas las filas de datos. Se agregaron `rejected` y `rejected_sample`.
- El enum del parámetro `value` de `history.csv` se llamaba `Value` en el esquema OpenAPI y ahora se llama `CandleField`. Los valores aceptados no cambiaron.
- `start=0` y cualquier fecha fuera de 1970 a 2100 dan 422. El rango ya no es ilimitado: el tope es `RATES_MAX_RANGE_DAYS`.
- `RATES_CORS_ORIGINS` era `["*"]` y ahora es `[]`. Un front en otro origen tiene que listarse.
- La CSP bloquea scripts inline. Si modificó `index.html` con JavaScript propio, muévalo a `static/app.js`.
- `seed_demo` se niega a escribir sobre una base con datos sin `--force`.
- El contenedor ya no corre como root; ver la nota de Docker sobre carpetas del host.

## Lo que viene

Están planificados, todavía no existen: exportación a Excel con gráfico y un informe mensual en PDF (fase D), y un flujo de n8n que dispare recolecciones y backfill diarios y avise por variaciones fuertes (fase E). El detalle está en `docs/superpowers/plans/2026-11-06-hardening-quality-ui-exports-n8n.md`.
