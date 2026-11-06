"""API de histórico USDT/VES (Bolívar) — FastAPI.

Arranque:  uvicorn app.main:app --reload
Docs:      http://localhost:8000/docs
Gráfica:   http://localhost:8000/
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .collector import collector
from .config import settings
from .db import get_db
from .routers import rates
from .security import SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_db()  # crea el esquema
    if settings.collector_enabled:
        collector.start()
    yield
    await collector.stop()
    get_db().close()


app = FastAPI(
    title="VES / USDT Rates API",
    version="1.0.0",
    description=(
        "Histórico del tipo de cambio Bolívar (VES) frente a USDT (Binance P2P, Yadio) y USD "
        "(BCV oficial, paralelo). Rango de fechas libre, agregación por hora/día/semana/mes, "
        "salida JSON o CSV y stream SSE para gráficas en vivo."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(rates.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "collector": collector.status()}
