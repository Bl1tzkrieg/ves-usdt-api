# Plan de prueba del flujo n8n

Estado: **revisado estructuralmente, ejecución sin verificar**. Lo que sí se corrió, sin n8n:

- `check_workflow.py` (skill `n8n-workflow-check`) sobre `ves-rates-workflow.json` y `fixture-validated-events.json`: `structurally_valid: true`, sin errores.
- `python -m json.tool` sobre ambos archivos.
- `pytest -q tests/test_n8n_workflow.py`: estructura, rutas, ausencia de secretos y, con Node, el JavaScript de "Calcular alertas" y del aviso de fallo ejecutado con `$input`, `$env` y datos estáticos simulados.

Lo que falta es correr los pasos de abajo en una instancia n8n 1.x **desechable**, sin credenciales de producción. No importe ni resetee la base de su n8n real para esto.

## Preparación

1. n8n 1.x de prueba, por ejemplo `docker run --rm -p 5678:5678 -e RATES_BASE_URL=http://host.docker.internal:8000 -e N8N_BLOCK_ENV_ACCESS_IN_NODE=false docker.n8n.io/n8nio/n8n:<versión 1.x>` (ponga una etiqueta 1.x concreta). Anote `n8n --version`.
2. No defina `RATES_ALERT_PCT` en esta instancia: los resultados esperados asumen el umbral por defecto, 3.
3. Para las pruebas con la API, una API local con datos sintéticos: `RATES_API_KEY=prueba-local python -m scripts.seed_demo --days 30 && RATES_API_KEY=prueba-local uvicorn app.main:app`.
4. Como destinos, un chat de Telegram de prueba y un SMTP falso (por ejemplo MailHog en `localhost:1025`). Nada de destinatarios reales.

## 1. Fixture sin red (`fixture-validated-events.json`)

Adapta el fixture de tres eventos del skill al contrato de `/stats`. No hace peticiones ni usa credenciales: los datos van dentro de nodos Code, y los tres nodos "Calcular alertas" llevan **el mismo** `jsCode` que el flujo (lo comprueba `test_fixture_uses_the_same_alert_code`).

Importar y ejecutar:

```sh
n8n import:workflow --input=n8n/fixture-validated-events.json
n8n execute --id=vesRatesAlertFixture01 --rawOutput
```

O desde el editor: importar el archivo y pulsar **Execute workflow**. Revise la salida de cada nodo, no solo el código de salida.

Entradas, en "Stats válidos (con fuente repetida)": cinco ítems de `/stats` — `binance_p2p` +4,2 %, `yadio` −3,0 % (justo en el umbral), `bcv` +0,5 %, `paralelo` con `change["24h"] = null`, y `binance_p2p` repetido.

Salida esperada:

| Nodo | Caso | Resultado esperado |
|---|---|---|
| Calcular alertas | **válido** | Un ítem con `threshold: 3`, `checked: 5`, `alerts` = `binance_p2p` (price 650.5, pct 4.2, from_price 624.25, from_date `2026-11-05T10:00:00-04:00`) y `yadio` (pct -3), `duplicates: ["binance_p2p\|2026-11-06T10:00:00-04:00"]` (repetición dentro de la corrida), `invalid: []`. `message` contiene `650,50`, `+4,20 %`, `624,25` y termina en `Ver: <RATES_BASE_URL>/` (o `Ver: /` sin la variable) |
| Calcular alertas (segundo tick) | **duplicado** (misma respuesta otra vez) | `alerts: []`, `duplicates` con 3 claves: las dos alertas ya enviadas y la repetición. Muestra que los datos estáticos se comparten entre nodos dentro de la ejecución |
| Calcular alertas (malformado) | **inválido** (`{"detail": ...}` y un `bcv` con `latest: "580,1"` y sin `change`) | Sale por la salida de error, que llega a "Rechazado (esperado)" con el mensaje `Respuesta de /stats malformada: 2 entradas inválidas, ninguna válida`. "Aceptado (no esperado)" no se ejecuta |

Repetir la ejecución da el mismo resultado: los datos estáticos de una ejecución manual no se guardan.

## 2. Flujo real, entrada válida

Importe `ves-rates-workflow.json` en la instancia de prueba, cree las credenciales (Header Auth con `X-API-Key: prueba-local`, Telegram de prueba, SMTP falso) y asígnelas.

| Paso | Acción | Esperado por nodo |
|---|---|---|
| 2.1 | Ejecutar desde "Cada día 06:00" | POST /collect: 200 con `collected`, `status`, `skipped`. POST /backfill/yadio 7 días: 200 con `{received, inserted, skipped}` (con red hacia Yadio) por la salida de éxito → "Backfill OK". No hay notificación |
| 2.2 | Ejecutar 2.1 otra vez enseguida | /collect: `skipped: true` (menos de 30 s) o 409; el backfill igual corre y `inserted` es 0 |
| 2.3 | Con datos de `seed_demo`, ejecutar desde "Cada hora" | GET /stats: un ítem por fuente. Calcular alertas: un ítem con `alerts` = las fuentes cuyo `abs(change.24h.pct) >= 3` en esos datos. Si hay alertas, "¿Hay alertas?" va por *true* y llegan un Telegram y un correo con el texto de `message`; si no, va a "Sin alertas" |

Para forzar una alerta conocida, fije el resultado de "GET /stats" con **Pin data** usando los ítems del fixture y ejecute desde "Cada hora": deben salir las alertas de `binance_p2p` y `yadio`.

## 3. Entrada inválida

| Paso | Acción | Esperado |
|---|---|---|
| 3.1 API caída | Detenga uvicorn y ejecute desde "Cada día 06:00" | POST /collect falla y continúa; POST /backfill/yadio sale por la salida de error → "Mensaje: backfill falló" → Telegram y correo con `[ves-rates] El backfill diario de Yadio falló` y el error (`ECONNREFUSED` o similar) |
| 3.2 API caída, rama horaria | Con la API detenida, ejecute desde "Cada hora" | GET /stats por la salida de error → "Aviso de fallo de stats" → un Telegram y un correo `[ves-rates] Alertas horarias sin datos` |
| 3.3 Sin clave | Arranque la API sin `RATES_API_KEY` y ejecute la rama diaria | Ambos POST reciben 503; el backfill sale por error y se notifica |
| 3.4 Clave errónea | Credencial con otro valor | 401 en ambos POST; misma notificación que 3.3 |
| 3.5 /stats malformado | Pin data en "GET /stats" con `[{"detail": "x"}]` y ejecute desde "Cada hora" | "Calcular alertas" sale por error → "Aviso de fallo de stats" → aviso con el texto `Respuesta de /stats malformada` |
| 3.6 Yadio caído | Sin salida a internet desde la API, rama diaria | Backfill responde 502; notificación como 3.1 con el detalle |

## 4. Evento duplicado entre corridas

Requiere el flujo **activo**, porque los datos estáticos solo se guardan en ejecuciones de producción.

1. Prepare la API con datos donde al menos una fuente supere el umbral en 24 h (por ejemplo `seed_demo` y comprobar con `GET /stats`) y arránquela con `RATES_COLLECTOR_ENABLED=false` para que no entren ticks nuevos. Pin data no sirve aquí: no se aplica en ejecuciones de producción.
2. Active el flujo y espere a que "Cada hora" dispare (minuto 5). Esperado: una alerta por cada fuente sobre el umbral.
3. Espere el siguiente disparo sin que entren ticks nuevos. Esperado: la ejecución termina en "Sin alertas"; en "Calcular alertas", `alerts: []` y las claves en `duplicates`. **Una sola alerta por tick.**
4. Inserte un tick nuevo de esa fuente (`POST /collect` o `/import`) que mantenga la variación sobre el umbral. Esperado en el siguiente disparo: una alerta nueva, porque cambió `latest_date`.
5. Para el aviso de caída: detenga la API durante dos disparos. Esperado: un solo aviso. Levántela, espere un disparo bueno, vuelva a detenerla: un aviso nuevo.

Para no esperar a la hora, puede cambiar temporalmente el intervalo de "Cada hora" a minutos en la instancia de prueba.

## Limpieza

Borre la instancia de prueba y sus credenciales. No reutilice el token del bot ni el SMTP de prueba en producción.
