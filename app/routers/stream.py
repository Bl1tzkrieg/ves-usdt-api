"""Stream SSE con el último valor de cada fuente."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..db import get_db
from ..series import tick_to_point
from ..sse import SSE_KEEPALIVE, sse_event
from .common import RATES_PREFIX, RATES_TAGS, resolve_sources_or_all

router = APIRouter(prefix=RATES_PREFIX, tags=RATES_TAGS)


@router.get("/stream", summary="Server-Sent Events con el último valor de cada fuente (para gráfica en vivo)")
async def stream(request: Request, sources: str | None = None, every: int = Query(15, ge=2, le=3600)):
    names = resolve_sources_or_all(sources)

    async def gen() -> AsyncIterator[str]:
        last_sent: dict[str, int] = {}
        while True:
            if await request.is_disconnected():
                break
            ticks = await asyncio.to_thread(get_db().latest, names)
            fresh = [t for t in ticks if last_sent.get(t.source) != t.ts]
            if fresh:
                yield sse_event("rates", [{"source": t.source, **tick_to_point(t)} for t in fresh])
                last_sent.update({t.source: t.ts for t in fresh})
            else:
                yield SSE_KEEPALIVE
            await asyncio.sleep(every)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
