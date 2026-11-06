"""Seguridad: API key para endpoints de escritura y cabeceras de seguridad."""
from __future__ import annotations

import hmac
from collections.abc import Collection

from fastapi import Header, HTTPException
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


def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """Exige `X-API-Key`. Se lee `settings.api_key` en cada petición."""
    expected = settings.api_key
    if expected == "":
        raise HTTPException(503, "API key no configurada (RATES_API_KEY)")
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "API key inválida o ausente")


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
