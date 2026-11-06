# Flujo n8n: backfill diario y alertas horarias

`ves-rates-workflow.json` es un flujo para n8n 1.x. Estado: **revisado estructuralmente, ejecución sin verificar**. El JSON pasa `check_workflow.py` del skill `n8n-workflow-check` y `tests/test_n8n_workflow.py`, y la lógica del nodo "Calcular alertas" se ejecuta con Node en esas pruebas, pero el flujo completo no se ha corrido en una instancia de n8n. Antes de activarlo, siga [`test-plan.md`](test-plan.md).

El JSON se entrega con `active: false` y sin credenciales enlazadas.

## Qué hace

Dos disparadores en un mismo flujo, con la zona horaria del flujo en `America/Caracas`:

| Nodo | Tipo (typeVersion) | Qué hace |
|---|---|---|
| Cada día 06:00 | Schedule Trigger (1.2) | Dispara la rama diaria a las 06:00 |
| POST /collect | HTTP Request (4.2) | `POST {RATES_BASE_URL}/api/v1/rates/collect` con la credencial de cabecera. Si falla (409 por recolección en curso, API caída) sigue igual: el recolector interno corre cada 5 minutos y esto es solo un empujón |
| POST /backfill/yadio 7 días | HTTP Request (4.2) | `POST .../backfill/yadio?days=7` con la credencial. Timeout 60 s. Tiene salida de error |
| Backfill OK | No Op (1) | Fin de la rama cuando el backfill respondió 2xx (`{received, inserted, skipped}`) |
| Mensaje: backfill falló | Edit Fields / Set (3.4) | Arma `subject` y `message` con la hora y el error |
| Cada hora | Schedule Trigger (1.2) | Dispara la rama de alertas cada hora, en el minuto 5 |
| GET /stats | HTTP Request (4.2) | `GET .../stats?sources=binance_p2p,yadio,bcv,paralelo&reference=bcv`, sin clave. Cada fuente sale como un ítem. Tiene salida de error |
| Calcular alertas | Code (2) | Para cada fuente con `change["24h"]` y `abs(pct) >= umbral` arma una alerta con precio, `pct`, `from_price`, `from_date` y el enlace `{RATES_BASE_URL}/`. Deduplica por fuente dentro de la corrida y por `source|latest_date` entre corridas. Si ninguna entrada de `/stats` es válida lanza un error, que va a su salida de error |
| ¿Hay alertas? | If (2.2) | `alerts.length > 0` |
| Sin alertas | No Op (1) | Fin silencioso |
| Aviso de fallo de stats (una vez por caída) | Code (2) | Recibe los errores de `GET /stats` y de "Calcular alertas". Avisa la primera vez y calla hasta que `/stats` vuelva a responder bien, para no mandar un aviso por hora mientras la API está caída |
| Telegram | Telegram (1.2) | Envía `message` al chat `RATES_TELEGRAM_CHAT_ID` |
| Email | Send Email (2.1) | Envía `subject` y `message` en texto plano de `RATES_ALERT_EMAIL_FROM` a `RATES_ALERT_EMAIL_TO` |

Telegram y Email sirven a las tres ramas que notifican (alertas, backfill fallido, `/stats` sin datos). Ambos tienen "On Error: Continue", así que si uno falla el otro igual se intenta; el fallo queda visible en la ejecución, no en una notificación.

Un mensaje de alerta se ve así:

```
Variación fuerte en las tasas VES (umbral 3 %):
- Binance P2P: 650,50 VES/USDT (+4,20 % en 24 h; antes 624,25 el 2026-11-05T10:00:00-04:00; último dato 2026-11-06T10:00:00-04:00)
- Yadio: 640,00 VES/USD (-3,00 % en 24 h; antes 659,80 el 2026-11-05T09:55:00-04:00; último dato 2026-11-06T09:55:00-04:00)

Ver: http://localhost:8000/
```

## Variables de entorno de n8n

Se leen con `$env`, así que van en el entorno del proceso de n8n (no en el de la API). n8n 1.x permite `$env` por defecto; si la instancia tiene `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` (el valor por defecto desde n8n 2.0) las URLs de los nodos HTTP no se resuelven y hay que ponerla en `false` o escribir la URL a mano en los tres nodos HTTP.

| Variable | Obligatoria | Para qué |
|---|---|---|
| `RATES_BASE_URL` | sí | Base de la API, por ejemplo `http://ves-rates:8000`. Sin barra final (si la trae, el enlace de la página la quita) |
| `RATES_ALERT_PCT` | no | Umbral en % de la variación 24h. Por defecto 3. Vacío, no numérico o `<= 0` usa 3. Decimal con punto: `2.5` |
| `RATES_TELEGRAM_CHAT_ID` | si usa Telegram | Chat o canal destino |
| `RATES_ALERT_EMAIL_FROM`, `RATES_ALERT_EMAIL_TO` | si usa correo | Remitente y destinatario(s), separados por coma |

`RATES_API_KEY` **no** es variable de n8n: la clave vive solo en la credencial de abajo y en el entorno de la API.

## Credenciales que hay que crear

El JSON no trae enlaces de credenciales (ni nombres ni IDs). Créelas en n8n y asígnelas a mano después de importar:

| Credencial (nombre sugerido) | Tipo en n8n | Contenido | Nodos |
|---|---|---|---|
| `ves-rates-api-key` | Header Auth | Name `X-API-Key`, Value = el valor de `RATES_API_KEY` de la API | POST /collect, POST /backfill/yadio 7 días |
| `ves-rates-telegram` | Telegram API | Token del bot (@BotFather). El bot tiene que estar en el chat | Telegram |
| `ves-rates-smtp` | SMTP | Servidor, puerto, usuario y contraseña | Email |

`GET /stats` es público y no lleva credencial.

## Importar y activar

1. En n8n: **Workflows → Import from File** y elija `n8n/ves-rates-workflow.json`. Por CLI, en una instancia de prueba: `n8n import:workflow --input=n8n/ves-rates-workflow.json`.
2. Cree las tres credenciales y ábralas en cada nodo indicado (en los HTTP, "Authentication" ya está en *Generic Credential Type → Header Auth*; solo falta elegir la credencial).
3. Defina las variables de entorno y reinicie n8n para que las lea.
4. Si no quiere uno de los canales, borre su nodo (Telegram o Email).
5. Pruebe según [`test-plan.md`](test-plan.md): primero el fixture sin red, luego cada rama con "Execute workflow" desde cada disparador.
6. Active el flujo con el interruptor **Active**. Solo con el flujo activo corren los horarios y se guardan los datos estáticos de deduplicación.

## Deduplicación y sus límites

- **Dentro de una corrida**: una fuente produce como mucho una alerta aunque `/stats` la repita.
- **Entre corridas**: "Calcular alertas" guarda en los datos estáticos del flujo (`$getWorkflowStaticData('global')`) la clave `source|latest_date` de cada alerta enviada y la conserva 7 días. El mismo tick no vuelve a avisar.
- Los datos estáticos solo se persisten en ejecuciones de producción (flujo activo). En una ejecución manual desde el editor se leen y modifican en memoria pero no se guardan, así que dos pruebas manuales seguidas avisan las dos veces.
- Son por flujo y por instancia: reimportar el JSON crea un flujo nuevo con datos estáticos vacíos, y dos instancias de n8n con el mismo flujo avisan cada una por su cuenta.
- La clave se marca **antes** de enviar: si Telegram y el correo fallan, esa alerta no se repite (como mucho una vez, no exactamente una vez).
- La clave es por tick, no por movimiento. Binance y Yadio registran un tick nuevo cada 5 minutos, así que mientras una variación 24h siga sobre el umbral la fuente avisa una vez por hora. Para avisar menos, suba `RATES_ALERT_PCT`.
- El aviso de `/stats` caído usa la bandera `statsDown` de los mismos datos estáticos y tiene los mismos límites.

## Otros límites

- `/collect` responde 409 si ya hay una recolección en curso; el flujo lo ignora a propósito.
- Si `/stats` responde una lista vacía (base sin datos) no hay alertas ni aviso.
- El backfill de las 06:00 no se repite si falla; el siguiente intento es al día siguiente (con `days=7` recupera la semana).
- Un fallo de "Telegram" o "Email" no se notifica por otro canal; se ve en la lista de ejecuciones.
