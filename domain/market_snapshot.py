"""Construcción del snapshot técnico de mercado (tabla trade_market_snapshots).

NO calcula la señal ni decide nada: solo LEE el DataFrame que la estrategia ya
enriqueció y deriva las métricas extendidas (pendientes EMA, squeeze, posición
en BB, Fibonacci histórico, cuerpo/mechas, ATR, ADX y etiqueta de tendencia).

Dos orígenes:
  - build_live_snapshot(df, tf)  -> en vivo, desde el frame de indicadores.
  - build_backfill_snapshot(row) -> desde una fila histórica de `trades`, para
    no perder lo ya recolectado (bb_width, rsi, etc.).

Toda métrica que no se pueda calcular queda en None (NULL en la BD).
"""
from __future__ import annotations

from datetime import datetime


def _f(v):
    """Float seguro: None si no es convertible o es NaN."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _div(a, b):
    a, b = _f(a), _f(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def _ts(epoch) -> str | None:
    e = _f(epoch)
    if e is None:
        return None
    try:
        return datetime.fromtimestamp(e).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def _trend_label(close, ema50, ema200, slope_50_3) -> str | None:
    vals = [close, ema50, ema200, slope_50_3]
    if any(v is None for v in vals):
        return None
    if close > ema200 and ema50 > ema200 and slope_50_3 > 0:
        return "bullish"
    if close < ema200 and ema50 < ema200 and slope_50_3 < 0:
        return "bearish"
    return "lateral"


def build_live_snapshot(df, timeframe_seconds: int) -> dict:
    """Snapshot desde el frame de indicadores (última vela cerrada = señal).

    `df` ya viene de `strategy.add_indicators(raw).dropna()`, así que trae
    ema50/ema200, rsi, bb_*, swing_*, fib_*, range, avg_range_20, ema_distance.

    Devuelve un dict SIN trade_id/created_at: los estampa la capa de
    persistencia justo antes de escribir (el trade_id se genera al insertar).
    """
    n = len(df)
    if n == 0:
        return {"timeframe_seconds": timeframe_seconds, "source": "live"}

    c = df.iloc[-1]
    prev = df.iloc[-2] if n >= 2 else c
    prev3 = df.iloc[-4] if n >= 4 else df.iloc[0]

    close = _f(c.get("close"))
    open_ = _f(c.get("open"))
    high = _f(c.get("high"))
    low = _f(c.get("low"))

    ema50 = _f(c.get("ema50"))
    ema200 = _f(c.get("ema200"))
    ema_distance = _f(c.get("ema_distance"))
    ema_slope_50_3 = (None if ema50 is None or _f(prev3.get("ema50")) is None
                      else ema50 - _f(prev3.get("ema50")))

    bb_top = _f(c.get("bb_top"))
    bb_bottom = _f(c.get("bb_bottom"))
    bb_width = _f(c.get("bb_width"))           # normalizado: (top-bottom)/mid
    bb_width_median = _f(c.get("bb_width_median"))

    swing_low = _f(c.get("swing_low"))
    swing_high = _f(c.get("swing_high"))
    swing_range = _f(c.get("swing_range"))

    candle_range = _f(c.get("range"))
    avg_range_20 = _f(c.get("avg_range_20"))
    atr = _f(c.get("atr"))

    body_size = None if (close is None or open_ is None) else abs(close - open_)
    upper_wick = (None if (high is None or open_ is None or close is None)
                  else high - max(open_, close))
    lower_wick = (None if (low is None or open_ is None or close is None)
                  else min(open_, close) - low)

    def fib_dist(level: float):
        if swing_low is None or swing_range is None or close is None:
            return None
        return abs(close - (swing_low + swing_range * level))

    return {
        "timeframe_seconds": timeframe_seconds,
        "candle_timestamp": _ts(c.get("timestamp")),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": _f(c.get("volume")),

        "ema50": ema50, "ema200": ema200,
        "ema_distance": ema_distance,
        "ema_distance_pct": _div(ema_distance, close),
        "ema_slope_50_1": (None if ema50 is None or _f(prev.get("ema50")) is None
                           else ema50 - _f(prev.get("ema50"))),
        "ema_slope_50_3": ema_slope_50_3,
        "ema_slope_200_1": (None if ema200 is None or _f(prev.get("ema200")) is None
                            else ema200 - _f(prev.get("ema200"))),
        "ema_slope_200_3": (None if ema200 is None or _f(prev3.get("ema200")) is None
                            else ema200 - _f(prev3.get("ema200"))),

        "rsi": _f(c.get("rsi")),
        "rsi_prev": _f(prev.get("rsi")),
        "rsi_delta": (None if _f(c.get("rsi")) is None or _f(prev.get("rsi")) is None
                      else _f(c.get("rsi")) - _f(prev.get("rsi"))),

        "bb_top": bb_top, "bb_mid": _f(c.get("bb_mid")), "bb_bottom": bb_bottom,
        "bb_width": bb_width,
        "bb_width_pct": (_div(bb_top - bb_bottom, close)
                         if bb_top is not None and bb_bottom is not None else None),
        "bb_width_median_100": bb_width_median,
        "bb_squeeze_ratio": _div(bb_width, bb_width_median),
        "bb_width_slope_3": (None if bb_width is None or _f(prev3.get("bb_width")) is None
                             else bb_width - _f(prev3.get("bb_width"))),
        "close_position_in_bb": (_div(close - bb_bottom, bb_top - bb_bottom)
                                 if bb_top is not None and bb_bottom is not None
                                 and close is not None else None),

        "swing_high": swing_high, "swing_low": swing_low, "swing_range": swing_range,
        "fib_position": _div((close - swing_low) if (close is not None and swing_low is not None) else None,
                             swing_range),
        "distance_to_fib_382": fib_dist(0.382),
        "distance_to_fib_500": fib_dist(0.500),
        "distance_to_fib_618": fib_dist(0.618),

        "candle_range": candle_range,
        "avg_range_20": avg_range_20,
        "candle_range_ratio": _div(candle_range, avg_range_20),
        "atr_14": atr,
        "adx_14": _f(c.get("adx")),
        "range_atr_ratio": _div(candle_range, atr),
        "body_size": body_size,
        "body_ratio": _div(body_size, candle_range),
        "upper_wick_ratio": _div(upper_wick, candle_range),
        "lower_wick_ratio": _div(lower_wick, candle_range),

        "trend_label": _trend_label(close, ema50, ema200, ema_slope_50_3),
        "source": "live",
    }


def build_backfill_snapshot(row: dict, trade_id: str, created_at: str) -> dict:
    """Snapshot desde una fila histórica de `trades`.

    Conserva lo ya recolectado (bb_width, bb_width_median, ema_distance,
    candle_range, avg_range_20, rsi) y deriva los ratios que sí se pueden
    calcular con esos valores. El resto queda en None.
    """
    bb_width = _f(row.get("bb_width"))
    bb_width_median = _f(row.get("bb_width_median"))
    ema_distance = _f(row.get("ema_distance"))
    candle_range = _f(row.get("candle_range"))
    avg_range_20 = _f(row.get("avg_range_20"))
    close = _f(row.get("entry_price"))  # mejor proxy del close de la señal

    return {
        "trade_id": trade_id,
        "timeframe_seconds": 60,
        "candle_timestamp": row.get("signal_time") or row.get("entry_time"),
        "close": close,
        "ema_distance": ema_distance,
        "ema_distance_pct": _div(ema_distance, close),
        "rsi": _f(row.get("rsi")),
        "bb_width": bb_width,
        "bb_width_median_100": bb_width_median,
        "bb_squeeze_ratio": _div(bb_width, bb_width_median),
        "candle_range": candle_range,
        "avg_range_20": avg_range_20,
        "candle_range_ratio": _div(candle_range, avg_range_20),
        "source": "backfill",
        "created_at": created_at,
    }
