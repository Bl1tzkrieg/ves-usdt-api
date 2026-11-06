"""Seguridad: API key para endpoints de escritura y cabeceras de seguridad."""
from __future__ import annotations

import hmac
from collections.abc import Collection

from fastapi import Header, HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'",
    ),
]


IMPORT_PATH = "/api/v1/rates/import"
IMPORT_OVERHEAD_BYTES = 64 * 1024  # margen para cabeceras y límites del multipart


def api_key_error(x_api_key: str | None) -> HTTPException | None:
    """Error (503/401) si la clave no es válida; None si lo es. Lee `settings.api_key` cada vez."""
    expected = settings.api_key
    if expected == "":
        return HTTPException(503, "API key no configurada (RATES_API_KEY)")
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        return HTTPException(401, "API key inválida o ausente")
    return None


def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """Exige `X-API-Key`. Se lee `settings.api_key` en cada petición."""
    if error := api_key_error(x_api_key):
        raise error


class ImportGuardMiddleware:
    """ASGI puro: valida `POST /import` antes de leer el cuerpo.

    FastAPI parsea el multipart antes de ejecutar las dependencias, así que sin esto un cliente
    sin clave podría subir un archivo ilimitado a disco. Comprueba `X-API-Key` (503/401) y exige
    `Content-Length` (411) no mayor que `import_max_bytes` + 64 KiB (413).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"].rstrip("/") == IMPORT_PATH:
            error = self._check(Headers(scope=scope))
            if error is not None:
                await JSONResponse({"detail": error.detail}, error.status_code)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _check(headers: Headers) -> HTTPException | None:
        if error := api_key_error(headers.get("x-api-key")):
            return error
        raw_length = headers.get("content-length")
        if raw_length is None:
            return HTTPException(411, "Se requiere la cabecera Content-Length")
        if not (raw_length.isascii() and raw_length.isdigit()):
            return HTTPException(400, "Content-Length inválido")
        limit = settings.import_max_bytes
        if int(raw_length) > limit + IMPORT_OVERHEAD_BYTES:
            return HTTPException(413, f"El archivo supera el máximo de {limit} bytes")
        return None


_CSP = b"content-security-policy"
_HEADERS_WITHOUT_CSP = [h for h in SECURITY_HEADERS if h[0] != _CSP]


class SecurityHeadersMiddleware:
    """ASGI puro: añade cabeceras a la respuesta sin almacenar el cuerpo (compatible con SSE).

    En `exempt_paths` (Swagger/ReDoc, que cargan del CDN y usan script inline) se omite la CSP.
    """

    def __init__(self, app: ASGIApp, exempt_paths: Collection[str] = ()) -> None:
        self.app = app
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _HEADERS_WITHOUT_CSP if scope["path"] in self.exempt_paths else SECURITY_HEADERS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = [*message.get("headers", []), *(h for h in headers if h[0] not in present)]
            await send(message)

        await self.app(scope, receive, send_with_headers)
