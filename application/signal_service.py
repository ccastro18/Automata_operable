"""Servicio de señales: une el repositorio de velas con la estrategia.

Devuelve la Signal, el último precio conocido (para resolver paper-trades) y
el contexto de mercado ya derivado del MISMO frame de indicadores que evaluó la
señal (snapshot 1M + velas OHLCV alrededor), para no recalcular ni perder datos.
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
from loguru import logger

from domain.market_snapshot import build_live_snapshot, _ts
from domain.models import Direction, Signal, SignalResult
from domain.strategy import FibPullbackStrategy
from infrastructure.candle_repository import CandleRepository

# Velas OHLCV a guardar alrededor de la señal (reconstrucción visual del mercado).
CANDLE_SNAPSHOT_WINDOW = 60

# Timeframes auxiliares (M5/M15) recolectados SOLO cuando la señal es
# accionable, para no cargar la API con fetches por cada ciclo de escaneo.
# {timeframe_seconds: velas_a_pedir}
_MULTI_TF_TIMEFRAMES: dict[int, int] = {300: 60, 900: 40}


class SignalService:
    def __init__(self, repo: CandleRepository, strategy: FibPullbackStrategy, candle_count: int,
                 collect_multi_tf: bool = True):
        self.repo = repo
        self.strategy = strategy
        self.candle_count = candle_count
        # Flag "Operación" (config/settings.py + config/runtime_config.py):
        # si está apagado, nunca se pide M5/M15 aunque la señal sea accionable.
        self.collect_multi_tf = collect_multi_tf
        # Repos auxiliares sobre el MISMO cliente (sin abrir conexiones nuevas).
        self._extra_repos: dict[int, CandleRepository] = {
            tf: CandleRepository(repo.client, tf) for tf in _MULTI_TF_TIMEFRAMES
        }

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

        multi_tf_snapshots: list[dict] = []
        multi_tf_candle_rows: dict[int, list[dict]] = {}
        multi_tf_latency_ms: dict[int, float] = {}
        # SOLO en señal accionable, y antes de ejecutar/registrar el trade:
        # fetch extra de M5/M15 para el trend multi-timeframe (fuente de
        # verdad para ML futuro). Nunca se pide en cada ciclo de escaneo.
        if self.collect_multi_tf and signal.is_actionable:
            for tf_seconds, count in _MULTI_TF_TIMEFRAMES.items():
                snap, rows, latency_ms = self._fetch_extra_timeframe(asset, tf_seconds, count)
                multi_tf_latency_ms[tf_seconds] = latency_ms
                if snap is not None:
                    multi_tf_snapshots.append(snap)
                if rows:
                    multi_tf_candle_rows[tf_seconds] = rows

        return SignalResult(
            signal=signal,
            last_close=last_close,
            candle_close_ts=candle_close_ts,
            market_snapshot=snapshot,
            candle_rows=candle_rows,
            candle_fetch_ms=fetch_ms,
            multi_tf_snapshots=multi_tf_snapshots,
            multi_tf_candle_rows=multi_tf_candle_rows,
            multi_tf_latency_ms=multi_tf_latency_ms,
        )

    def _fetch_extra_timeframe(self, asset: str, tf_seconds: int,
                               count: int) -> tuple[dict | None, list[dict], float]:
        """Fetch + snapshot de un timeframe auxiliar (M5/M15).

        Best-effort: si falla, devuelve (None, [], latencia) y solo deja un
        warning en el log. NUNCA debe bloquear ni retrasar el registro del
        trade (los datos auxiliares son secundarios).

        A diferencia del M1 de la señal, aquí NO se hace `.dropna()`: con
        60/40 velas no alcanza para calentar ema200 (necesita ~200), así que
        forzar dropna dejaría el frame vacío y se perderían hasta las velas
        OHLCV crudas. build_live_snapshot ya es NaN-safe por campo (via _f),
        así que los indicadores sin suficiente historia quedan en None/NULL
        limpiamente y el resto (open/high/low/close/volume) se conserva.
        """
        repo = self._extra_repos[tf_seconds]
        t0 = time.perf_counter()
        try:
            df = repo.get_dataframe(asset, count)
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.perf_counter() - t0) * 1000
            logger.warning(f"[{asset}] fetch de velas tf={tf_seconds}s falló: {exc}")
            return None, [], latency_ms
        latency_ms = (time.perf_counter() - t0) * 1000

        if df.empty:
            logger.warning(f"[{asset}] sin velas tf={tf_seconds}s para snapshot multi-timeframe.")
            return None, [], latency_ms

        try:
            enriched_tf = self.strategy.add_indicators(df)  # sin dropna: ver docstring
            snapshot = build_live_snapshot(enriched_tf, tf_seconds)
            rows = self._candle_rows(enriched_tf, window=count)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{asset}] snapshot multi-timeframe tf={tf_seconds}s falló: {exc}")
            return None, [], latency_ms
        return snapshot, rows, latency_ms

    @staticmethod
    def _candle_rows(enriched: pd.DataFrame, window: int = CANDLE_SNAPSHOT_WINDOW) -> list[dict]:
        tail = enriched.tail(window)
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
