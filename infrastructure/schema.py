"""DDL de las tablas satélite para la recolección de contexto de mercado.

La tabla principal `trades` NO se toca: se queda liviana y sigue siendo la
fuente de la operación base. Aquí viven las tablas que cuelgan de ella por
`trade_id` y que guardan, sin inflar `trades`:

  - trade_market_snapshots  -> contexto técnico por timeframe (1M/5M/15M)
  - trade_filter_evaluations -> una fila por filtro evaluado (HARD/SOFT/INFO)
  - strategy_evaluations     -> embudo de condiciones por activo/vela, aun sin trade
  - trade_candle_snapshots   -> OHLCV alrededor de la señal (reconstrucción visual)
  - trade_price_path         -> recorrido del precio durante la operación (fase 2)
  - trade_api_context        -> estado de la API en el momento de la señal
  - trade_outcomes           -> métricas finales (exit_price, MFE, MAE, ...)

NOTA sobre claves foráneas: SQLite no las exige salvo PRAGMA foreign_keys=ON
(que esta app no activa, igual que en el resto del proyecto). Se declaran como
documentación de la relación; la integridad se mantiene desde el código.
"""
from __future__ import annotations

# Cada bloque es CREATE TABLE IF NOT EXISTS para que sea aditivo e idempotente.
SATELLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_market_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT NOT NULL,
    timeframe_seconds   INTEGER NOT NULL,   -- 60, 300, 900
    candle_timestamp    TEXT,

    open                REAL,
    high                REAL,
    low                 REAL,
    close               REAL,
    volume              REAL,

    ema50               REAL,
    ema200              REAL,
    ema_distance        REAL,
    ema_distance_pct    REAL,
    ema_slope_50_1      REAL,
    ema_slope_50_3      REAL,
    ema_slope_200_1     REAL,
    ema_slope_200_3     REAL,

    rsi                 REAL,
    rsi_prev            REAL,
    rsi_delta           REAL,

    bb_top              REAL,
    bb_mid              REAL,
    bb_bottom           REAL,
    bb_width            REAL,
    bb_width_pct        REAL,
    bb_width_median_100 REAL,
    bb_squeeze_ratio    REAL,
    bb_width_slope_3    REAL,
    close_position_in_bb REAL,

    swing_high          REAL,
    swing_low           REAL,
    swing_range         REAL,
    fib_position        REAL,
    distance_to_fib_382 REAL,
    distance_to_fib_500 REAL,
    distance_to_fib_618 REAL,

    candle_range        REAL,
    avg_range_20        REAL,
    candle_range_ratio  REAL,
    atr_14              REAL,
    adx_14              REAL,
    range_atr_ratio     REAL,
    body_size           REAL,
    body_ratio          REAL,
    upper_wick_ratio    REAL,
    lower_wick_ratio    REAL,

    trend_label         TEXT,               -- bullish, bearish, lateral
    source              TEXT,               -- live, backfill
    created_at          TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS trade_filter_evaluations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT NOT NULL,

    filter_name TEXT NOT NULL,
    filter_type TEXT NOT NULL,   -- HARD, SOFT, INFO
    triggered   INTEGER NOT NULL,
    blocked     INTEGER NOT NULL,
    value       REAL,
    threshold   REAL,
    reason      TEXT,

    created_at  TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS strategy_evaluations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset               TEXT NOT NULL,
    timeframe_seconds   INTEGER NOT NULL,
    candle_timestamp    TEXT NOT NULL,
    config_epoch        TEXT,
    strategy_revision   TEXT NOT NULL,
    candidate_direction TEXT,
    signal_direction    TEXT,
    actionable          INTEGER NOT NULL,
    rejection_reason    TEXT,

    band_reentry        INTEGER,
    candle_confirm      INTEGER,
    rsi_extreme         INTEGER,
    rsi_turn            INTEGER,
    wick_confirm        INTEGER,
    adx_ok              INTEGER,
    range_atr_ok        INTEGER,

    rsi                 REAL,
    prev_rsi            REAL,
    adx                 REAL,
    atr                 REAL,
    range_atr_ratio     REAL,
    upper_wick_ratio    REAL,
    lower_wick_ratio    REAL,
    bb_top              REAL,
    bb_bottom           REAL,
    close               REAL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_candle_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id          TEXT NOT NULL,
    timeframe_seconds INTEGER NOT NULL,   -- 60, 300, 900
    candle_index      INTEGER NOT NULL,   -- -N ... 0 (0 = vela de la señal)
    candle_timestamp  TEXT,

    open              REAL,
    high              REAL,
    low               REAL,
    close             REAL,
    volume            REAL,

    created_at        TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS trade_price_path (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT NOT NULL,

    timestamp           TEXT,
    seconds_from_entry  REAL NOT NULL,

    price               REAL,
    candle_open         REAL,
    candle_high         REAL,
    candle_low          REAL,
    candle_close        REAL,

    created_at          TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS trade_api_context (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                    TEXT NOT NULL,

    asset                       TEXT,
    normalized_asset            TEXT,
    market_type                 TEXT,       -- turbo, binary
    asset_exists                INTEGER,
    asset_open                  INTEGER,

    payout                      REAL,
    payout_source               TEXT,       -- turbo, binary, unavailable
    open_time_cache_age_seconds REAL,
    profit_cache_age_seconds    REAL,

    api_latency_candles_ms      REAL,
    api_latency_candles_m5_ms   REAL,   -- fetch M5 (300s), solo en señal accionable
    api_latency_candles_m15_ms  REAL,   -- fetch M15 (900s), solo en señal accionable
    api_latency_profit_ms       REAL,
    api_latency_open_time_ms    REAL,
    api_latency_buy_ms          REAL,

    remaining_to_expiration     REAL,
    connection_ok               INTEGER,

    created_at                  TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS trade_risk_management (
    id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                            TEXT NOT NULL,

    -- 1 = la señal pasó TODOS los filtros normales y habría sido REAL,
    -- pero la capa de gestión de riesgo la bloqueó (quedó como paper).
    would_be_real_without_risk          INTEGER,
    risk_blocked                        INTEGER,   -- 1 = bloqueada por gestión de riesgo
    risk_block_reason                   TEXT,      -- risk_cooldown_2_consecutive_losses | risk_window_6_13_max_3_losses
    risk_pause_until                    TEXT,      -- hasta cuándo dura la pausa (si la hay)

    -- Foto de los contadores en el momento de la señal (para análisis posterior).
    risk_consecutive_losses_at_signal   INTEGER,
    risk_losses_6_13_at_signal          INTEGER,
    in_window                           INTEGER,   -- 1 = signal_time dentro de 06:00-13:00

    created_at                          TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                 TEXT NOT NULL,

    result                   TEXT,          -- win, loss, draw, pending, unknown
    profit                   REAL,

    entry_price              REAL,
    exit_price               REAL,
    raw_price_delta          REAL,
    directional_price_delta  REAL,
    win_margin               REAL,

    max_price_during_trade   REAL,
    min_price_during_trade   REAL,
    max_favorable_excursion  REAL,
    max_adverse_excursion    REAL,

    seconds_to_first_profit  REAL,
    seconds_to_max_favorable REAL,
    seconds_to_max_adverse   REAL,

    close_time               TEXT,
    result_source            TEXT,          -- check_win_v3, check_win_v2, virtual, reconstructed
    realized_payout          REAL,          -- payout real derivado del profit en un WIN (profit/amount)
    -- 1 = exit_price detectado como corrupto (escala de OTRO activo, bug de
    -- condición de carrera en get_candles) y anulado por tools/repair_exit_prices.py.
    -- result/profit NUNCA se tocan: vienen de la API y son correctos.
    outcome_price_suspect    INTEGER,
    created_at               TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_trade_tf
    ON trade_market_snapshots(trade_id, timeframe_seconds);
CREATE INDEX IF NOT EXISTS idx_filters_name_triggered
    ON trade_filter_evaluations(filter_name, triggered);
CREATE INDEX IF NOT EXISTS idx_filters_trade
    ON trade_filter_evaluations(trade_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_eval_candle_epoch
    ON strategy_evaluations(asset, timeframe_seconds, candle_timestamp, config_epoch);
CREATE INDEX IF NOT EXISTS idx_strategy_eval_reason
    ON strategy_evaluations(strategy_revision, rejection_reason);
CREATE INDEX IF NOT EXISTS idx_candles_trade_tf
    ON trade_candle_snapshots(trade_id, timeframe_seconds);
CREATE INDEX IF NOT EXISTS idx_price_path_trade
    ON trade_price_path(trade_id);
CREATE INDEX IF NOT EXISTS idx_api_context_trade
    ON trade_api_context(trade_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_trade
    ON trade_outcomes(trade_id);
CREATE INDEX IF NOT EXISTS idx_risk_mgmt_trade
    ON trade_risk_management(trade_id);
CREATE INDEX IF NOT EXISTS idx_risk_mgmt_blocked
    ON trade_risk_management(risk_blocked, risk_block_reason);

-- Índices adicionales sobre trades para el análisis cruzado pedido.
CREATE INDEX IF NOT EXISTS idx_trades_asset_signal_time
    ON trades(asset, signal_time);
CREATE INDEX IF NOT EXISTS idx_trades_kind_result
    ON trades(kind, result);
"""

# Columnas canónicas de cada tabla satélite (para inserciones por diccionario).
# El builder produce dicts cuyas claves son un subconjunto de estas.
MARKET_SNAPSHOT_COLUMNS = [
    "trade_id", "timeframe_seconds", "candle_timestamp",
    "open", "high", "low", "close", "volume",
    "ema50", "ema200", "ema_distance", "ema_distance_pct",
    "ema_slope_50_1", "ema_slope_50_3", "ema_slope_200_1", "ema_slope_200_3",
    "rsi", "rsi_prev", "rsi_delta",
    "bb_top", "bb_mid", "bb_bottom", "bb_width", "bb_width_pct",
    "bb_width_median_100", "bb_squeeze_ratio", "bb_width_slope_3",
    "close_position_in_bb",
    "swing_high", "swing_low", "swing_range", "fib_position",
    "distance_to_fib_382", "distance_to_fib_500", "distance_to_fib_618",
    "candle_range", "avg_range_20", "candle_range_ratio",
    "atr_14", "adx_14", "range_atr_ratio",
    "body_size", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "trend_label", "source", "created_at",
]
