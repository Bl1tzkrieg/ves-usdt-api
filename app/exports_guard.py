"""Serializa las construcciones pesadas (XLSX y PDF): una a la vez, las demás esperan."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

_semaphore = asyncio.Semaphore(1)


async def run_heavy(fn: Callable[..., Any], *args: Any) -> Any:
    """Ejecuta `fn(*args)` en un hilo, con a lo sumo una construcción pesada a la vez en el proceso."""
    async with _semaphore:
        return await asyncio.to_thread(fn, *args)
