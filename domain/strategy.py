"""Estrategias puras del dominio.

`FibPullbackStrategy` se conserva únicamente para reproducir y auditar el
histórico. El runtime usa `ConfirmedBandReversionStrategy`: una estrategia
experimental nueva de reversión de liquidez que exige confirmación después de
un exceso en Bollinger, en vez de interpretar cualquier toque como entrada.

NO sabe nada de IQ Option. Recibe un DataFrame de velas (open, high, low,
close, timestamp) y devuelve un `Signal`.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from domain.models import Direction, Signal


class FibPullbackStrategy:
    """Implementación histórica, fuera de producción desde 2026-07-24."""

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


class ConfirmedBandReversionStrategy(FibPullbackStrategy):
    """Reversión experimental tras rechazo confirmado de una banda.

    Una banda solo define un precio relativamente extremo; no basta para
    operar. La entrada exige, en la misma vela cerrada o tras una vela fuera:

    1. exceso y cierre de vuelta dentro de Bollinger 20x2;
    2. vela y mecha de rechazo en la dirección de la reversión;
    3. RSI previamente extremo y girando;
    4. ADX moderado y rango no explosivo frente a ATR.

    La estrategia es simétrica para CALL/PUT y no usa Fibonacci ni la
    dirección de EMA50/EMA200 para decidir.
    """

    name = "confirmed_band_reversion_v1"
    actionable = True
    description = (
        "Experimental: reingreso Bollinger + rechazo de mecha + giro RSI "
        "+ régimen ADX/ATR; exclusivamente PRACTICE"
    )

    def __init__(
        self,
        ema_fast: int = 50,
        ema_slow: int = 200,
        rsi_period: int = 14,
        bb_period: int = 20,
        bb_mult: float = 2.0,
        fib_lookback: int = 25,
        squeeze_lookback: int = 100,
        atr_period: int = 14,
        adx_period: int = 14,
        reversion_rsi_threshold: float = 35.0,
        reversion_min_rsi_turn: float = 1.5,
        reversion_min_wick_ratio: float = 0.25,
        reversion_max_adx: float = 28.0,
        reversion_max_range_atr: float = 1.8,
    ):
        super().__init__(
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_period=rsi_period,
            bb_period=bb_period,
            bb_mult=bb_mult,
            fib_lookback=fib_lookback,
            squeeze_lookback=squeeze_lookback,
        )
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.reversion_rsi_threshold = reversion_rsi_threshold
        self.reversion_min_rsi_turn = reversion_min_rsi_turn
        self.reversion_min_wick_ratio = reversion_min_wick_ratio
        self.reversion_max_adx = reversion_max_adx
        self.reversion_max_range_atr = reversion_max_range_atr

    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = super().add_indicators(df)

        # Bollinger define su formulación original con desviación poblacional.
        df["bb_std"] = df["close"].rolling(self.bb_period).std(ddof=0)
        df["bb_top"] = df["bb_mid"] + df["bb_std"] * self.bb_mult
        df["bb_bottom"] = df["bb_mid"] - df["bb_std"] * self.bb_mult
        df["bb_width"] = (df["bb_top"] - df["bb_bottom"]) / df["bb_mid"]
        df["bb_width_median"] = df["bb_width"].rolling(self.squeeze_lookback).median()

        prev_close = df["close"].shift(1)
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr"] = true_range.ewm(
            alpha=1 / self.atr_period,
            adjust=False,
            min_periods=self.atr_period,
        ).mean()

        up_move = df["high"].diff()
        down_move = -df["low"].diff()
        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index,
        )
        adx_true_range = true_range.ewm(
            alpha=1 / self.adx_period,
            adjust=False,
            min_periods=self.adx_period,
        ).mean()
        plus_di = 100 * plus_dm.ewm(
            alpha=1 / self.adx_period,
            adjust=False,
            min_periods=self.adx_period,
        ).mean() / adx_true_range.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(
            alpha=1 / self.adx_period,
            adjust=False,
            min_periods=self.adx_period,
        ).mean() / adx_true_range.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        df["adx"] = dx.ewm(
            alpha=1 / self.adx_period,
            adjust=False,
            min_periods=self.adx_period,
        ).mean()

        safe_range = df["range"].replace(0, np.nan)
        body = (df["close"] - df["open"]).abs()
        upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
        lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
        df["body_ratio"] = body / safe_range
        df["upper_wick_ratio"] = upper_wick.clip(lower=0) / safe_range
        df["lower_wick_ratio"] = lower_wick.clip(lower=0) / safe_range
        df["range_atr_ratio"] = df["range"] / df["atr"].replace(0, np.nan)

        return df

    @staticmethod
    def _value(row, column: str) -> float:
        try:
            value = float(row.get(column, 0.0))
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if np.isnan(value) else value

    def _conditions(self, df: pd.DataFrame) -> dict:
        if len(df) < 2:
            return {
                "candidate_direction": "none",
                "rejection_reason": "not_enough_data",
                "actionable": False,
            }

        c = df.iloc[-1]
        p = df.iloc[-2]
        call_band_reentry = (
            (c.low <= c.bb_bottom or p.close < p.bb_bottom)
            and c.close > c.bb_bottom
        )
        put_band_reentry = (
            (c.high >= c.bb_top or p.close > p.bb_top)
            and c.close < c.bb_top
        )
        ambiguous = call_band_reentry and put_band_reentry

        adx_ok = c.adx <= self.reversion_max_adx
        range_atr_ok = 0 < c.range_atr_ratio <= self.reversion_max_range_atr
        call_checks = {
            "band_reentry": call_band_reentry and not ambiguous,
            "candle_confirm": c.close > c.open,
            "rsi_extreme": p.rsi <= self.reversion_rsi_threshold and c.rsi <= 50,
            "rsi_turn": c.rsi - p.rsi >= self.reversion_min_rsi_turn,
            "wick_confirm": c.lower_wick_ratio >= self.reversion_min_wick_ratio,
            "adx_ok": adx_ok,
            "range_atr_ok": range_atr_ok,
        }
        put_checks = {
            "band_reentry": put_band_reentry and not ambiguous,
            "candle_confirm": c.close < c.open,
            "rsi_extreme": p.rsi >= 100 - self.reversion_rsi_threshold and c.rsi >= 50,
            "rsi_turn": p.rsi - c.rsi >= self.reversion_min_rsi_turn,
            "wick_confirm": c.upper_wick_ratio >= self.reversion_min_wick_ratio,
            "adx_ok": adx_ok,
            "range_atr_ok": range_atr_ok,
        }

        if ambiguous:
            candidate = "none"
            checks = call_checks
            rejection = "ambiguous_band_reentry"
        elif call_band_reentry:
            candidate = "call"
            checks = call_checks
            failed = [name for name, passed in checks.items() if not passed]
            rejection = "" if not failed else "call:" + ",".join(failed)
        elif put_band_reentry:
            candidate = "put"
            checks = put_checks
            failed = [name for name, passed in checks.items() if not passed]
            rejection = "" if not failed else "put:" + ",".join(failed)
        else:
            candidate = "none"
            checks = {
                "band_reentry": False,
                "candle_confirm": False,
                "rsi_extreme": False,
                "rsi_turn": False,
                "wick_confirm": False,
                "adx_ok": adx_ok,
                "range_atr_ok": range_atr_ok,
            }
            rejection = "no_band_reentry"

        return {
            "candidate_direction": candidate,
            "rejection_reason": rejection,
            "actionable": candidate in ("call", "put") and all(checks.values()),
            **checks,
            "rsi": self._value(c, "rsi"),
            "prev_rsi": self._value(p, "rsi"),
            "adx": self._value(c, "adx"),
            "atr": self._value(c, "atr"),
            "range_atr_ratio": self._value(c, "range_atr_ratio"),
            "upper_wick_ratio": self._value(c, "upper_wick_ratio"),
            "lower_wick_ratio": self._value(c, "lower_wick_ratio"),
            "bb_top": self._value(c, "bb_top"),
            "bb_bottom": self._value(c, "bb_bottom"),
            "close": self._value(c, "close"),
            "candle_timestamp": self._value(c, "timestamp"),
        }

    def signal_from_frame(self, asset: str, df: pd.DataFrame) -> Signal:
        context = self._conditions(df)
        if len(df) < 2:
            return Signal(
                asset=asset,
                direction=Direction.NONE,
                signal_time=datetime.now(),
                strategy_reason="not_enough_data",
                notes="confirmed_band_reversion: not_enough_data",
            )

        c = df.iloc[-1]
        p = df.iloc[-2]
        signal_time = (
            datetime.fromtimestamp(c.timestamp)
            if "timestamp" in df.columns
            else datetime.now()
        )

        if context["actionable"] and context["candidate_direction"] == "call":
            direction = Direction.CALL
            reason = "confirmed_band_reversion_call"
        elif context["actionable"] and context["candidate_direction"] == "put":
            direction = Direction.PUT
            reason = "confirmed_band_reversion_put"
        else:
            direction = Direction.NONE
            reason = context["rejection_reason"] or "no_signal"

        return Signal(
            asset=asset,
            direction=direction,
            signal_time=signal_time,
            strategy_reason=reason,
            close=self._value(c, "close"),
            rsi=self._value(c, "rsi"),
            prev_rsi=self._value(p, "rsi"),
            bb_width=self._value(c, "bb_width"),
            bb_width_median=self._value(c, "bb_width_median"),
            ema_distance=self._value(c, "ema_distance"),
            candle_range=self._value(c, "range"),
            avg_range_20=self._value(c, "avg_range_20"),
            notes=(
                f"strategy={self.name}; candidate={context['candidate_direction']}; "
                f"rsi={context['rsi']:.2f}; prev_rsi={context['prev_rsi']:.2f}; "
                f"adx={context['adx']:.2f}; atr={context['atr']:.6f}; "
                f"range_atr={context['range_atr_ratio']:.3f}; "
                f"wick_up={context['upper_wick_ratio']:.3f}; "
                f"wick_down={context['lower_wick_ratio']:.3f}; "
                f"decision={reason}"
            ),
        )

    def evaluation_from_frame(self, asset: str, df: pd.DataFrame, signal: Signal) -> dict:
        """Telemetría por activo/vela, incluso cuando no hubo entrada."""
        context = self._conditions(df)
        if len(df) < 2:
            return {}
        return {
            **context,
            "asset": asset,
            "timeframe_seconds": 60,
            "candle_timestamp": datetime.fromtimestamp(
                context["candle_timestamp"]
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "strategy_revision": self.name,
            "signal_direction": signal.direction.value,
        }
