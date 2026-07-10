"""config_epoch: huella corta y estable de la configuración que afecta la
generación y selección de señales.

Por qué existe: cada cambio de configuración (parámetros de indicadores,
umbrales de filtros, rechazos activos/log-only, gestión de riesgo...)
fragmenta el dataset de `trades` en épocas distintas. Comparar el win-rate
de un filtro (o de la estrategia) mezclando trades de configuraciones
distintas contamina cualquier análisis estadístico posterior (ML incluido).
`compute_config_epoch()` da un identificador corto y determinista para
etiquetar cada trade con la configuración vigente en el momento en que se
generó, así el análisis puede agrupar/filtrar por época sin ambigüedad.

Claves incluidas en el hash (afectan qué señal se genera o si se acepta/
rechaza; ver `config/runtime_config.py::FIELDS` para la definición de cada
una):
  - Estrategia (indicadores): ema_fast, ema_slow, rsi_period, bb_period,
    bb_mult, fib_lookback
  - Umbrales de filtros / operación: squeeze_factor, giant_candle_factor,
    lateral_factor, expiration_minutes, min_payout, allow_otc, otc_fallback,
    auto_asset_selection
  - Rechazos configurables (bloqueante vs log-only): reject_low_payout,
    reject_bb_squeeze, reject_giant_candle, reject_lateral_market,
    reject_late_entry
  - Gestión de riesgo: risk_mgmt_enabled, risk_cooldown_losses,
    risk_cooldown_minutes, risk_window_start_hour, risk_window_end_hour,
    risk_window_max_losses
  - Ejecución/recolección: max_entry_delay_seconds, collect_multi_tf

Deliberadamente NO se incluyen: base_amount, max_assets,
asset_refresh_minutes, allow_digital, operate_without_payout,
assumed_payout, assets (whitelist manual). Esas claves cambian CÓMO o
CUÁNTO se opera (dinero, nº de activos monitoreados, mecánica de compra),
pero no QUÉ señal genera la estrategia ni si un filtro la acepta/rechaza,
que es lo que este epoch etiqueta para el análisis de filtros/ML.
"""
from __future__ import annotations

import hashlib
import json

# Orden EXPLÍCITO y estable: no depende del orden de iteración del dict de
# entrada. Añadir una clave nueva aquí es un cambio deliberado (documentarlo
# en este docstring) y automáticamente produce una nueva época para todo
# trade posterior, aunque los VALORES no hayan cambiado.
EPOCH_KEYS: tuple[str, ...] = (
    # Estrategia (indicadores)
    "ema_fast", "ema_slow", "rsi_period", "bb_period", "bb_mult", "fib_lookback",
    # Umbrales de filtros / operación
    "squeeze_factor", "giant_candle_factor", "lateral_factor",
    "expiration_minutes", "min_payout", "allow_otc", "otc_fallback",
    "auto_asset_selection",
    # Rechazos configurables
    "reject_low_payout", "reject_bb_squeeze", "reject_giant_candle",
    "reject_lateral_market", "reject_late_entry",
    # Gestión de riesgo
    "risk_mgmt_enabled", "risk_cooldown_losses", "risk_cooldown_minutes",
    "risk_window_start_hour", "risk_window_end_hour", "risk_window_max_losses",
    # Ejecución / recolección
    "max_entry_delay_seconds", "collect_multi_tf",
)


def compute_config_epoch(config: dict) -> str:
    """Hash corto (sha256, 12 hex = 48 bits) del subconjunto ORDENADO y
    estable de `config` definido en EPOCH_KEYS.

    Propiedades garantizadas:
      - Determinista: mismos valores -> mismo hash, sin importar el orden de
        las claves en `config` (se serializa con `sort_keys=True` sobre un
        dict armado en el orden fijo de EPOCH_KEYS, nunca por iteración
        directa del dict de entrada).
      - Sensible a cualquier cambio de valor en una clave de EPOCH_KEYS
        (incluye cambios de tipo, p.ej. True vs 1 vs "true" ya coercionados
        por `runtime_config.coerce`).
      - Tolerante a configs parciales: una clave de EPOCH_KEYS ausente en
        `config` se serializa como None en vez de lanzar KeyError (útil para
        configs viejas restauradas de BD antes de que existiera una clave,
        o para tests con dicts mínimos).
      - Pura: no lee reloj, red ni BD. Misma entrada -> misma salida siempre.
    """
    subset = {k: config.get(k) for k in EPOCH_KEYS}
    payload = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
