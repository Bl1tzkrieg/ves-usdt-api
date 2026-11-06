"""Configuración por variables de entorno (prefijo RATES_)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RATES_", env_file=".env", extra="ignore")

    # Persistencia
    db_path: str = "data/rates.db"

    # Zona horaria usada para cortar los días/semanas/meses (Venezuela = UTC-4, sin DST)
    tz_offset_hours: int = -4

    # Recolector en segundo plano
    collector_enabled: bool = True
    collector_interval_seconds: int = 300  # cada 5 minutos
    collector_sources: list[str] = Field(
        default_factory=lambda: ["binance_p2p", "yadio", "bcv", "paralelo"]
    )
    http_timeout_seconds: float = 15.0

    # Binance P2P: cuántos anuncios usar para la mediana
    binance_rows: int = 10

    # Límite blando de puntos en respuestas "raw" (0 = sin límite)
    raw_max_points: int = 0

    # Tamaño máximo del CSV subido a /import (bytes)
    import_max_bytes: int = 10_000_000

    # Orígenes CORS permitidos (lista JSON). Vacío = sin CORS (solo mismo origen).
    cors_origins: list[str] = Field(default_factory=list)

    # Clave para los endpoints de escritura (/import, /collect). Vacía = se rechazan con 503.
    api_key: str = ""


settings = Settings()
