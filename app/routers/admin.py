"""Endpoints de administración: recolección manual, estado del recolector e importación CSV."""
from __future__ import annotations

import asyncio
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile

from .. import aggregation as agg
from ..collector import CollectInProgress, collector
from ..config import settings
from ..db import get_db
from ..models import ImportResult
from ..security import require_api_key
from ..sources import REGISTRY
from ..tick_parsing import (
    PRICE_KEYS,
    SOURCE_KEYS,
    TS_KEYS,
    decimal_hint_for,
    rows_to_ticks,
    sniff_dialect,
)
from .common import RATES_PREFIX, RATES_TAGS

router = APIRouter(prefix=RATES_PREFIX, tags=RATES_TAGS)

IMPORT_REJECTED_SAMPLE = 10
_READ_CHUNK = 1 << 20  # 1 MiB


@router.post("/collect", summary="Forzar una recolección inmediata de todas las fuentes",
             dependencies=[Depends(require_api_key)])
async def collect_now():
    before = collector.last_run
    try:
        ticks = await collector.collect_once()
    except CollectInProgress as e:
        raise HTTPException(409, str(e)) from e
    skipped = not ticks and collector.last_run == before  # corrida omitida por la separación mínima
    return {"skipped": skipped,
            "collected": [{"source": t.source, "price": t.price, "date": agg.local_iso(t.ts)} for t in ticks],
            "status": collector.last_status}


@router.get("/collector", summary="Estado del recolector")
async def collector_status():
    return collector.status()


@router.post("/import", response_model=ImportResult, summary="Importar histórico desde CSV",
             dependencies=[Depends(require_api_key)])
async def import_csv(
    file: UploadFile,
    source: str | None = Query(None, description="Fuente destino si el CSV no trae columna `source`"),
    replace: bool = Query(False, description="Borrar antes los ticks de la(s) fuente(s) en el rango importado"),
):
    """CSV con cabecera. Columnas aceptadas (insensible a mayúsculas):
    - `date` (YYYY-MM-DD, ISO 8601, DD/MM/YYYY) **o** `timestamp` (epoch en s o ms)
    - `price` (o `close`, `rate`, `promedio`); acepta `1.234,56` y `1,5`. Para valores ambiguos
      (`1.234`, `36,512`) el delimitador decide el separador decimal: con `;` el decimal es `,`
      y con `,` es `.` (igual que escribe `history.csv`), así que su propia exportación se reimporta.
    - `source` (opcional si se pasa `?source=`)
    - `buy`, `sell` (opcionales → se guardan en `extra`)

    Las filas inválidas no abortan la importación: se informan en `rejected`/`rejected_sample`
    (`row` es el índice de fila de datos, desde 1, sin contar la cabecera).
    Con `replace=true` cualquier fila rechazada aborta la importación con 422 sin escribir nada.
    """
    content = await _read_csv_text(file)
    dialect = sniff_dialect(content[:2048])
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise HTTPException(422, "CSV vacío o sin cabecera")
    headers = {c.lower().strip() for c in reader.fieldnames if c}
    if not headers & set(TS_KEYS) or not headers & set(PRICE_KEYS):
        raise HTTPException(422, f"Faltan columnas: se requiere (date|timestamp) y price. Cabecera: {reader.fieldnames}")
    if not headers & set(SOURCE_KEYS) and not source:
        raise HTTPException(422, "Indique `?source=` o incluya una columna `source`")
    if source and source not in REGISTRY:
        raise HTTPException(422, f"Fuente desconocida {source!r}. Disponibles: {sorted(REGISTRY)}")

    try:
        parsed = rows_to_ticks(
            reader, default_source=source, allowed_sources=REGISTRY, decimal_hint=decimal_hint_for(dialect.delimiter)
        )
    except csv.Error as e:
        raise HTTPException(422, f"CSV mal formado: {e}") from e

    if replace and parsed.rejected:
        # Una fila rechazada puede caer dentro del rango a reemplazar y se borraría sin sustituto.
        raise HTTPException(422, {
            "message": "Con `replace=true` no se admiten filas rechazadas; corrija el CSV. No se escribió nada.",
            "rejected": len(parsed.rejected),
            "rejected_sample": parsed.rejected[:IMPORT_REJECTED_SAMPLE],
        })

    db = get_db()
    write = db.replace_ticks if replace else db.insert_ticks
    inserted = await asyncio.to_thread(write, parsed.ticks)
    return ImportResult(
        received=len(parsed.ticks) + len(parsed.rejected),
        inserted=inserted,
        duplicates=len(parsed.ticks) - inserted,
        rejected=len(parsed.rejected),
        rejected_sample=parsed.rejected[:IMPORT_REJECTED_SAMPLE],
    )


async def _read_csv_text(file: UploadFile) -> str:
    """Lee el archivo por trozos con tope de tamaño (413) y lo decodifica como UTF-8 (422)."""
    limit = settings.import_max_bytes
    too_large = HTTPException(413, f"El archivo supera el máximo de {limit} bytes")
    if file.size is not None and file.size > limit:
        raise too_large
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise too_large
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(422, "El archivo debe estar codificado en UTF-8") from e
