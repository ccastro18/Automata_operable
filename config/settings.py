"""Configuración central del bot.

Carga el .env con pydantic-settings y expone un objeto `settings` tipado.
Incluye el primer blindaje de seguridad: si se pide REAL sin ALLOW_REAL,
la app NO arranca.
"""
from __future__ import annotations

import os
from datetime import time as dtime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Raíz del proyecto (carpeta que contiene este archivo -> ..)
BASE_DIR = Path(__file__).resolve().parent.parent

# La BD NUNCA debe vivir en una carpeta sincronizada (iCloud/Dropbox): sincronizar
# los archivos -wal/-shm de SQLite mientras el proceso escribe provoca
# "disk I/O error" y posible corrupción. El proyecto está bajo ~/Documents (iCloud),
# así que DATA_DIR sale por defecto FUERA del árbol sincronizado.
# Override opcional con la variable de entorno IQBOT_DATA_DIR.
DATA_DIR = Path(
    os.environ.get("IQBOT_DATA_DIR", Path.home() / ".local" / "share" / "iqbot")
).expanduser()
LOGS_DIR = BASE_DIR / "logs"   # los logs son texto append: sincronizar no corrompe


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Credenciales ---
    iq_email: str
    iq_password: str

    # --- Seguro de cuenta ---
    balance_mode: str = "PRACTICE"
    allow_real: bool = False

    # --- Operación ---
    base_amount: float = 1.0
    expiration_minutes: int = 5
    timeframe_seconds: int = 60
    candle_count: int = 400
    max_entry_delay_seconds: int = 10
    # Al detectarse una señal accionable, además de las 60 M1 ya recolectadas,
    # trae y persiste M5/M15 (trend multi-timeframe, fuente de verdad para ML
    # futuro). Nunca se pide en cada ciclo de escaneo, solo en señal.
    collect_multi_tf: bool = True

    # --- Filtros de estrategia ---
    min_payout: float = 0.80
    squeeze_factor: float = 0.65
    giant_candle_factor: float = 2.2
    lateral_factor: float = 0.25

    # --- Límites (desactivados en esta fase) ---
    enforce_limits: bool = False
    max_trades_per_day: int = 8
    max_consecutive_losses: int = 3
    daily_stop_loss_r: float = -3
    daily_take_profit_r: float = 5

    # --- Horario (desactivado en esta fase) ---
    enforce_session: bool = False
    session_start: str = "07:30"
    session_end: str = "10:30"
    timezone: str = "America/Bogota"

    # --- Activos ---
    # NoDecode: evita que pydantic-settings intente parsear el .env como JSON;
    # el split lo hace el validador de abajo.
    assets: Annotated[list[str], NoDecode] = ["EURUSD"]
    allow_otc: bool = False

    # --- Selección automática de activos de mercado real (no-OTC) ---
    # Con esto activo, la whitelist manual de `assets` se ignora: el bot
    # descubre y rota los activos él solo (ver domain/asset_selection.py).
    auto_asset_selection: bool = True
    asset_refresh_minutes: int = 15
    # Si NO hay ningún activo real abierto (fin de semana/festivo), por defecto
    # el bot queda en idle (no opera OTC). Encender esto permite volver a OTC
    # SOLO en ese caso concreto.
    otc_fallback: bool = False

    # ------------------------------------------------------------------ #
    #  Validadores
    # ------------------------------------------------------------------ #
    @field_validator("assets", mode="before")
    @classmethod
    def _split_assets(cls, v):
        if isinstance(v, str):
            return [a.strip().upper() for a in v.split(",") if a.strip()]
        return v

    @field_validator("balance_mode")
    @classmethod
    def _upper_mode(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _safety_guard(self):
        """Blindaje #1: nunca arrancar en REAL sin permiso explícito."""
        if self.balance_mode == "REAL" and not self.allow_real:
            raise ValueError(
                "BALANCE_MODE=REAL pero ALLOW_REAL=false. "
                "Este bot es SOLO PARA ESTUDIO en cuenta DEMO. "
                "Cambia BALANCE_MODE=PRACTICE."
            )
        return self

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def session_start_time(self) -> dtime:
        h, m = self.session_start.split(":")
        return dtime(int(h), int(m))

    @property
    def session_end_time(self) -> dtime:
        h, m = self.session_end.split(":")
        return dtime(int(h), int(m))


def load_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()  # type: ignore[call-arg]


settings = load_settings()
