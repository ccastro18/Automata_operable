"""Capa ADICIONAL de gestión de riesgo (se evalúa DESPUÉS de los filtros normales).

Orden global de decisión:
  1. Filtros normales existentes (RiskEngine): payout, already_open, giant_candle,
     lateral_market, late_entry, etc. Si rechazan -> paper con su reject_reason
     original (esta capa NO los toca).
  2. SOLO si la señal pasó todos los filtros normales (habría sido REAL) se evalúa
     esta capa. Si pausa el riesgo -> paper con un reject_reason de riesgo, pero
     marcando que habría sido real (would_be_real_without_risk=1).

Dos reglas:
  A. Cooldown por 2 pérdidas reales consecutivas -> pausa 15 min desde el
     close_time de la 2ª pérdida.
  B. Dentro de 06:00-13:00, si se acumulan 3 pérdidas reales en esa ventana ->
     pausa hasta las 13:00 del mismo día.

Prioridad cuando ambas aplican: gana la de ventana (más restrictiva, hasta 13:00).

Los contadores son responsabilidad de quien llama (se recomputan desde la BD a
partir de operaciones REALES cerradas). Este módulo es puro: recibe los números
y decide. Así es testeable sin BD y consistente con RiskEngine.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.models import RiskMgmtDecision

COOLDOWN_REASON = "risk_cooldown_2_consecutive_losses"
WINDOW_REASON = "risk_window_6_13_max_3_losses"
_FMT = "%Y-%m-%d %H:%M:%S"


class RiskManager:
    def __init__(
        self,
        enabled: bool = True,
        cooldown_losses: int = 2,
        cooldown_minutes: int = 15,
        window_start_hour: int = 6,
        window_end_hour: int = 13,
        window_max_losses: int = 3,
    ):
        self.enabled = enabled
        self.cooldown_losses = cooldown_losses
        self.cooldown_minutes = cooldown_minutes
        self.window_start_hour = window_start_hour
        self.window_end_hour = window_end_hour
        self.window_max_losses = window_max_losses

    # ------------------------------------------------------------------ #
    def in_window(self, t: datetime) -> bool:
        """True si la hora cae en [start, end): 06:00:00 <= t < 13:00:00."""
        return self.window_start_hour <= t.hour < self.window_end_hour

    def evaluate(
        self,
        now: datetime,
        *,
        consecutive_losses: int,
        last_loss_close_time: datetime | None,
        window_losses: int,
    ) -> RiskMgmtDecision:
        # --- Regla A: cooldown por pérdidas consecutivas ---
        cooldown_until = None
        cooldown_active = False
        if consecutive_losses >= self.cooldown_losses and last_loss_close_time is not None:
            cooldown_until = last_loss_close_time + timedelta(minutes=self.cooldown_minutes)
            cooldown_active = now < cooldown_until

        # --- Regla B: tope de pérdidas dentro de la ventana 06:00-13:00 ---
        inwin = self.in_window(now)
        window_active = inwin and window_losses >= self.window_max_losses
        window_until = None
        if window_active:
            window_until = now.replace(
                hour=self.window_end_hour, minute=0, second=0, microsecond=0
            )

        # Prioridad: ventana primero (pausa hasta 13:00 es más restrictiva).
        if window_active:
            reason, pause_until = WINDOW_REASON, window_until
        elif cooldown_active:
            reason, pause_until = COOLDOWN_REASON, cooldown_until
        else:
            reason, pause_until = "", None

        return RiskMgmtDecision(
            blocked=bool(reason),
            reason=reason,
            pause_until=pause_until.strftime(_FMT) if pause_until else None,
            consecutive_losses=consecutive_losses,
            window_losses=window_losses,
            in_window=inwin,
            cooldown_active=cooldown_active,
            window_active=window_active,
        )

    # ------------------------------------------------------------------ #
    #  Logging de filtros (1 fila por regla, igual que RiskEngine.evaluate_all).
    #  triggered = la pausa de esa regla está activa AHORA.
    #  blocked   = esa regla fue la que efectivamente mandó la señal a paper.
    # ------------------------------------------------------------------ #
    def filter_rows(self, d: RiskMgmtDecision) -> list[dict]:
        return [
            {
                "filter_name": COOLDOWN_REASON,
                "filter_type": "HARD",
                "triggered": 1 if d.cooldown_active else 0,
                "blocked": 1 if d.reason == COOLDOWN_REASON else 0,
                "value": float(d.consecutive_losses),
                "threshold": float(self.cooldown_losses),
                "reason": ("Paused after 2 consecutive real losses"
                           if d.cooldown_active else "ok"),
            },
            {
                "filter_name": WINDOW_REASON,
                "filter_type": "HARD",
                "triggered": 1 if d.window_active else 0,
                "blocked": 1 if d.reason == WINDOW_REASON else 0,
                "value": float(d.window_losses),
                "threshold": float(self.window_max_losses),
                "reason": ("Paused until 13:00 after 3 real losses inside "
                           "06:00-13:00 window" if d.window_active else "ok"),
            },
        ]

    def risk_row(self, d: RiskMgmtDecision) -> dict:
        """Fila para trade_risk_management (foto del riesgo en la señal)."""
        return {
            "would_be_real_without_risk": 1 if d.blocked else 0,
            "risk_blocked": 1 if d.blocked else 0,
            "risk_block_reason": d.reason or None,
            "risk_pause_until": d.pause_until,
            "risk_consecutive_losses_at_signal": d.consecutive_losses,
            "risk_losses_6_13_at_signal": d.window_losses,
            "in_window": 1 if d.in_window else 0,
        }
