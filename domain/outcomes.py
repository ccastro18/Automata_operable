"""Reconstrucción de métricas de resultado (tabla trade_outcomes).

Al cierre de la operación se traen las velas de 1M del periodo
[entrada, vencimiento] con get_candles y se calculan exit_price, MFE, MAE y los
deltas direccionales. Sin streaming: precisión a nivel de vela de 1 minuto.

Función pura: recibe los datos crudos y devuelve un dict listo para insertar.
"""
from __future__ import annotations

from datetime import datetime


def _f(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _norm_candle(c: dict) -> dict | None:
    """Normaliza una vela de iqoptionapi (min/max) a open/high/low/close/from."""
    frm = _f(c.get("from"))
    high = _f(c.get("max", c.get("high")))
    low = _f(c.get("min", c.get("low")))
    close = _f(c.get("close"))
    open_ = _f(c.get("open"))
    if frm is None or high is None or low is None or close is None:
        return None
    return {"from": frm, "to": _f(c.get("to")) or frm + 60,
            "open": open_, "high": high, "low": low, "close": close}


def _ts(epoch) -> str | None:
    e = _f(epoch)
    if e is None:
        return None
    try:
        return datetime.fromtimestamp(e).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def build_price_path(candles: list[dict], entry_ts: float, expiry_ts: float) -> list[dict]:
    """Puntos de recorrido del precio durante la operación, para trade_price_path.

    Reutiliza las mismas velas 1M que se traen al cierre (sin llamadas extra ni
    streaming). Una fila por vela dentro de la ventana [entrada, vencimiento].
    """
    norm = [n for n in (_norm_candle(c) for c in (candles or [])) if n]
    window = [n for n in norm if entry_ts - 60 <= n["from"] <= expiry_ts]
    if not window:
        window = norm
    points = []
    for n in sorted(window, key=lambda x: x["from"]):
        points.append({
            "timestamp": _ts(n["from"]),
            "seconds_from_entry": round(n["from"] - entry_ts, 2),
            "price": n["close"],
            "candle_open": n["open"], "candle_high": n["high"],
            "candle_low": n["low"], "candle_close": n["close"],
        })
    return points


def reconstruct_outcome(
    *,
    trade_id: str,
    direction: str,
    entry_price: float | None,
    entry_ts: float,
    expiry_ts: float,
    candles: list[dict],
    result: str,
    profit: float,
    created_at: str,
    result_source: str = "reconstructed",
) -> dict:
    """Calcula exit_price, MFE/MAE y deltas a partir de las velas del periodo."""
    base = {
        "trade_id": trade_id,
        "result": result,
        "profit": _f(profit),
        "entry_price": _f(entry_price),
        "close_time": _ts(expiry_ts),
        "result_source": result_source,
        "created_at": created_at,
    }

    entry = _f(entry_price)
    norm = [n for n in (_norm_candle(c) for c in (candles or [])) if n]
    # Velas dentro de la operación: la que abre en/antes del vencimiento y
    # desde la vela de entrada en adelante.
    window = [n for n in norm if n["from"] >= entry_ts - 60 and n["from"] <= expiry_ts]
    if not window:
        window = norm  # fallback: lo que haya

    if entry is None or not window:
        return base

    # exit_price: close de la vela que cubre el vencimiento (o la última).
    exit_price = None
    for n in window:
        if n["from"] <= expiry_ts < n["to"]:
            exit_price = n["close"]
            break
    if exit_price is None:
        exit_price = window[-1]["close"]

    max_price = max(n["high"] for n in window)
    min_price = min(n["low"] for n in window)

    is_call = direction == "call"
    if is_call:
        mfe = max_price - entry
        mae = entry - min_price
    else:
        mfe = entry - min_price
        mae = max_price - entry
    # Excursiones: nunca negativas (si la ventana no tocó esa dirección, es 0).
    mfe = max(mfe, 0.0)
    mae = max(mae, 0.0)

    raw_delta = exit_price - entry
    directional = raw_delta if is_call else -raw_delta

    # Tiempos (aprox. al inicio de la vela correspondiente).
    def secs(cond_key: str):
        for n in window:
            fav = (n["high"] - entry) if is_call else (entry - n["low"])
            adv = (entry - n["low"]) if is_call else (n["high"] - entry)
            if cond_key == "favorable" and fav >= mfe - 1e-12:
                return max(n["from"] - entry_ts, 0.0)
            if cond_key == "adverse" and adv >= mae - 1e-12:
                return max(n["from"] - entry_ts, 0.0)
        return None

    seconds_to_first_profit = None
    for n in window:
        dir_close = (n["close"] - entry) if is_call else (entry - n["close"])
        if dir_close > 0:
            seconds_to_first_profit = max(n["from"] - entry_ts, 0.0)
            break

    base.update({
        "exit_price": exit_price,
        "raw_price_delta": raw_delta,
        "directional_price_delta": directional,
        "win_margin": directional,
        "max_price_during_trade": max_price,
        "min_price_during_trade": min_price,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "seconds_to_first_profit": seconds_to_first_profit,
        "seconds_to_max_favorable": secs("favorable"),
        "seconds_to_max_adverse": secs("adverse"),
    })
    return base
