"""Servicio de señales: une el repositorio de velas con la estrategia.

Devuelve la Signal, el último precio conocido (para resolver paper-trades) y
el contexto de mercado ya derivado del MISMO frame de indicadores que evaluó la
señal (snapshot 1M + velas OHLCV alrededor), para no recalcular ni perder datos.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from domain.market_snapshot import build_live_snapshot, _ts
from domain.models import Direction, Signal, SignalResult
from domain.strategy import FibPullbackStrategy
from infrastructure.candle_repository import CandleRepository

# Velas OHLCV a guardar alrededor de la señal (reconstrucción visual del mercado).
CANDLE_SNAPSHOT_WINDOW = 60


class SignalService:
    def __init__(self, repo: CandleRepository, strategy: FibPullbackStrategy, candle_count: int):
        self.repo = repo
        self.strategy = strategy
        self.candle_count = candle_count

    def get_signal(self, asset: str) -> SignalResult:
        """Devuelve un SignalResult (señal + contexto de mercado).

        candle_close_ts es el timestamp de cierre de la última vela cerrada,
        para calcular el retraso de entrada.
        """
        t0 = time.perf_counter()
        df = self.repo.get_dataframe(asset, self.candle_count)
        fetch_ms = (time.perf_counter() - t0) * 1000

        if df.empty:
            empty = Signal(asset, Direction.NONE, datetime.now(), "no_candles")
            return SignalResult(empty, 0.0, 0.0, candle_fetch_ms=fetch_ms)

        # Un solo cálculo de indicadores, reutilizado para señal y snapshot.
        enriched = self.strategy.add_indicators(df).dropna()
        signal = self.strategy.signal_from_frame(asset, enriched)

        last = df.iloc[-1]
        last_close = float(last["close"])
        candle_close_ts = float(last["timestamp"]) + self.repo.timeframe_seconds

        snapshot = None
        candle_rows: list = []
        if not enriched.empty:
            try:
                snapshot = build_live_snapshot(enriched, self.repo.timeframe_seconds)
                candle_rows = self._candle_rows(enriched)
            except Exception:  # noqa: BLE001 - el snapshot nunca debe romper la señal
                snapshot, candle_rows = None, []

        return SignalResult(
            signal=signal,
            last_close=last_close,
            candle_close_ts=candle_close_ts,
            market_snapshot=snapshot,
            candle_rows=candle_rows,
            candle_fetch_ms=fetch_ms,
        )

    @staticmethod
    def _candle_rows(enriched: pd.DataFrame) -> list[dict]:
        tail = enriched.tail(CANDLE_SNAPSHOT_WINDOW)
        rows = []
        for _, c in tail.iterrows():
            rows.append({
                "candle_timestamp": _ts(c.get("timestamp")),
                "open": _num(c.get("open")), "high": _num(c.get("high")),
                "low": _num(c.get("low")), "close": _num(c.get("close")),
                "volume": _num(c.get("volume")),
            })
        return rows


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f
