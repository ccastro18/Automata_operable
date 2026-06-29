"""Estrategia Fib Pullback 5M.

NO sabe nada de IQ Option. Recibe un DataFrame de velas (con columnas
open, high, low, close, timestamp) y devuelve un Signal (CALL/PUT/NONE).

Replica el script de IQ Option / Quadcode:
  - EMA 50 / 200, RSI 14, Bollinger 20x2, Fibonacci sobre swing de 25 velas.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from domain.models import Direction, Signal


class FibPullbackStrategy:
    def __init__(
        self,
        ema_fast: int = 50,
        ema_slow: int = 200,
        rsi_period: int = 14,
        bb_period: int = 20,
        bb_mult: float = 2,
        fib_lookback: int = 25,
        squeeze_lookback: int = 100,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.bb_mult = bb_mult
        self.fib_lookback = fib_lookback
        self.squeeze_lookback = squeeze_lookback

    # ------------------------------------------------------------------ #
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        df["ema50"] = df["close"].ewm(span=self.ema_fast, adjust=False).mean()
        df["ema200"] = df["close"].ewm(span=self.ema_slow, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / self.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / self.rsi_period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

        df["bb_mid"] = df["close"].rolling(self.bb_period).mean()
        df["bb_std"] = df["close"].rolling(self.bb_period).std()
        df["bb_top"] = df["bb_mid"] + df["bb_std"] * self.bb_mult
        df["bb_bottom"] = df["bb_mid"] - df["bb_std"] * self.bb_mult
        df["bb_width"] = (df["bb_top"] - df["bb_bottom"]) / df["bb_mid"]
        df["bb_width_median"] = df["bb_width"].rolling(self.squeeze_lookback).median()

        df["swing_high"] = df["high"].rolling(self.fib_lookback).max()
        df["swing_low"] = df["low"].rolling(self.fib_lookback).min()
        df["swing_range"] = df["swing_high"] - df["swing_low"]

        df["fib_call_382"] = df["swing_high"] - df["swing_range"] * 0.382
        df["fib_call_618"] = df["swing_high"] - df["swing_range"] * 0.618
        df["fib_put_382"] = df["swing_low"] + df["swing_range"] * 0.382
        df["fib_put_618"] = df["swing_low"] + df["swing_range"] * 0.618

        df["range"] = df["high"] - df["low"]
        df["avg_range_20"] = df["range"].rolling(20).mean()
        df["ema_distance"] = (df["ema50"] - df["ema200"]).abs()

        return df

    # ------------------------------------------------------------------ #
    def evaluate(self, asset: str, df: pd.DataFrame) -> Signal:
        """Atajo: enriquece el frame y evalúa. Comportamiento idéntico al previo."""
        return self.signal_from_frame(asset, self.add_indicators(df).dropna())

    def signal_from_frame(self, asset: str, df: pd.DataFrame) -> Signal:
        """Evalúa sobre un frame YA enriquecido (add_indicators + dropna).

        Separado de `evaluate` para que la capa de señales pueda reutilizar el
        mismo frame de indicadores al construir el snapshot de mercado, sin
        recalcular ni cambiar la lógica.
        """
        if len(df) < 210:
            return Signal(
                asset=asset,
                direction=Direction.NONE,
                signal_time=datetime.now(),
                strategy_reason="not_enough_data",
            )

        c = df.iloc[-1]
        p = df.iloc[-2]

        signal_time = (
            datetime.fromtimestamp(c.timestamp)
            if "timestamp" in df.columns
            else datetime.now()
        )

        trend_up = c.close > c.ema200 and c.ema50 > c.ema200
        trend_down = c.close < c.ema200 and c.ema50 < c.ema200

        in_call_fib_zone = (
            c.swing_range > 0
            and c.close <= c.fib_call_382
            and c.close >= c.fib_call_618 - c.swing_range * 0.10
        )
        in_put_fib_zone = (
            c.swing_range > 0
            and c.close >= c.fib_put_382
            and c.close <= c.fib_put_618 + c.swing_range * 0.10
        )

        call_touch = c.low <= c.ema50 or c.low <= c.bb_mid
        put_touch = c.high >= c.ema50 or c.high >= c.bb_mid

        call_rsi = c.rsi >= 51 and c.rsi > p.rsi
        put_rsi = c.rsi <= 49 and c.rsi < p.rsi

        call_candle = c.close > c.open and c.close < c.bb_top
        put_candle = c.close < c.open and c.close > c.bb_bottom

        def build_signal(direction: Direction, reason: str) -> Signal:
            return Signal(
                asset=asset,
                direction=direction,
                signal_time=signal_time,
                strategy_reason=reason,
                close=float(c.close),
                rsi=float(c.rsi),
                prev_rsi=float(p.rsi),
                bb_width=float(c.bb_width),
                bb_width_median=float(c.bb_width_median),
                ema_distance=float(c.ema_distance),
                candle_range=float(c["range"]),
                avg_range_20=float(c.avg_range_20),
                notes=self._build_notes(c, direction),
            )

        if trend_up and in_call_fib_zone and call_touch and call_candle and call_rsi:
            return build_signal(Direction.CALL, "fib_pullback_call")

        if trend_down and in_put_fib_zone and put_touch and put_candle and put_rsi:
            return build_signal(Direction.PUT, "fib_pullback_put")

        return build_signal(Direction.NONE, "no_signal")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_notes(c, direction: Direction) -> str:
        """Texto con el contexto de mercado para entender el porqué del resultado."""
        trend = "alcista" if c.ema50 > c.ema200 else "bajista"
        if direction == Direction.CALL:
            pos = "pullback a EMA50/BB" if c.low <= c.bb_mid else "rebote zona dinámica"
        elif direction == Direction.PUT:
            pos = "pullback a EMA50/BB" if c.high >= c.bb_mid else "rebote zona dinámica"
        else:
            pos = "sin pullback"
        return (
            f"tendencia={trend}; rsi={c.rsi:.1f}; "
            f"bb_width={c.bb_width:.4f} (med={c.bb_width_median:.4f}); "
            f"ema_dist={c.ema_distance:.5f}; rango={c['range']:.5f}; ctx={pos}"
        )
