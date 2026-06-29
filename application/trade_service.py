"""Servicio de trading: aplica el motor de riesgo y enruta la señal.

Orden de decisión:
  1. Filtros normales (RiskEngine):
       - rechazada  -> paper-trade con su reject_reason original.
       - aprobada   -> sigue al paso 2.
  2. Gestión de riesgo (RiskManager), SOLO si pasó los filtros normales:
       - pausa activa -> paper-trade con reject_reason de riesgo
                         (would_be_real_without_risk=1: habría sido real).
       - sin pausa    -> operación real en demo.
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger

from domain.models import RiskDecision, Signal, TradeLogContext
from domain.risk import RiskEngine
from domain.risk_management import RiskManager
from infrastructure.trade_executor import TradeExecutor

_FMT = "%Y-%m-%d %H:%M:%S"


class TradeService:
    def __init__(self, risk: RiskEngine, executor: TradeExecutor,
                 risk_manager: RiskManager | None = None):
        self.risk = risk
        self.executor = executor
        self.risk_manager = risk_manager

    def process_signal(
        self, signal: Signal, payout: float, entry_delay_seconds: float,
        log_ctx: TradeLogContext | None = None, market_type: str = "turbo",
    ) -> RiskDecision:
        decision = self.risk.evaluate(
            signal=signal,
            payout=payout,
            entry_delay_seconds=entry_delay_seconds,
            open_assets=self.executor.open_assets,
        )

        # Logging de filtros normales (no altera la decisión): una fila por filtro.
        if log_ctx is not None:
            try:
                log_ctx.filter_rows = self.risk.evaluate_all(
                    signal=signal, payout=payout,
                    entry_delay_seconds=entry_delay_seconds,
                    open_assets=self.executor.open_assets,
                    reject_reason=decision.reject_reason,
                )
            except Exception:  # noqa: BLE001
                log_ctx.filter_rows = []

        # 1) Rechazada por un filtro normal -> paper como siempre. La capa de
        #    riesgo NO se evalúa ni cambia el motivo (p.ej. lateral_market sigue
        #    siendo lateral_market aunque sea entre 06:00 y 13:00).
        if not decision.approved:
            logger.info(
                f"[{signal.asset}] Señal {signal.direction.value.upper()} "
                f"RECHAZADA por '{decision.reject_reason}' -> paper-trade"
            )
            self.executor.register_virtual(signal, decision, log_ctx)
            return decision

        # 2) Pasó los filtros normales -> capa adicional de gestión de riesgo.
        rm = self.risk_manager
        if rm is not None and rm.enabled:
            rmd = self._evaluate_risk_mgmt(rm, signal)
            if log_ctx is not None:
                try:
                    log_ctx.filter_rows = (log_ctx.filter_rows or []) + rm.filter_rows(rmd)
                except Exception:  # noqa: BLE001
                    pass
                log_ctx.risk_row = rm.risk_row(rmd)
            if rmd.blocked:
                logger.info(
                    f"[{signal.asset}] Señal {signal.direction.value.upper()} "
                    f"habría sido REAL pero PAUSADA por gestión de riesgo "
                    f"'{rmd.reason}' (consec={rmd.consecutive_losses}, "
                    f"ventana={rmd.window_losses}) -> paper-trade"
                )
                self.executor.register_virtual(
                    signal, decision, log_ctx, reject_override=rmd.reason)
                return decision

        # 3) Sin pausa de riesgo (o gestión desactivada) -> operación real en demo.
        self.executor.execute_real(signal, decision, log_ctx, market_type)
        return decision

    def _evaluate_risk_mgmt(self, rm: RiskManager, signal: Signal):
        """Recomputa los contadores desde la BD (solo trades reales cerrados) y
        deja que el RiskManager decida. Cualquier fallo de lectura => no bloquea
        (mejor operar que perder una entrada real por un error de conteo)."""
        db = self.executor.db
        try:
            consecutive, last_close_str = db.real_consecutive_losses()
            window_losses = db.real_losses_in_window_today(
                rm.window_start_hour, rm.window_end_hour)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{signal.asset}] no se pudieron leer contadores de riesgo: {exc}")
            from domain.models import RiskMgmtDecision
            return RiskMgmtDecision()

        last_close = None
        if last_close_str:
            try:
                last_close = datetime.strptime(last_close_str, _FMT)
            except ValueError:
                last_close = None

        return rm.evaluate(
            signal.signal_time,
            consecutive_losses=consecutive,
            last_loss_close_time=last_close,
            window_losses=window_losses,
        )
