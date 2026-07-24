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
    Field("collect_multi_tf", "bool", None, None, "Recolectar M5/M15 en señal", "Operación",
          "Al detectarse una señal accionable, además de las velas M1 se guardan M5/M15 "
          "(trade_candle_snapshots/trade_market_snapshots) para el trend multi-timeframe. "
          "Solo se pide en señal, nunca en cada ciclo de escaneo."),

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
    Field("auto_asset_selection", "bool", None, None, "Selección automática de activos", "Filtros de riesgo",
          "Si está activo, ignora la whitelist manual y el bot descubre solo los activos de mercado REAL "
          "(no-OTC) abiertos con mejor payout. Recomendado: encendido (el proyecto pivotó a mercado real)."),
    Field("asset_refresh_minutes", "int", 1, 120, "Frecuencia de rotación de activos (min)", "Filtros de riesgo",
          "Cada cuántos minutos se recalcula la lista de activos automática (además de al conectar)."),
    Field("otc_fallback", "bool", None, None, "Fallback a OTC si no hay mercado real abierto", "Filtros de riesgo",
          "Solo aplica con selección automática activa. Si NO hay ningún activo real abierto (fin de semana), "
          "permite operar OTC temporalmente. Por defecto apagado: el bot queda en idle en vez de operar OTC."),
    Field("allow_digital", "bool", None, None, "Operar digitales (no-OTC)", "Filtros de riesgo",
          "Si turbo/binary no tiene payout, intenta opción DIGITAL (útil para pares reales). Si falla, queda como paper."),
    Field("operate_without_payout", "bool", None, None, "Operar sin payout (binaria asumida)", "Filtros de riesgo",
          "Si NINGUNA fuente da payout y el ÚNICO bloqueo es el payout, monta la orden como binaria con el payout asumido."),
    Field("assumed_payout", "float", 0, 1, "Payout asumido", "Filtros de riesgo",
          "Payout (0-1) que se asume cuando no hay payout real. Normalmente 0.85; ajústalo si cambia."),

    # --- Rechazos configurables ---
    Field("reject_low_payout", "bool", None, None, "Rechazar por payout bajo", "Rechazos configurables",
          "Def ENCENDIDO (bloqueante): el payout bajo es una decisión ECONÓMICA (rentabilidad "
          "de la operación), no una señal predictiva, así que no aplica el análisis 2026-07-10 "
          "de los filtros de estrategia. Si está apagado, un payout bajo queda registrado como "
          "permitido por usuario."),
    Field("reject_bb_squeeze", "bool", None, None, "Rechazar por squeeze Bollinger", "Rechazos configurables",
          "Análisis 2026-07-10: sin valor predictivo con n=4,700 (no sobrevive Bonferroni; "
          "los trades bloqueados ganan igual que los permitidos). Se mantiene en LOG-ONLY "
          "(def apagado): el filtro se sigue evaluando y registrando en trade_filter_evaluations "
          "para seguir midiendo, pero ya no bloquea la señal."),
    Field("reject_giant_candle", "bool", None, None, "Rechazar por vela gigante", "Rechazos configurables",
          "Análisis 2026-07-10: sin valor predictivo con n=4,700 (no sobrevive Bonferroni; "
          "los trades bloqueados ganan igual que los permitidos). Se mantiene en LOG-ONLY "
          "(def apagado): el filtro se sigue evaluando y registrando en trade_filter_evaluations "
          "para seguir midiendo, pero ya no bloquea la señal."),
    Field("reject_lateral_market", "bool", None, None, "Rechazar por mercado lateral", "Rechazos configurables",
          "Análisis 2026-07-10: sin valor predictivo con n=4,700 (no sobrevive Bonferroni; "
          "los trades bloqueados ganan igual que los permitidos). Se mantiene en LOG-ONLY "
          "(def apagado): el filtro se sigue evaluando y registrando en trade_filter_evaluations "
          "para seguir midiendo, pero ya no bloquea la señal."),
    Field("reject_late_entry", "bool", None, None, "Rechazar por entrada tarde", "Rechazos configurables",
          "Def ENCENDIDO (bloqueante): la entrada tardía es un problema de EJECUCIÓN "
          "(el precio ya se movió), no de predicción de la señal, así que no aplica el "
          "análisis 2026-07-10 de los filtros de estrategia. Si está apagado, late_entry "
          "queda registrado como permitido por usuario."),

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

    # --- Estrategia activa: Confirmed Band Reversion ---
    Field("rsi_period", "int", 2, 100, "RSI periodo", "Estrategia activa",
          "Periodo Wilder del RSI usado para detectar extremo y giro."),
    Field("bb_period", "int", 5, 200, "Bollinger periodo", "Estrategia activa",
          "Media y ventana de volatilidad de las bandas (def 20)."),
    Field("bb_mult", "float", 1, 5, "Bollinger desviación", "Estrategia activa",
          "Número de desviaciones poblacionales de las bandas (def 2)."),
    Field("atr_period", "int", 2, 100, "ATR periodo", "Estrategia activa",
          "Periodo Wilder para normalizar el rango de la vela."),
    Field("adx_period", "int", 2, 100, "ADX periodo", "Estrategia activa",
          "Periodo Wilder para medir fuerza de tendencia."),
    Field("reversion_rsi_threshold", "float", 20, 49, "RSI extremo", "Estrategia activa",
          "CALL exige RSI previo <= valor; PUT exige RSI previo >= 100-valor."),
    Field("reversion_min_rsi_turn", "float", 0, 20, "Giro RSI mínimo", "Estrategia activa",
          "Cambio mínimo del RSI hacia la reversión entre las dos últimas velas."),
    Field("reversion_min_wick_ratio", "float", 0, 1, "Mecha mínima", "Estrategia activa",
          "Fracción mínima de la vela que debe rechazar el extremo (0.25 = 25%)."),
    Field("reversion_max_adx", "float", 0, 100, "ADX máximo", "Estrategia activa",
          "Evita intentar reversión en tendencias demasiado fuertes."),
    Field("reversion_max_range_atr", "float", 0.5, 10, "Rango/ATR máximo", "Estrategia activa",
          "Evita entrar contra velas de choque anormalmente grandes."),

    # --- Contexto histórico, no participa en la señal nueva ---
    Field("ema_fast", "int", 1, 500, "EMA rápida", "Contexto diagnóstico",
          "Se conserva para snapshots y análisis de régimen; no decide la entrada."),
    Field("ema_slow", "int", 1, 1000, "EMA lenta", "Contexto diagnóstico",
          "Se conserva para snapshots y análisis de régimen; no decide la entrada."),
    Field("fib_lookback", "int", 5, 500, "Rango histórico lookback", "Contexto diagnóstico",
          "Compatibilidad con el histórico FibPullback; no participa en la señal nueva."),
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
        "collect_multi_tf": getattr(s, "collect_multi_tf", True),
        "min_payout": s.min_payout,
        "max_entry_delay_seconds": s.max_entry_delay_seconds,
        "squeeze_factor": s.squeeze_factor,
        "giant_candle_factor": s.giant_candle_factor,
        "lateral_factor": s.lateral_factor,
        "allow_otc": s.allow_otc,
        "auto_asset_selection": getattr(s, "auto_asset_selection", True),
        "asset_refresh_minutes": getattr(s, "asset_refresh_minutes", 15),
        "otc_fallback": getattr(s, "otc_fallback", False),
        "allow_digital": True,
        "operate_without_payout": True,
        "assumed_payout": 0.85,
        "reject_low_payout": True,
        # bb_squeeze / giant_candle / lateral_market: LOG-ONLY por defecto desde
        # 2026-07-10 (ver Field.help arriba y DICCIONARIO_DATOS.md). Se siguen
        # evaluando y registrando en trade_filter_evaluations, pero no bloquean.
        "reject_bb_squeeze": False,
        "reject_giant_candle": False,
        "reject_lateral_market": False,
        "reject_late_entry": True,
        "risk_mgmt_enabled": True,
        "risk_cooldown_losses": 2,
        "risk_cooldown_minutes": 15,
        "risk_window_start_hour": 6,
        "risk_window_end_hour": 13,
        "risk_window_max_losses": 3,
        "ema_fast": 50, "ema_slow": 200, "rsi_period": 14,
        "bb_period": 20, "bb_mult": 2, "fib_lookback": 25,
        "atr_period": 14, "adx_period": 14,
        "reversion_rsi_threshold": 35.0,
        "reversion_min_rsi_turn": 1.5,
        "reversion_min_wick_ratio": 0.25,
        "reversion_max_adx": 28.0,
        "reversion_max_range_atr": 1.8,
    }


def schema() -> list[dict]:
    return [
        {"key": f.key, "type": f.type, "min": f.min, "max": f.max,
         "label": f.label, "group": f.group, "help": f.help}
        for f in FIELDS
    ]
