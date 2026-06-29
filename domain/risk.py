"""Motor de riesgo / filtros de "NO operar".

Recibe una Signal accionable + contexto (payout, retraso de entrada, activos
con operación abierta) y decide si se ejecuta en demo o se manda a paper-trade.

En esta fase de estadísticas NO hay límites diarios ni stop. Los filtros
activos son:
  - payout mínimo
  - squeeze de Bollinger (bandas comprimidas)
  - vela gigante
  - mercado lateral (EMA50 ~ EMA200)
  - entrada tarde (> N segundos de la nueva vela)
  - operación simultánea en el mismo activo
"""
from __future__ import annotations

from domain.models import RiskDecision, Signal


class RiskEngine:
    def __init__(
        self,
        min_payout: float,
        squeeze_factor: float,
        giant_candle_factor: float,
        lateral_factor: float,
        max_entry_delay_seconds: float,
        reject_low_payout: bool = True,
        reject_bb_squeeze: bool = True,
        reject_giant_candle: bool = True,
        reject_lateral_market: bool = True,
        reject_late_entry: bool = True,
    ):
        self.min_payout = min_payout
        self.squeeze_factor = squeeze_factor
        self.giant_candle_factor = giant_candle_factor
        self.lateral_factor = lateral_factor
        self.max_entry_delay_seconds = max_entry_delay_seconds
        self.reject_low_payout = reject_low_payout
        self.reject_bb_squeeze = reject_bb_squeeze
        self.reject_giant_candle = reject_giant_candle
        self.reject_lateral_market = reject_lateral_market
        self.reject_late_entry = reject_late_entry

    def evaluate(
        self,
        signal: Signal,
        payout: float,
        entry_delay_seconds: float,
        open_assets: set[str],
    ) -> RiskDecision:
        notes: list[str] = []
        permitted: list[str] = []

        # 1a) Payout NO disponible (bug de lectura / 0 / None). NO mezclar con
        # low_payout real: registrar como 'payout_unavailable'.
        if payout is None or payout <= 0:
            return self._reject(
                "payout_unavailable", payout or 0.0, entry_delay_seconds,
                ["payout=0/None (no disponible)"],
            )

        # 1b) Payout mínimo
        if payout < self.min_payout:
            note = f"payout={payout:.2f} < {self.min_payout:.2f}"
            if self.reject_low_payout:
                return self._reject("low_payout", payout, entry_delay_seconds, notes + [note])
            self._permit("low_payout", note, notes, permitted)

        # 2) Operación simultánea en el mismo activo
        if signal.asset in open_assets:
            note = f"ya hay operación abierta en {signal.asset}"
            return self._reject(
                "already_open", payout, entry_delay_seconds,
                notes + [note],
            )

        # 3) Squeeze Bollinger: bandas comprimidas
        if signal.bb_width_median > 0 and signal.bb_width < signal.bb_width_median * self.squeeze_factor:
            note = f"bb_width={signal.bb_width:.4f} < med*{self.squeeze_factor}"
            if self.reject_bb_squeeze:
                return self._reject("bb_squeeze", payout, entry_delay_seconds, notes + [note])
            self._permit("bb_squeeze", note, notes, permitted)

        # 4) Vela gigante
        if signal.avg_range_20 > 0 and signal.candle_range > signal.avg_range_20 * self.giant_candle_factor:
            note = f"range={signal.candle_range:.5f} > avg*{self.giant_candle_factor}"
            if self.reject_giant_candle:
                return self._reject("giant_candle", payout, entry_delay_seconds, notes + [note])
            self._permit("giant_candle", note, notes, permitted)

        # 5) Mercado lateral (EMA50 ~ EMA200)
        if signal.avg_range_20 > 0 and signal.ema_distance < signal.avg_range_20 * self.lateral_factor:
            note = f"ema_dist={signal.ema_distance:.5f} < avg*{self.lateral_factor}"
            if self.reject_lateral_market:
                return self._reject("lateral_market", payout, entry_delay_seconds, notes + [note])
            self._permit("lateral_market", note, notes, permitted)

        # 6) Entrada tarde
        if entry_delay_seconds > self.max_entry_delay_seconds:
            note = f"delay={entry_delay_seconds:.1f}s > {self.max_entry_delay_seconds}s"
            if self.reject_late_entry:
                return self._reject("late_entry", payout, entry_delay_seconds, notes + [note])
            self._permit("late_entry", note, notes, permitted)

        notes.append(f"payout={payout:.2f}; delay={entry_delay_seconds:.1f}s")
        return RiskDecision(
            approved=True,
            reject_reason=self._permitted_reasons_text(permitted),
            payout=payout,
            entry_delay_seconds=entry_delay_seconds,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    #  Logging de filtros (NO altera la decisión: solo describe cada filtro)
    # ------------------------------------------------------------------ #
    def evaluate_all(
        self,
        signal: Signal,
        payout: float,
        entry_delay_seconds: float,
        open_assets: set[str],
        reject_reason: str = "",
    ) -> list[dict]:
        """Una fila por filtro evaluado, para trade_filter_evaluations.

        `triggered` = la condición de NO operar se cumple para ese filtro.
        `blocked`   = ese filtro fue el que efectivamente rechazó la señal
                      (coincide con `reject_reason`). Solo describe; no decide.
        """
        payout = payout or 0.0
        squeeze_thr = (signal.bb_width_median * self.squeeze_factor
                       if signal.bb_width_median > 0 else None)
        giant_thr = (signal.avg_range_20 * self.giant_candle_factor
                     if signal.avg_range_20 > 0 else None)
        lateral_thr = (signal.avg_range_20 * self.lateral_factor
                       if signal.avg_range_20 > 0 else None)
        payout_unavailable = payout <= 0
        low_payout = not payout_unavailable and payout < self.min_payout
        bb_squeeze = squeeze_thr is not None and signal.bb_width < squeeze_thr
        giant_candle = giant_thr is not None and signal.candle_range > giant_thr
        lateral_market = lateral_thr is not None and signal.ema_distance < lateral_thr
        late_entry = entry_delay_seconds > self.max_entry_delay_seconds

        rows = [
            self._fe("payout", "HARD",
                     triggered=(payout_unavailable or low_payout),
                     value=payout, threshold=self.min_payout,
                     reason=self._filter_reason(
                         "payout no disponible" if payout_unavailable else "payout bajo",
                         payout_unavailable or low_payout,
                         permitted=(low_payout and not self.reject_low_payout),
                     )),
            self._fe("already_open", "HARD",
                     triggered=(signal.asset in open_assets),
                     value=None, threshold=None,
                     reason="operación abierta en el activo" if signal.asset in open_assets else "ok"),
            self._fe("bb_squeeze", "SOFT",
                     triggered=bb_squeeze,
                     value=signal.bb_width, threshold=squeeze_thr,
                     reason=self._filter_reason(
                         "bb_width bajo umbral", bb_squeeze,
                         permitted=(bb_squeeze and not self.reject_bb_squeeze),
                     )),
            self._fe("giant_candle", "SOFT",
                     triggered=giant_candle,
                     value=signal.candle_range, threshold=giant_thr,
                     reason=self._filter_reason(
                         "vela gigante", giant_candle,
                         permitted=(giant_candle and not self.reject_giant_candle),
                     )),
            self._fe("lateral_market", "SOFT",
                     triggered=lateral_market,
                     value=signal.ema_distance, threshold=lateral_thr,
                     reason=self._filter_reason(
                         "mercado lateral", lateral_market,
                         permitted=(lateral_market and not self.reject_lateral_market),
                     )),
            self._fe("late_entry", "HARD",
                     triggered=late_entry,
                     value=entry_delay_seconds, threshold=self.max_entry_delay_seconds,
                     reason=self._filter_reason(
                         "entrada tarde", late_entry,
                         permitted=(late_entry and not self.reject_late_entry),
                     )),
        ]
        # "payout" cubre dos motivos de rechazo: payout_unavailable y low_payout.
        blocking = {"payout"} if reject_reason in ("payout_unavailable", "low_payout") else {reject_reason}
        for r in rows:
            r["blocked"] = 1 if r["filter_name"] in blocking else 0
        return rows

    @staticmethod
    def _fe(name, ftype, *, triggered, value, threshold, reason) -> dict:
        return {
            "filter_name": name,
            "filter_type": ftype,
            "triggered": 1 if triggered else 0,
            "blocked": 0,  # se ajusta en evaluate_all según el reject_reason real
            "value": float(value) if value is not None else None,
            "threshold": float(threshold) if threshold is not None else None,
            "reason": reason,
        }

    @staticmethod
    def _permit(reason: str, note: str, notes: list[str], permitted: list[str]) -> None:
        permitted.append(reason)
        notes.append(f"{note}; permitido por usuario")

    @staticmethod
    def _permitted_reasons_text(permitted: list[str]) -> str:
        return ";".join(f"{reason}_permitido_por_usuario" for reason in permitted)

    @staticmethod
    def _filter_reason(reason: str, triggered: bool, *, permitted: bool = False) -> str:
        if not triggered:
            return "ok"
        if permitted:
            return f"{reason}; permitido por usuario"
        return reason

    @staticmethod
    def _reject(reason, payout, delay, notes) -> RiskDecision:
        return RiskDecision(
            approved=False,
            reject_reason=reason,
            payout=payout,
            entry_delay_seconds=delay,
            notes=notes,
        )
