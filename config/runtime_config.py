"""Esquema de configuración editable desde el panel (pestaña Configuración).

Define qué parámetros se pueden cambiar en caliente, sus tipos, límites y
valores por defecto (tomados del .env). La validación vive aquí para que el
backend y el frontend usen la misma fuente de verdad.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    type: str  # "float" | "int" | "bool"
    min: float | None
    max: float | None
    label: str
    group: str
    help: str = ""


FIELDS: list[Field] = [
    # --- Operación ---
    Field("base_amount", "float", 0.01, 1_000_000, "Monto por operación", "Operación",
          "Cantidad a invertir en cada entrada."),
    Field("expiration_minutes", "int", 1, 5, "Expiración (min)", "Operación",
          "Duración de la opción binaria (turbo 1-5)."),
    Field("max_assets", "int", 1, 8, "Máx. activos a monitorear", "Operación",
          "Cuántos activos puedes seguir a la vez (1-8). Cada activo añade llamadas a la API por vela."),

    # --- Filtros de riesgo ---
    Field("min_payout", "float", 0, 1, "Payout mínimo", "Filtros de riesgo",
          "No opera si el payout es menor (0-1, ej. 0.80)."),
    Field("max_entry_delay_seconds", "int", 0, 60, "Máx. retraso de entrada (s)", "Filtros de riesgo",
          "Segundos máximos tras el cierre de la vela para entrar."),
    Field("squeeze_factor", "float", 0, 5, "Factor squeeze Bollinger", "Filtros de riesgo",
          "Si bb_width < mediana*factor => no opera (bandas comprimidas)."),
    Field("giant_candle_factor", "float", 0, 10, "Factor vela gigante", "Filtros de riesgo",
          "Si rango > avg_range_20*factor => no opera."),
    Field("lateral_factor", "float", 0, 5, "Factor mercado lateral", "Filtros de riesgo",
          "Si |ema50-ema200| < avg_range_20*factor => no opera."),
    Field("allow_otc", "bool", None, None, "Permitir OTC", "Filtros de riesgo",
          "Operar y listar activos OTC."),
    Field("allow_digital", "bool", None, None, "Operar digitales (no-OTC)", "Filtros de riesgo",
          "Si turbo/binary no tiene payout, intenta opción DIGITAL (útil para pares reales). Si falla, queda como paper."),
    Field("operate_without_payout", "bool", None, None, "Operar sin payout (binaria asumida)", "Filtros de riesgo",
          "Si NINGUNA fuente da payout y el ÚNICO bloqueo es el payout, monta la orden como binaria con el payout asumido."),
    Field("assumed_payout", "float", 0, 1, "Payout asumido", "Filtros de riesgo",
          "Payout (0-1) que se asume cuando no hay payout real. Normalmente 0.85; ajústalo si cambia."),

    # --- Rechazos configurables ---
    Field("reject_low_payout", "bool", None, None, "Rechazar por payout bajo", "Rechazos configurables",
          "Si está apagado, un payout bajo queda registrado como permitido por usuario."),
    Field("reject_bb_squeeze", "bool", None, None, "Rechazar por squeeze Bollinger", "Rechazos configurables",
          "Si está apagado, bb_squeeze queda registrado como permitido por usuario."),
    Field("reject_giant_candle", "bool", None, None, "Rechazar por vela gigante", "Rechazos configurables",
          "Si está apagado, giant_candle queda registrado como permitido por usuario."),
    Field("reject_lateral_market", "bool", None, None, "Rechazar por mercado lateral", "Rechazos configurables",
          "Si está apagado, lateral_market queda registrado como permitido por usuario."),
    Field("reject_late_entry", "bool", None, None, "Rechazar por entrada tarde", "Rechazos configurables",
          "Si está apagado, late_entry queda registrado como permitido por usuario."),

    # --- Gestión de riesgo (capa nueva, tras los filtros normales) ---
    Field("risk_mgmt_enabled", "bool", None, None, "Activar gestión de riesgo", "Gestión de riesgo",
          "Si está apagado, las señales que pasan los filtros normales se operan en real sin pausas. "
          "Si está encendido, aplica las pausas A y B (las bloqueadas quedan como paper)."),
    Field("risk_cooldown_losses", "int", 1, 20, "Pérdidas consecutivas para pausar", "Gestión de riesgo",
          "Nº de pérdidas REALES seguidas que disparan el cooldown (def 2)."),
    Field("risk_cooldown_minutes", "int", 1, 480, "Minutos de cooldown", "Gestión de riesgo",
          "Duración de la pausa tras las pérdidas consecutivas (def 15), desde el cierre de la última."),
    Field("risk_window_start_hour", "int", 0, 23, "Inicio ventana protegida (h)", "Gestión de riesgo",
          "Hora de inicio de la ventana 06:00-13:00 (def 6). Incluye desde HH:00:00."),
    Field("risk_window_end_hour", "int", 1, 24, "Fin ventana protegida (h)", "Gestión de riesgo",
          "Hora de fin EXCLUSIVA de la ventana (def 13). No incluye 13:00:00 en adelante."),
    Field("risk_window_max_losses", "int", 1, 50, "Máx. pérdidas en ventana", "Gestión de riesgo",
          "Pérdidas REALES dentro de la ventana que detienen el real hasta el fin de la ventana (def 3)."),

    # --- Estrategia (indicadores) ---
    Field("ema_fast", "int", 1, 500, "EMA rápida", "Estrategia (indicadores)", "Periodo (def 50)."),
    Field("ema_slow", "int", 1, 1000, "EMA lenta", "Estrategia (indicadores)", "Periodo (def 200)."),
    Field("rsi_period", "int", 1, 100, "RSI periodo", "Estrategia (indicadores)", "def 14."),
    Field("bb_period", "int", 1, 200, "Bollinger periodo", "Estrategia (indicadores)", "def 20."),
    Field("bb_mult", "float", 1, 5, "Bollinger desviación", "Estrategia (indicadores)", "def 2."),
    Field("fib_lookback", "int", 5, 500, "Fibonacci lookback", "Estrategia (indicadores)",
          "Velas para el swing (def 25)."),
]

FIELD_MAP: dict[str, Field] = {f.key: f for f in FIELDS}


def coerce(key: str, value):
    """Valida y convierte un valor según el esquema. Lanza ValueError si no cumple."""
    f = FIELD_MAP[key]
    if f.type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "sí", "si")
    try:
        num = float(value) if f.type == "float" else int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"{f.label}: valor inválido")
    if f.min is not None and num < f.min:
        raise ValueError(f"{f.label}: mínimo {f.min}")
    if f.max is not None and num > f.max:
        raise ValueError(f"{f.label}: máximo {f.max}")
    return num


def defaults_from_settings(s) -> dict:
    return {
        "base_amount": s.base_amount,
        "expiration_minutes": s.expiration_minutes,
        "max_assets": getattr(s, "max_assets", 4),
        "min_payout": s.min_payout,
        "max_entry_delay_seconds": s.max_entry_delay_seconds,
        "squeeze_factor": s.squeeze_factor,
        "giant_candle_factor": s.giant_candle_factor,
        "lateral_factor": s.lateral_factor,
        "allow_otc": s.allow_otc,
        "allow_digital": True,
        "operate_without_payout": True,
        "assumed_payout": 0.85,
        "reject_low_payout": True,
        "reject_bb_squeeze": True,
        "reject_giant_candle": True,
        "reject_lateral_market": True,
        "reject_late_entry": True,
        "risk_mgmt_enabled": True,
        "risk_cooldown_losses": 2,
        "risk_cooldown_minutes": 15,
        "risk_window_start_hour": 6,
        "risk_window_end_hour": 13,
        "risk_window_max_losses": 3,
        "ema_fast": 50, "ema_slow": 200, "rsi_period": 14,
        "bb_period": 20, "bb_mult": 2, "fib_lookback": 25,
    }


def schema() -> list[dict]:
    return [
        {"key": f.key, "type": f.type, "min": f.min, "max": f.max,
         "label": f.label, "group": f.group, "help": f.help}
        for f in FIELDS
    ]
