"""Ejecutor de operaciones.

Dos caminos:
  - REAL (demo): la señal pasó todos los filtros -> se compra en IQ Option.
    El resultado (check_win_v2 es bloqueante ~5 min) se espera en un hilo
    aparte para no frenar el escaneo de velas.
  - VIRTUAL (paper): la señal fue rechazada por un filtro -> NO se opera,
    pero se simula para medir si el filtro realmente aportó. Se resuelve
    comparando el precio de entrada con el precio al vencimiento.

El ejecutor NO decide nada: solo ejecuta lo que el motor de riesgo aprobó.
Persiste todo en SQLite (tabla `trades`, columna `kind` = real | paper).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger

from domain.models import (
    Direction, RiskDecision, Signal, TradeLogContext, TradeRecord, TradeResult,
)
from domain.outcomes import build_price_path, reconstruct_outcome
from infrastructure.database import Database
from infrastructure.iqoption_client import IQOptionClient


def is_otc_asset(asset: str) -> bool:
    return asset.upper().endswith("-OTC") or asset.upper().endswith("OTC")


@dataclass
class PaperTrade:
    trade_id: str
    signal: Signal
    decision: RiskDecision
    amount: float
    payout: float
    expiration: int
    entry_time: datetime
    expiry_time: datetime
    entry_price: float
    is_otc: bool


class TradeExecutor:
    def __init__(
        self,
        client: IQOptionClient,
        db: Database,
        amount: float,
        expiration_minutes: int,
        allow_digital: bool = True,
    ):
        self.client = client
        self.db = db
        self.amount = amount
        self.expiration_minutes = expiration_minutes
        # Si en una binaria asumida IQ rechaza la orden, ¿intentar digital antes de paper?
        self.allow_digital = allow_digital

        self.open_assets: set[str] = set()
        self._pending_paper: list[PaperTrade] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------ #
    #  REAL (demo)
    # ------------------------------------------------------------------ #
    def execute_real(self, signal: Signal, decision: RiskDecision,
                     log_ctx: TradeLogContext | None = None,
                     market_type: str = "turbo") -> bool:
        # Caminos "compra-primero" (si no se puede montar -> paper, sin fila huérfana):
        #  - digital: otra API de compra/resultado.
        #  - binary_assumed: binaria sin payout real, con payout asumido configurado.
        if market_type == "digital":
            return self._execute_buy_first(signal, decision, log_ctx, "digital", "digital_unavailable")
        if market_type == "binary_assumed":
            return self._execute_buy_first(signal, decision, log_ctx, "binary_assumed", "no_real_instrument")

        asset = signal.asset
        action = signal.direction.value
        now = datetime.now()
        trade_id = self._new_id()

        # Persistir ANTES de llamar a IQ Option. Si la compra se acepta y luego
        # la app se cae, queda al menos la evidencia del intento en SQLite.
        pending = self._build_record(
            trade_id=trade_id, signal=signal, decision=decision, entry_time=now,
            order_id="BUY_REQUESTED", result=TradeResult.PENDING.value, profit=0.0,
            balance_after="", valid_trade=True, reject_reason=decision.reject_reason,
        )
        row_id = self.db.insert_trade("real", pending, entry_price=signal.close)
        logger.info(
            f"[{asset}] intento REAL {action.upper()} registrado en BD "
            f"(row={row_id}, trade_id={trade_id}); enviando compra a IQ Option."
        )

        t_buy = time.perf_counter()
        try:
            ok, order_id = self.client.buy(self.amount, asset, action, self.expiration_minutes)
        except Exception as exc:  # noqa: BLE001
            self._mark_real_error(
                trade_id=trade_id,
                asset=asset,
                order_id="BUY_EXCEPTION",
                reason="buy_exception",
                message=f"Error al comprar: {exc}",
            )
            return False

        if not ok:
            self._mark_real_error(
                trade_id=trade_id,
                asset=asset,
                order_id=order_id or "BUY_REJECTED",
                reason="buy_rejected",
                message=f"La compra fue rechazada por IQ Option (id={order_id}).",
            )
            return False

        try:
            self.db.update_trade_order(trade_id, order_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"[{asset}] compra aceptada id={order_id}, pero no se pudo "
                f"actualizar order_id en BD todavía: {exc}"
            )

        with self._lock:
            self.open_assets.add(asset)

        logger.success(
            f"[{asset}] OPERACIÓN {action.upper()} demo | id={order_id} | "
            f"monto={self.amount} | payout={decision.payout:.2f} | trade_id={trade_id}"
        )

        # Logging satélite (best-effort): nunca debe afectar la operación.
        if log_ctx is not None:
            log_ctx.api_context = {**(log_ctx.api_context or {}),
                                   "api_latency_buy_ms": (time.perf_counter() - t_buy) * 1000}
        self._log_entry_context(trade_id, log_ctx)

        t = threading.Thread(
            target=self._await_real_result,
            args=(trade_id, asset, order_id, signal, decision, now, market_type),
            daemon=True,
        )
        t.start()
        return True

    # ------------------------------------------------------------------ #
    #  REAL — "compra primero" (digital / binaria asumida).
    #  Si no se puede montar la orden -> cae a paper (sin fila 'real' huérfana).
    # ------------------------------------------------------------------ #
    def _execute_buy_first(self, signal: Signal, decision: RiskDecision,
                           log_ctx: TradeLogContext | None,
                           market_type: str, fallback_reason: str) -> bool:
        asset = signal.asset
        action = signal.direction.value

        # Cadena de intentos: monta la orden en el PRIMER tipo que IQ acepte.
        # Prioridad binarias -> binaria asumida; si IQ la rechaza, intenta digital
        # (la lectura de payout digital es poco fiable, así que probamos la compra
        # directa); si ninguna se monta, recién ahí cae a paper.
        if market_type == "binary_assumed":
            chain = ["binary_assumed"] + (["digital"] if self.allow_digital else [])
        else:
            chain = [market_type]

        for mt in chain:
            now = datetime.now()
            t_buy = time.perf_counter()
            ok, order_id = self._try_buy(mt, asset, action)
            if not ok:
                logger.info(f"[{asset}] no se pudo montar como {mt} (id={order_id}); probando siguiente…")
                continue

            trade_id = self._new_id()
            pending = self._build_record(
                trade_id=trade_id, signal=signal, decision=decision, entry_time=now,
                order_id=str(order_id), result=TradeResult.PENDING.value, profit=0.0,
                balance_after="", valid_trade=True, reject_reason=decision.reject_reason,
            )
            row_id = self.db.insert_trade("real", pending, entry_price=signal.close)

            with self._lock:
                self.open_assets.add(asset)

            logger.success(
                f"[{asset}] OPERACIÓN {action.upper()} {mt.upper()} demo | id={order_id} | "
                f"monto={self.amount} | payout={decision.payout:.2f} | trade_id={trade_id} (row={row_id})"
            )

            if log_ctx is not None:
                # Guarda el tipo REALMENTE usado (puede diferir del propuesto).
                log_ctx.api_context = {**(log_ctx.api_context or {}),
                                       "market_type": mt,
                                       "payout_source": mt,
                                       "api_latency_buy_ms": (time.perf_counter() - t_buy) * 1000}
            self._log_entry_context(trade_id, log_ctx)

            t = threading.Thread(
                target=self._await_real_result,
                args=(trade_id, asset, order_id, signal, decision, now, mt),
                daemon=True,
            )
            t.start()
            return True

        logger.warning(
            f"[{asset}] no operable en ningún tipo {chain}; fallback a paper (rechazo={fallback_reason})."
        )
        self.register_virtual(signal, decision, log_ctx, reject_override=fallback_reason)
        return False

    def _try_buy(self, market_type: str, asset: str, action: str):
        """Intenta montar la orden en un tipo concreto. Devuelve (ok, order_id)."""
        if market_type == "digital":
            return self.client.buy_digital(self.amount, asset, action, self.expiration_minutes)
        try:
            return self.client.buy(self.amount, asset, action, self.expiration_minutes)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{asset}] excepción al comprar ({market_type}): {exc}")
            return False, None

    def _await_real_result(self, trade_id, asset, order_id, signal: Signal, decision: RiskDecision,
                           entry_time: datetime, market_type: str = "turbo"):
        # Timeout = expiración + margen (la opción no puede cerrar antes de vencer).
        timeout = self.expiration_minutes * 60 + 120
        logger.info(f"[{asset}] resultado pendiente en BD; esperando cierre id={order_id} ({market_type}).")
        try:
            if market_type == "digital":
                result, profit = self.client.check_result_digital(order_id, timeout=timeout)
            else:
                result, profit = self.client.check_result(order_id, timeout=timeout)
            balance = self.client.get_balance()
        except Exception as exc:  # noqa: BLE001
            result, profit, balance = TradeResult.ERROR.value, 0.0, 0.0
            logger.exception(f"[{asset}] error consultando resultado id={order_id}: {exc}")

        with self._lock:
            self.open_assets.discard(asset)

        try:
            self.db.update_trade_result(trade_id, result, profit, balance, order_id=order_id)
        except LookupError:
            logger.error(
                f"[{asset}] no existía fila para trade_id={trade_id}; se reinsertará "
                "con el resultado final para no perder la operación."
            )
            recovered = self._build_record(
                trade_id=trade_id, signal=signal, decision=decision, entry_time=entry_time,
                order_id=str(order_id), result=result, profit=profit,
                balance_after=balance, valid_trade=True, reject_reason=decision.reject_reason,
            )
            self.db.insert_trade("real", recovered, entry_price=signal.close)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{asset}] NO se pudo guardar el resultado en BD: {exc}")
            return
        realized = self._realized_payout(result, profit, self.amount)
        extra = f" | payout_real={realized:.2f}" if realized is not None else ""
        logger.info(
            f"[{asset}] RESULTADO {result.upper()} | profit={profit:.2f} | balance={balance:.2f}{extra}"
        )
        self._log_outcome(
            trade_id=trade_id, asset=asset, direction=signal.direction.value,
            entry_price=signal.close, entry_time=entry_time,
            expiration_min=self.expiration_minutes, result=result, profit=profit,
            amount=self.amount,
        )

    def _mark_real_error(self, trade_id: str, asset: str, order_id, reason: str, message: str) -> None:
        try:
            self.db.update_trade_result(
                trade_id=trade_id,
                result=TradeResult.ERROR.value,
                profit=0.0,
                balance_after="",
                notes=message,
                reject_reason=reason,
                order_id=order_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{asset}] NO se pudo marcar error en BD para trade_id={trade_id}: {exc}")
        logger.warning(f"[{asset}] {message} Registrado en BD como ERROR (trade_id={trade_id}).")

    # ------------------------------------------------------------------ #
    #  VIRTUAL (paper)
    # ------------------------------------------------------------------ #
    def register_virtual(self, signal: Signal, decision: RiskDecision,
                         log_ctx: TradeLogContext | None = None,
                         reject_override: str | None = None) -> None:
        # reject_override permite el fallback de digital: la señal fue APROBADA
        # pero no se pudo comprar, así que se registra como paper con ese motivo.
        reject_reason = reject_override if reject_override is not None else decision.reject_reason
        now = datetime.now()
        paper = PaperTrade(
            trade_id=self._new_id(),
            signal=signal,
            decision=decision,
            amount=self.amount,
            payout=decision.payout,
            expiration=self.expiration_minutes,
            entry_time=now,
            expiry_time=now + timedelta(minutes=self.expiration_minutes),
            entry_price=signal.close,
            is_otc=is_otc_asset(signal.asset),
        )
        # Registra YA en BD como 'pending' (con el precio de entrada para poder
        # resolverlo incluso tras reiniciar la app).
        pending = self._build_record(
            trade_id=paper.trade_id, signal=signal, decision=decision, entry_time=now,
            order_id="VIRTUAL", result=TradeResult.PENDING.value, profit=0.0,
            balance_after="", valid_trade=False, reject_reason=reject_reason,
        )
        row_id = self.db.insert_trade("paper", pending, entry_price=signal.close)

        # Logging satélite del contexto de la señal rechazada (best-effort).
        self._log_entry_context(paper.trade_id, log_ctx)

        with self._lock:
            self._pending_paper.append(paper)
        logger.info(
            f"[{signal.asset}] PAPER {signal.direction.value.upper()} "
            f"(rechazo={reject_reason}) registrado en BD "
            f"(row={row_id}, trade_id={paper.trade_id}, pending)"
        )

    def resolve_due_virtuals(self, asset: str, current_price: float, now: datetime) -> None:
        """Resuelve los paper-trades vencidos del activo usando el precio actual."""
        due: list[PaperTrade] = []
        with self._lock:
            still_pending = []
            for p in self._pending_paper:
                if p.signal.asset == asset and now >= p.expiry_time:
                    due.append(p)
                else:
                    still_pending.append(p)
            self._pending_paper = still_pending

        for p in due:
            self._resolve_paper(p, current_price)

    @staticmethod
    def _paper_outcome(direction: Direction, entry_price: float, exit_price: float,
                       amount: float, payout: float) -> tuple[str, float]:
        if exit_price > entry_price:
            won = direction == Direction.CALL
        elif exit_price < entry_price:
            won = direction == Direction.PUT
        else:
            return TradeResult.DRAW.value, 0.0
        if won:
            return TradeResult.WIN.value, round(amount * payout, 2)
        return TradeResult.LOSS.value, round(-amount, 2)

    def _resolve_paper(self, p: PaperTrade, exit_price: float) -> None:
        result, profit = self._paper_outcome(
            p.signal.direction, p.entry_price, exit_price, p.amount, p.payout)
        try:
            self.db.update_trade_result(p.trade_id, result, profit, "")
        except LookupError:
            logger.error(
                f"[{p.signal.asset}] no existía fila paper trade_id={p.trade_id}; "
                "se reinsertará con el resultado final."
            )
            recovered = self._build_record(
                trade_id=p.trade_id, signal=p.signal, decision=p.decision,
                entry_time=p.entry_time, order_id="VIRTUAL", result=result, profit=profit,
                balance_after="", valid_trade=False, reject_reason=p.decision.reject_reason,
            )
            self.db.insert_trade("paper", recovered, entry_price=p.entry_price)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{p.signal.asset}] NO se pudo guardar resultado paper en BD: {exc}")
            return
        logger.info(
            f"[{p.signal.asset}] PAPER {result.upper()} (rechazo={p.decision.reject_reason}) "
            f"entrada={p.entry_price:.5f} salida={exit_price:.5f} profit={profit:.2f}"
        )
        self._log_outcome(
            trade_id=p.trade_id, asset=p.signal.asset, direction=p.signal.direction.value,
            entry_price=p.entry_price, entry_time=p.entry_time,
            expiration_min=p.expiration, result=result, profit=profit,
            amount=p.amount, result_source="virtual",
        )

    # ------------------------------------------------------------------ #
    #  Resolución de pendientes al iniciar la app
    # ------------------------------------------------------------------ #
    def resolve_pending_real(self) -> None:
        for row in self.db.get_pending_trades("real"):
            tid, asset, oid = row.get("trade_id"), row.get("asset"), row.get("order_id")
            try:
                target = int(oid)
            except (TypeError, ValueError):
                self.db.update_trade_result(tid, TradeResult.ERROR.value, 0.0, "")
                continue
            option = self.client._find_closed_option(target)
            if option is not None:
                result, profit = self.client._interpret_option(option)
                self.db.update_trade_result(tid, result, profit, self.client.get_balance())
                logger.info(f"[{asset}] pendiente real resuelto al iniciar -> {result}")
            else:
                self.db.update_trade_result(tid, TradeResult.ERROR.value, 0.0, "")
                logger.warning(f"[{asset}] pendiente real no hallado (id={oid}) -> error")

    def resolve_pending_paper(self) -> None:
        for row in self.db.get_pending_trades("paper"):
            tid, asset = row.get("trade_id"), row.get("asset")
            entry_price = row.get("entry_price")
            direction_str = row.get("direction")
            try:
                entry_time = datetime.strptime(row["entry_time"], "%Y-%m-%d %H:%M:%S")
            except (KeyError, TypeError, ValueError):
                entry_time = None
            if entry_price is None or not direction_str or entry_time is None:
                self.db.update_trade_result(tid, TradeResult.ERROR.value, 0.0, "")
                continue

            exp_min = int(row.get("expiration") or self.expiration_minutes)
            expiry = entry_time + timedelta(minutes=exp_min)
            direction = Direction(direction_str)
            amount = float(row.get("amount") or self.amount)
            payout = float(row.get("payout") or 0.0)

            if datetime.now() >= expiry:
                exit_price = self.client.price_at(asset, expiry.timestamp())
                if exit_price is None:
                    self.db.update_trade_result(tid, TradeResult.ERROR.value, 0.0, "")
                    continue
                result, profit = self._paper_outcome(direction, entry_price, exit_price, amount, payout)
                self.db.update_trade_result(tid, result, profit, "")
                logger.info(f"[{asset}] pendiente paper resuelto al iniciar -> {result}")
            else:
                # Aún no vence: recargar en memoria para resolverlo normalmente.
                sig = Signal(asset, direction, entry_time, "reloaded", close=entry_price)
                dec = RiskDecision(approved=False, reject_reason=row.get("reject_reason") or "",
                                   payout=payout)
                paper = PaperTrade(
                    trade_id=tid, signal=sig, decision=dec, amount=amount, payout=payout,
                    expiration=exp_min, entry_time=entry_time, expiry_time=expiry,
                    entry_price=entry_price, is_otc=is_otc_asset(asset),
                )
                with self._lock:
                    self._pending_paper.append(paper)
                logger.info(f"[{asset}] pendiente paper recargado (vence {expiry:%H:%M:%S}).")

    # ------------------------------------------------------------------ #
    #  Logging satélite (best-effort: jamás propaga excepciones al trade)
    # ------------------------------------------------------------------ #
    def _log_entry_context(self, trade_id: str, log_ctx: TradeLogContext | None) -> None:
        if log_ctx is None:
            return
        if log_ctx.market_snapshot:
            self._safe(lambda: self.db.write_market_snapshot(
                {**log_ctx.market_snapshot, "trade_id": trade_id}), trade_id, "market_snapshot")
        if log_ctx.candle_rows:
            self._safe(lambda: self.db.write_candle_snapshots(
                trade_id, log_ctx.timeframe_seconds, log_ctx.candle_rows), trade_id, "candles")
        if log_ctx.filter_rows:
            self._safe(lambda: self.db.write_filter_evaluations(
                trade_id, log_ctx.filter_rows), trade_id, "filters")
        if log_ctx.risk_row:
            self._safe(lambda: self.db.write_risk_management(
                {**log_ctx.risk_row, "trade_id": trade_id}), trade_id, "risk_mgmt")
        if log_ctx.api_context:
            self._safe(lambda: self.db.write_api_context(
                {**log_ctx.api_context, "trade_id": trade_id}), trade_id, "api_context")
            # Espeja el tipo de instrumento en la propia tabla trades (consulta fácil).
            mt = log_ctx.api_context.get("market_type")
            if mt:
                self._safe(lambda: self.db.update_trade_market_type(trade_id, mt),
                           trade_id, "market_type")

    def _log_outcome(self, *, trade_id, asset, direction, entry_price, entry_time,
                     expiration_min, result, profit, amount=None,
                     result_source="reconstructed") -> None:
        realized = self._realized_payout(result, profit, amount if amount is not None else self.amount)

        def work():
            entry_ts = entry_time.timestamp()
            expiry_ts = entry_ts + expiration_min * 60
            # Velas de 1M que cubren la operación (+margen) para reconstruir el recorrido.
            count = expiration_min + 6
            candles = self.client.get_candles(asset, 60, count, endtime=expiry_ts + 120) or []
            outcome = reconstruct_outcome(
                trade_id=trade_id, direction=direction, entry_price=entry_price,
                entry_ts=entry_ts, expiry_ts=expiry_ts, candles=candles,
                result=result, profit=profit, created_at=self.db._now(),
                result_source=result_source,
            )
            # Payout REAL derivado del profit (descubre el payout efectivo del activo).
            outcome["realized_payout"] = realized
            self.db.write_outcome(outcome)
            # Recorrido del precio durante la operación (mismas velas, sin red extra).
            points = build_price_path(candles, entry_ts, expiry_ts)
            if points:
                self.db.write_price_path(trade_id, points)
        self._safe(work, trade_id, "outcome")

    @staticmethod
    def _realized_payout(result: str, profit: float, amount: float):
        """Payout real (fracción) a partir del profit. Solo en WIN: profit/amount.

        En un WIN, profit = win_amount - amount (neto), así que profit/amount es
        exactamente el payout efectivo que pagó el activo. None en loss/draw.
        """
        if result == TradeResult.WIN.value and amount and amount > 0 and profit > 0:
            return round(profit / amount, 4)
        return None

    @staticmethod
    def _safe(fn, trade_id: str, what: str) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{trade_id}] no se pudo registrar {what}: {exc}")

    # ------------------------------------------------------------------ #
    def _build_record(
        self, trade_id, signal: Signal, decision: RiskDecision, entry_time: datetime,
        order_id, result, profit, balance_after, valid_trade, reject_reason,
    ) -> TradeRecord:
        notes = signal.notes
        if decision.notes:
            notes = f"{notes} | {decision.notes_text()}"
        return TradeRecord(
            trade_id=trade_id,
            date=entry_time.strftime("%Y-%m-%d"),
            time=entry_time.strftime("%H:%M:%S"),
            asset=signal.asset,
            is_otc=is_otc_asset(signal.asset),
            direction=signal.direction.value,
            amount=self.amount,
            payout=round(decision.payout, 4),
            expiration=self.expiration_minutes,
            signal_time=signal.signal_time.strftime("%Y-%m-%d %H:%M:%S"),
            entry_time=entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            entry_delay_seconds=round(decision.entry_delay_seconds, 2),
            order_id=str(order_id),
            result=result,
            profit=round(profit, 4),
            balance_after=(round(balance_after, 2) if isinstance(balance_after, (int, float)) else balance_after),
            valid_trade=valid_trade,
            reject_reason=reject_reason,
            bb_width=round(signal.bb_width, 6),
            bb_width_median=round(signal.bb_width_median, 6),
            ema_distance=round(signal.ema_distance, 6),
            candle_range=round(signal.candle_range, 6),
            avg_range_20=round(signal.avg_range_20, 6),
            rsi=round(signal.rsi, 2),
            notes=notes,
        )
