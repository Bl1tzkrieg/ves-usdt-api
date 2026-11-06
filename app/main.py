"""API de histórico USDT/VES (Bolívar) — FastAPI.

Arranque:  uvicorn app.main:app --reload
Docs:      http://localhost:8000/docs
Gráfica:   http://localhost:8000/
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .collector import collector
from .config import settings
from .db import get_db
from .routers import admin, history, stream
from .security import ImportGuardMiddleware, SecurityHeadersMiddleware
from .series import InvalidQuery

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
app.add_middleware(ImportGuardMiddleware)  # el más interno: tras CORS y cabeceras de seguridad
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
)
app.add_middleware(
    SecurityHeadersMiddleware,
    exempt_paths={
        p for p in (app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url, app.openapi_url) if p
    },
)
app.include_router(history.router)
app.include_router(stream.router)
app.include_router(admin.router)


@app.exception_handler(InvalidQuery)
async def invalid_query_handler(_: Request, exc: InvalidQuery) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=422)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "collector": collector.status()}
