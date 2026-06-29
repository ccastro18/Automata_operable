"""Modelos del dominio. No dependen de IQ Option ni de pandas.

Son las "piezas" que viajan entre la estrategia, el motor de riesgo,
el ejecutor y el almacenamiento.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    CALL = "call"
    PUT = "put"
    NONE = "none"


@dataclass
class Signal:
    """Lo que devuelve la estrategia. No sabe nada de payout ni de filtros."""

    asset: str
    direction: Direction
    signal_time: datetime
    strategy_reason: str  # fib_pullback_call / fib_pullback_put / no_signal / not_enough_data

    # Precio de referencia de entrada (close de la vela que disparó la señal)
    close: float = 0.0

    # Snapshot de indicadores (para métricas y análisis posterior)
    rsi: float = 0.0
    prev_rsi: float = 0.0
    bb_width: float = 0.0
    bb_width_median: float = 0.0
    ema_distance: float = 0.0
    candle_range: float = 0.0
    avg_range_20: float = 0.0

    # Texto descriptivo de condiciones de mercado
    notes: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.direction in (Direction.CALL, Direction.PUT)


@dataclass
class RiskDecision:
    """Resultado del motor de riesgo: ¿se opera o no, y por qué?"""

    approved: bool
    reject_reason: str = ""
    payout: float = 0.0
    entry_delay_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def notes_text(self) -> str:
        return "; ".join(self.notes)


@dataclass
class RiskMgmtDecision:
    """Resultado de la capa de gestión de riesgo (DESPUÉS de los filtros normales).

    Solo se evalúa para señales que ya pasaron todos los filtros normales y que
    habrían sido operación REAL. `blocked=True` => no se envía a IQ, se registra
    como paper con `reason` como reject_reason.
    """

    blocked: bool = False
    reason: str = ""                  # risk_cooldown_2_consecutive_losses | risk_window_6_13_max_3_losses | ""
    pause_until: str | None = None    # "%Y-%m-%d %H:%M:%S" o None
    consecutive_losses: int = 0
    window_losses: int = 0
    in_window: bool = False
    cooldown_active: bool = False
    window_active: bool = False


@dataclass
class TradeLogContext:
    """Contexto satélite a persistir junto a un trade (best-effort).

    No participa en NINGUNA decisión: solo transporta datos a registrar.
    Los dicts vienen SIN trade_id; el ejecutor lo estampa al escribir.
    """

    timeframe_seconds: int = 60
    market_snapshot: dict | None = None        # trade_market_snapshots (1M)
    candle_rows: list = field(default_factory=list)   # trade_candle_snapshots
    api_context: dict | None = None            # trade_api_context
    filter_rows: list = field(default_factory=list)   # trade_filter_evaluations
    risk_row: dict | None = None               # trade_risk_management


@dataclass
class SignalResult:
    """Lo que devuelve la capa de señales: la señal + el contexto de mercado
    ya derivado del frame de indicadores (para no recalcular ni filtrar dos veces).
    """

    signal: "Signal"
    last_close: float
    candle_close_ts: float
    market_snapshot: dict | None = None
    candle_rows: list = field(default_factory=list)
    candle_fetch_ms: float | None = None


class TradeResult(str, Enum):
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"
    PENDING = "pending"
    ERROR = "error"


@dataclass
class TradeRecord:
    """Fila final que se escribe en CSV (real o paper/virtual)."""

    trade_id: str
    date: str
    time: str
    asset: str
    is_otc: bool
    direction: str
    amount: float
    payout: float
    expiration: int
    signal_time: str
    entry_time: str
    entry_delay_seconds: float
    order_id: str
    result: str
    profit: float
    balance_after: float | str
    valid_trade: bool
    reject_reason: str
    bb_width: float
    bb_width_median: float
    ema_distance: float
    candle_range: float
    avg_range_20: float
    rsi: float
    notes: str

    # Orden EXACTO de columnas pedido por el usuario
    COLUMNS = [
        "trade_id", "date", "time", "asset", "is_otc", "direction", "amount",
        "payout", "expiration", "signal_time", "entry_time", "entry_delay_seconds",
        "order_id", "result", "profit", "balance_after", "valid_trade",
        "reject_reason", "bb_width", "bb_width_median", "ema_distance",
        "candle_range", "avg_range_20", "rsi", "notes",
    ]

    def to_row(self) -> dict:
        return asdict(self)
