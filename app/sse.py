"""Formato Server-Sent Events."""

from __future__ import annotations

import json
from typing import Any

SSE_KEEPALIVE = ": keep-alive\n\n"


def sse_event(name: str, data: Any) -> str:
    """Evento SSE con nombre y `data` serializado como JSON en una línea."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"
