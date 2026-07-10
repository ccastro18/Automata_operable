"""Capa de datos en SQLite.

Toda la persistencia (trades reales + paper-trades) y la configuración
controlable desde el panel (activos, estado on/off) viven aquí. Si más
adelante quieres Postgres, este es el único archivo a reescribir.

Thread-safe: el bot escribe desde hilos en segundo plano y el panel web lee
desde el threadpool de FastAPI. Se usa WAL + un Lock global.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from datetime import datetime

from loguru import logger

from domain.market_snapshot import build_backfill_snapshot
from domain.models import TradeRecord
from infrastructure.schema import SATELLITE_SCHEMA

# Mismas 25 columnas del dominio (el bot escribe TradeRecord) + kind.
_TRADE_COLUMNS = TradeRecord.COLUMNS


class Database:
    def __init__(self, db_path: Path):
        self.path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind                TEXT NOT NULL,            -- real | paper
                    trade_id            TEXT,
                    date                TEXT,
                    time                TEXT,
                    asset               TEXT,
                    is_otc              INTEGER,
                    direction           TEXT,
                    amount              REAL,
                    payout              REAL,
                    expiration          INTEGER,
                    signal_time         TEXT,
                    entry_time          TEXT,
                    entry_delay_seconds REAL,
                    order_id            TEXT,
                    result              TEXT,
                    profit              REAL,
                    balance_after       TEXT,
                    valid_trade         INTEGER,
                    reject_reason       TEXT,
                    bb_width            REAL,
                    bb_width_median     REAL,
                    ema_distance        REAL,
                    candle_range        REAL,
                    avg_range_20        REAL,
                    rsi                 REAL,
                    notes               TEXT,
                    entry_price         REAL,
                    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trades_kind ON trades(kind);
                CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
                CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
                CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_trade_id
                    ON trades(trade_id)
                    WHERE trade_id IS NOT NULL AND trade_id != '';

                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            # Migración para BDs antiguas: añade columnas si faltan (IDEMPOTENTE:
            # tolera que ya existan, p.ej. si dos arranques migran casi a la vez).
            self._add_columns_if_missing("trades", [
                ("entry_price", "REAL"), ("updated_at", "TEXT"), ("market_type", "TEXT"),
                # config_epoch: huella de la config vigente cuando se generó el trade
                # (real o paper). Ver config/epoch.py::compute_config_epoch. Migración
                # idempotente: BDs viejas quedan con config_epoch=NULL en filas previas
                # (no se puede reconstruir retroactivamente qué config tenían).
                ("config_epoch", "TEXT"),
            ])
            # Índice sobre config_epoch (creado DESPUÉS de la migración de columna
            # de arriba, porque en instalaciones nuevas la columna no existe en el
            # CREATE TABLE inicial, solo tras _add_columns_if_missing).
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_config_epoch ON trades(config_epoch)"
            )

            # Tablas satélite de contexto de mercado (aditivas, no tocan trades).
            self._conn.executescript(SATELLITE_SCHEMA)
            self._add_columns_if_missing("trade_outcomes", [
                ("realized_payout", "REAL"),
                ("outcome_price_suspect", "INTEGER"),
            ])
            self._add_columns_if_missing("trade_api_context", [
                ("api_latency_candles_m5_ms", "REAL"),
                ("api_latency_candles_m15_ms", "REAL"),
            ])
            self._conn.commit()

        # Backfill fuera del lock (usa métodos que lockean por su cuenta).
        try:
            self._backfill_market_snapshots()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Backfill de trade_market_snapshots omitido: {exc}")

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _add_columns_if_missing(self, table: str, columns: list[tuple[str, str]]) -> None:
        """ALTER TABLE ADD COLUMN idempotente: si la columna ya existe, no falla."""
        existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in columns:
            if col in existing:
                continue
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    # ------------------------------------------------------------------ #
    #  Trades
    # ------------------------------------------------------------------ #
    def insert_trade(self, kind: str, record: TradeRecord, entry_price: float | None = None,
                     config_epoch: str | None = None) -> int:
        """config_epoch: huella de la config vigente al generar el trade (ver
        config/epoch.py). Igual que entry_price/market_type, NO vive en
        TradeRecord (no participa de ninguna decisión de dominio, es
        metadato de auditoría), se pasa aparte y se persiste directo en la
        columna homónima de `trades`."""
        row = record.to_row()
        cols = ["kind"] + _TRADE_COLUMNS + ["entry_price", "config_epoch"]
        values = [kind] + [self._coerce(row[c]) for c in _TRADE_COLUMNS] + [entry_price, config_epoch]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO trades ({','.join(cols)}) VALUES ({placeholders})"
        cur = self._write(sql, values, f"insert trade_id={record.trade_id}")
        row_id = int(cur.lastrowid)
        logger.debug(f"Trade persistido en SQLite row_id={row_id} trade_id={record.trade_id} kind={kind}.")
        return row_id

    def update_trade_order(self, trade_id: str, order_id) -> int:
        """Guarda el order_id real apenas IQ Option lo entrega."""
        cur = self._write(
            "UPDATE trades SET order_id = ?, updated_at = datetime('now') WHERE trade_id = ?",
            [str(order_id), trade_id],
            f"update order trade_id={trade_id}",
        )
        return self._ensure_updated(cur.rowcount, trade_id, "order_id")

    def update_trade_result(self, trade_id: str, result: str, profit: float,
                            balance_after, notes: str | None = None,
                            reject_reason: str | None = None,
                            order_id=None) -> int:
        """Actualiza una fila ya insertada (de 'pending' a su resultado final)."""
        sets = ["result = ?", "profit = ?", "balance_after = ?", "updated_at = datetime('now')"]
        params: list = [result, profit, str(balance_after)]
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        if reject_reason is not None:
            sets.append("reject_reason = ?")
            params.append(reject_reason)
        if order_id is not None:
            sets.append("order_id = ?")
            params.append(str(order_id))
        params.append(trade_id)
        cur = self._write(
            f"UPDATE trades SET {', '.join(sets)} WHERE trade_id = ?",
            params,
            f"update result trade_id={trade_id}",
        )
        return self._ensure_updated(cur.rowcount, trade_id, f"result={result}")

    def get_pending_trades(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades WHERE result = 'pending'"
        params: list = []
        if kind in ("real", "paper"):
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _coerce(v):
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    def recent_trades(self, kind: str = "all", limit: int = 50,
                      since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM trades"
        where, params = self._kind_where(kind, since=since)
        sql += f" {where}"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Estadísticas
    # ------------------------------------------------------------------ #
    def stats(self, kind: str = "all", since: str | None = None) -> dict:
        where, params = self._kind_where(kind, since=since)
        with self._lock:
            totals = self._conn.execute(
                f"""SELECT
                        COUNT(*) AS trades,
                        SUM(result='win')  AS wins,
                        SUM(result='loss') AS losses,
                        SUM(result='draw') AS draws,
                        SUM(result='pending') AS pending,
                        SUM(result='error') AS errors,
                        COALESCE(SUM(profit),0) AS net_profit
                    FROM trades {where}""",
                params,
            ).fetchone()
            by_asset = self._conn.execute(
                f"""SELECT asset,
                        COUNT(*) AS trades,
                        SUM(result='win')  AS wins,
                        SUM(result='loss') AS losses,
                        COALESCE(SUM(profit),0) AS net_profit
                    FROM trades {where}
                    GROUP BY asset ORDER BY trades DESC""",
                params,
            ).fetchall()
            by_direction = self._conn.execute(
                f"""SELECT direction,
                        COUNT(*) AS trades,
                        SUM(result='win')  AS wins,
                        SUM(result='loss') AS losses,
                        COALESCE(SUM(profit),0) AS net_profit
                    FROM trades {where}
                    GROUP BY direction""",
                params,
            ).fetchall()
            by_day = self._conn.execute(
                f"""SELECT date,
                        COUNT(*) AS trades,
                        SUM(result='win')  AS wins,
                        SUM(result='loss') AS losses,
                        COALESCE(SUM(profit),0) AS net_profit
                    FROM trades {where}
                    GROUP BY date ORDER BY date DESC LIMIT 30""",
                params,
            ).fetchall()
            by_reject_since, by_reject_params = self._since_clause(
                since, base_keyword="AND"
            )
            by_reject = self._conn.execute(
                f"""SELECT reject_reason,
                        COUNT(*) AS trades,
                        SUM(result='win')  AS wins,
                        SUM(result='loss') AS losses
                    FROM trades
                    WHERE kind='paper' AND reject_reason != ''
                    {by_reject_since}
                    GROUP BY reject_reason ORDER BY trades DESC""",
                by_reject_params,
            ).fetchall()

        return {
            "totals": self._with_winrate(dict(totals)),
            "by_asset": [self._with_winrate(dict(r)) for r in by_asset],
            "by_direction": [self._with_winrate(dict(r)) for r in by_direction],
            "by_day": [self._with_winrate(dict(r)) for r in by_day],
            "by_reject_reason": [self._with_winrate(dict(r)) for r in by_reject],
        }

    def analytics(self, kind: str = "all", since: str | None = None) -> dict:
        """Lecturas agregadas para la pestaña de estadísticas avanzadas.

        Solo resume datos existentes. Si alguna tabla satélite todavía no tiene
        filas, devuelve listas vacías/cobertura 0 sin afectar el monitoreo.
        """
        where, params = self._kind_where(kind, alias="t", since=since)
        with self._lock:
            coverage = self._conn.execute(
                f"""SELECT
                        COUNT(DISTINCT t.id) AS trades,
                        COUNT(DISTINCT s.trade_id) AS market_snapshots,
                        COUNT(DISTINCT f.trade_id) AS filter_trades,
                        COUNT(DISTINCT c.trade_id) AS candle_trades,
                        COUNT(DISTINCT a.trade_id) AS api_contexts,
                        COUNT(DISTINCT o.trade_id) AS outcomes
                    FROM trades t
                    LEFT JOIN trade_market_snapshots s
                        ON s.trade_id = t.trade_id AND s.timeframe_seconds = 60
                    LEFT JOIN trade_filter_evaluations f ON f.trade_id = t.trade_id
                    LEFT JOIN trade_candle_snapshots c ON c.trade_id = t.trade_id
                    LEFT JOIN trade_api_context a ON a.trade_id = t.trade_id
                    LEFT JOIN trade_outcomes o ON o.trade_id = t.trade_id
                    {where}""",
                params,
            ).fetchone()

            by_asset = self._conn.execute(
                f"""SELECT t.asset,
                        COUNT(*) AS trades,
                        SUM(t.result='win') AS wins,
                        SUM(t.result='loss') AS losses,
                        SUM(t.result='pending') AS pending,
                        COALESCE(SUM(t.profit),0) AS net_profit,
                        ROUND(AVG(s.bb_squeeze_ratio), 4) AS avg_squeeze_ratio,
                        ROUND(AVG(s.rsi), 2) AS avg_rsi,
                        ROUND(AVG(s.candle_range_ratio), 4) AS avg_candle_ratio,
                        ROUND(AVG(o.max_favorable_excursion), 6) AS avg_mfe,
                        ROUND(AVG(o.max_adverse_excursion), 6) AS avg_mae
                    FROM trades t
                    LEFT JOIN trade_market_snapshots s
                        ON s.trade_id = t.trade_id AND s.timeframe_seconds = 60
                    LEFT JOIN trade_outcomes o ON o.trade_id = t.trade_id
                    {where}
                    GROUP BY t.asset
                    ORDER BY trades DESC""",
                params,
            ).fetchall()

            by_day = self._conn.execute(
                f"""SELECT t.date,
                        COUNT(*) AS trades,
                        SUM(t.result='win') AS wins,
                        SUM(t.result='loss') AS losses,
                        SUM(t.result='pending') AS pending,
                        COALESCE(SUM(t.profit),0) AS net_profit
                    FROM trades t
                    {where}
                    GROUP BY t.date
                    ORDER BY t.date DESC
                    LIMIT 30""",
                params,
            ).fetchall()

            filter_performance = self._conn.execute(
                f"""SELECT f.filter_name,
                        f.filter_type,
                        COUNT(*) AS evaluations,
                        SUM(f.triggered) AS triggered,
                        SUM(f.blocked) AS blocked,
                        SUM(CASE WHEN f.triggered=1 AND t.result='win' THEN 1 ELSE 0 END) AS triggered_wins,
                        SUM(CASE WHEN f.triggered=1 AND t.result='loss' THEN 1 ELSE 0 END) AS triggered_losses,
                        ROUND(AVG(f.value), 6) AS avg_value,
                        ROUND(AVG(f.threshold), 6) AS avg_threshold
                    FROM trade_filter_evaluations f
                    JOIN trades t ON t.trade_id = f.trade_id
                    {where}
                    GROUP BY f.filter_name, f.filter_type
                    ORDER BY evaluations DESC, f.filter_name""",
                params,
            ).fetchall()

            trend = self._conn.execute(
                f"""SELECT COALESCE(s.trend_label, 'sin_dato') AS bucket,
                        COUNT(*) AS trades,
                        SUM(t.result='win') AS wins,
                        SUM(t.result='loss') AS losses,
                        COALESCE(SUM(t.profit),0) AS net_profit
                    FROM trades t
                    LEFT JOIN trade_market_snapshots s
                        ON s.trade_id = t.trade_id AND s.timeframe_seconds = 60
                    {where}
                    GROUP BY bucket
                    ORDER BY trades DESC""",
                params,
            ).fetchall()

            squeeze = self._bucket_rows(
                where, params,
                """CASE
                    WHEN s.bb_squeeze_ratio IS NULL THEN 'sin_dato'
                    WHEN s.bb_squeeze_ratio < 0.65 THEN '<0.65'
                    WHEN s.bb_squeeze_ratio < 0.85 THEN '0.65-0.85'
                    WHEN s.bb_squeeze_ratio < 1.10 THEN '0.85-1.10'
                    ELSE '>=1.10'
                END""",
            )
            rsi = self._bucket_rows(
                where, params,
                """CASE
                    WHEN s.rsi IS NULL THEN 'sin_dato'
                    WHEN s.rsi < 40 THEN '<40'
                    WHEN s.rsi < 45 THEN '40-45'
                    WHEN s.rsi < 50 THEN '45-50'
                    WHEN s.rsi < 55 THEN '50-55'
                    WHEN s.rsi < 60 THEN '55-60'
                    ELSE '>=60'
                END""",
            )
            bb_position = self._bucket_rows(
                where, params,
                """CASE
                    WHEN s.close_position_in_bb IS NULL THEN 'sin_dato'
                    WHEN s.close_position_in_bb < 0 THEN '<0%'
                    WHEN s.close_position_in_bb < 0.25 THEN '0-25%'
                    WHEN s.close_position_in_bb < 0.50 THEN '25-50%'
                    WHEN s.close_position_in_bb < 0.75 THEN '50-75%'
                    WHEN s.close_position_in_bb <= 1 THEN '75-100%'
                    ELSE '>100%'
                END""",
            )
            candle_ratio = self._bucket_rows(
                where, params,
                """CASE
                    WHEN s.candle_range_ratio IS NULL THEN 'sin_dato'
                    WHEN s.candle_range_ratio < 0.75 THEN '<0.75'
                    WHEN s.candle_range_ratio < 1.25 THEN '0.75-1.25'
                    WHEN s.candle_range_ratio < 2.00 THEN '1.25-2.00'
                    ELSE '>=2.00'
                END""",
            )

            outcomes = self._conn.execute(
                f"""SELECT COALESCE(o.result, t.result) AS result,
                        COUNT(o.trade_id) AS outcomes,
                        ROUND(AVG(o.directional_price_delta), 6) AS avg_directional_delta,
                        ROUND(AVG(o.win_margin), 6) AS avg_win_margin,
                        ROUND(AVG(o.max_favorable_excursion), 6) AS avg_mfe,
                        ROUND(AVG(o.max_adverse_excursion), 6) AS avg_mae,
                        ROUND(AVG(CASE WHEN o.entry_price > 0
                            AND o.max_favorable_excursion >= 0
                            AND o.max_favorable_excursion / o.entry_price < 0.5
                            THEN o.max_favorable_excursion / o.entry_price * 100 END), 4) AS avg_mfe_pct,
                        ROUND(AVG(CASE WHEN o.entry_price > 0
                            AND o.max_adverse_excursion >= 0
                            AND o.max_adverse_excursion / o.entry_price < 0.5
                            THEN o.max_adverse_excursion / o.entry_price * 100 END), 4) AS avg_mae_pct,
                        ROUND(AVG(o.seconds_to_first_profit), 2) AS avg_seconds_to_first_profit,
                        ROUND(AVG(o.realized_payout), 4) AS avg_realized_payout
                    FROM trades t
                    LEFT JOIN trade_outcomes o ON o.trade_id = t.trade_id
                    {where}
                    GROUP BY COALESCE(o.result, t.result)
                    ORDER BY outcomes DESC""",
                params,
            ).fetchall()

        by_asset_l = [self._with_winrate(dict(r)) for r in by_asset]
        filters_l = [self._filter_row(dict(r)) for r in filter_performance]
        market = {
            "trend": [self._with_winrate(dict(r)) for r in trend],
            "squeeze": [self._with_winrate(dict(r)) for r in squeeze],
            "rsi": [self._with_winrate(dict(r)) for r in rsi],
            "bb_position": [self._with_winrate(dict(r)) for r in bb_position],
            "candle_ratio": [self._with_winrate(dict(r)) for r in candle_ratio],
        }
        return {
            "coverage": self._coverage(dict(coverage)),
            "by_asset": by_asset_l,
            "by_day": [self._with_winrate(dict(r)) for r in by_day],
            "filter_performance": filters_l,
            "market": market,
            "outcomes": [dict(r) for r in outcomes],
            "insights": self._build_insights(by_asset_l, market, filters_l),
        }

    # Umbrales de fiabilidad (heurísticos: pocas muestras = poco fiable).
    SAMPLE_MIN = 30   # nº de operaciones decididas para considerar fiable el global
    ASSET_MIN = 5     # mínimo por activo para compararlos
    GROUP_MIN = 8     # mínimo por bucket de mercado

    @classmethod
    def _build_insights(cls, by_asset: list[dict], market: dict, filters: list[dict]) -> list[dict]:
        """Conclusiones legibles en español a partir de los agregados.

        Cada insight: {level: good|bad|warn|info, text}. Solo describe los datos
        existentes; nunca sugiere cambiar la estrategia (fase de recolección).
        """
        out: list[dict] = []
        decided = sum((a["wins"] + a["losses"]) for a in by_asset)
        wins = sum(a["wins"] for a in by_asset)

        if decided == 0:
            out.append({"level": "info", "text": "Aún no hay operaciones con resultado. "
                        "Las conclusiones aparecerán cuando se cierren operaciones."})
            return out

        wr = round(wins / decided * 100, 1)
        if decided < cls.SAMPLE_MIN:
            out.append({"level": "warn", "text": f"Muestra pequeña (n={decided} decididas). "
                        f"Los porcentajes son poco fiables todavía; tómalos como tendencia provisional. "
                        f"Para conclusiones más sólidas conviene ~{cls.SAMPLE_MIN}+ operaciones."})
        out.append({"level": "info", "text": f"Win-rate global: {wr}% sobre {decided} operaciones decididas."})

        # Mejor y peor activo (con muestra mínima).
        qual = [a for a in by_asset if (a["wins"] + a["losses"]) >= cls.ASSET_MIN]
        if len(qual) >= 2:
            best = max(qual, key=lambda a: a["win_rate"])
            worst = min(qual, key=lambda a: a["win_rate"])
            if best["asset"] != worst["asset"]:
                out.append({"level": "good", "text": f"Mejor activo: {best['asset']} con "
                            f"{best['win_rate']}% win-rate (n={best['wins'] + best['losses']})."})
                out.append({"level": "bad", "text": f"Peor activo: {worst['asset']} con "
                            f"{worst['win_rate']}% win-rate (n={worst['wins'] + worst['losses']})."})

        # Mejor condición de mercado entre todos los buckets.
        dim_labels = {"trend": "tendencia", "squeeze": "squeeze", "rsi": "RSI",
                      "bb_position": "posición en BB", "candle_ratio": "tamaño de vela"}
        best_bucket = None
        for dim, rows in market.items():
            for r in rows:
                n = r["wins"] + r["losses"]
                if r["bucket"] == "sin_dato" or n < cls.GROUP_MIN:
                    continue
                if best_bucket is None or r["win_rate"] > best_bucket["win_rate"]:
                    best_bucket = {"dim": dim, "bucket": r["bucket"], "win_rate": r["win_rate"], "n": n}
        if best_bucket:
            out.append({"level": "good", "text": f"Mejor contexto: {dim_labels.get(best_bucket['dim'], best_bucket['dim'])}"
                        f" = {best_bucket['bucket']} → {best_bucket['win_rate']}% win-rate (n={best_bucket['n']})."})

        # Filtros SOFT que podrían estar costando operaciones buenas.
        for f in filters:
            triggered_decided = f["triggered_wins"] + f["triggered_losses"]
            if (f["filter_type"] == "SOFT" and f["blocked"] > 0
                    and triggered_decided >= cls.ASSET_MIN and f["triggered_win_rate"] >= 55):
                out.append({"level": "warn", "text": f"El filtro '{f['filter_name']}' rechazó señales que "
                            f"habrían ganado el {f['triggered_win_rate']}% (n={triggered_decided}). "
                            f"Quizá te está costando operaciones buenas."})

        return out

    def _bucket_rows(self, where: str, params: list, bucket_sql: str):
        return self._conn.execute(
            f"""SELECT {bucket_sql} AS bucket,
                    COUNT(*) AS trades,
                    SUM(t.result='win') AS wins,
                    SUM(t.result='loss') AS losses,
                    COALESCE(SUM(t.profit),0) AS net_profit
                FROM trades t
                LEFT JOIN trade_market_snapshots s
                    ON s.trade_id = t.trade_id AND s.timeframe_seconds = 60
                {where}
                GROUP BY bucket
                ORDER BY CASE bucket
                    WHEN '<0.65' THEN 1 WHEN '0.65-0.85' THEN 2
                    WHEN '0.85-1.10' THEN 3 WHEN '>=1.10' THEN 4
                    WHEN '<40' THEN 1 WHEN '40-45' THEN 2 WHEN '45-50' THEN 3
                    WHEN '50-55' THEN 4 WHEN '55-60' THEN 5 WHEN '>=60' THEN 6
                    WHEN '<0%' THEN 1 WHEN '0-25%' THEN 2 WHEN '25-50%' THEN 3
                    WHEN '50-75%' THEN 4 WHEN '75-100%' THEN 5 WHEN '>100%' THEN 6
                    WHEN '<0.75' THEN 1 WHEN '0.75-1.25' THEN 2
                    WHEN '1.25-2.00' THEN 3 WHEN '>=2.00' THEN 4
                    ELSE 99 END""",
            params,
        ).fetchall()

    @staticmethod
    def _kind_where(kind: str, alias: str | None = None,
                    since: str | None = None):
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list = []
        if kind in ("real", "paper"):
            clauses.append(f"{prefix}kind = ?")
            params.append(kind)
        if since:
            clauses.append(
                f"COALESCE({prefix}entry_time, {prefix}signal_time, "
                f"{prefix}created_at) >= ?"
            )
            params.append(since)
        if not clauses:
            return "", []
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _since_clause(since: str | None, base_keyword: str = "WHERE",
                      alias: str | None = None):
        if not since:
            return "", []
        prefix = f"{alias}." if alias else ""
        return (
            f"{base_keyword} COALESCE({prefix}entry_time, "
            f"{prefix}signal_time, {prefix}created_at) >= ?",
            [since],
        )

    @staticmethod
    def _with_winrate(d: dict) -> dict:
        wins = d.get("wins") or 0
        losses = d.get("losses") or 0
        decided = wins + losses
        d["wins"] = wins
        d["losses"] = losses
        d["draws"] = d.get("draws") or 0
        d["pending"] = d.get("pending") or 0
        d["errors"] = d.get("errors") or 0
        d["net_profit"] = round(d.get("net_profit") or 0.0, 2)
        d["win_rate"] = round(wins / decided * 100, 2) if decided else 0.0
        return d

    @staticmethod
    def _coverage(d: dict) -> dict:
        trades = d.get("trades") or 0
        out = {k: (v or 0) for k, v in d.items()}
        for key in ("market_snapshots", "filter_trades", "candle_trades", "api_contexts", "outcomes"):
            out[f"{key}_pct"] = round((out[key] / trades * 100), 2) if trades else 0.0
        return out

    @staticmethod
    def _filter_row(d: dict) -> dict:
        wins = d.get("triggered_wins") or 0
        losses = d.get("triggered_losses") or 0
        triggered = d.get("triggered") or 0
        d["evaluations"] = d.get("evaluations") or 0
        d["triggered"] = triggered
        d["blocked"] = d.get("blocked") or 0
        d["triggered_wins"] = wins
        d["triggered_losses"] = losses
        d["trigger_rate"] = round(triggered / d["evaluations"] * 100, 2) if d["evaluations"] else 0.0
        d["triggered_win_rate"] = round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0
        return d

    def _write(self, sql: str, params: list | tuple, label: str) -> sqlite3.Cursor:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            with self._lock:
                try:
                    cur = self._conn.execute(sql, params)
                    self._conn.commit()
                    return cur
                except sqlite3.OperationalError as exc:
                    self._conn.rollback()
                    last_exc = exc
                    msg = str(exc).lower()
                    retryable = "locked" in msg or "busy" in msg or "disk i/o" in msg
                    if not retryable or attempt == 3:
                        logger.exception(f"SQLite falló en {label}.")
                        self._dump_failed_write(sql, params, label, exc)
                        raise
                except sqlite3.Error as exc:
                    self._conn.rollback()
                    logger.exception(f"SQLite falló en {label}.")
                    self._dump_failed_write(sql, params, label, exc)
                    raise
            time.sleep(0.25 * attempt)
        self._dump_failed_write(sql, params, label, last_exc)
        raise RuntimeError(f"SQLite no pudo completar {label}: {last_exc}")

    def _dump_failed_write(self, sql: str, params, label: str, exc) -> None:
        """Último recurso: si SQLite no pudo escribir, deja la operación en un
        JSONL junto a la BD para no perder el dato y poder re-aplicarlo a mano."""
        try:
            rescue = self.path.with_suffix(".rescue.jsonl")
            with open(rescue, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": self._now(), "label": label, "error": str(exc),
                     "sql": sql, "params": list(params)},
                    default=str,
                ) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo escribir el rescate JSONL del write fallido.")

    @staticmethod
    def _ensure_updated(rowcount: int, trade_id: str, action: str) -> int:
        if rowcount < 1:
            raise LookupError(
                f"No se encontró una fila SQLite para trade_id={trade_id} al guardar {action}."
            )
        if rowcount > 1:
            logger.warning(
                f"SQLite actualizó {rowcount} filas para trade_id={trade_id} al guardar {action}."
            )
        return rowcount

    # ------------------------------------------------------------------ #
    #  Tablas satélite (contexto de mercado, filtros, velas, API, outcomes)
    # ------------------------------------------------------------------ #
    def _insert_dict(self, table: str, data: dict, label: str) -> int:
        """Inserta un dict {columna: valor} en `table`. Devuelve el row_id."""
        cols = list(data.keys())
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        cur = self._write(sql, [self._coerce(data[c]) for c in cols], label)
        return int(cur.lastrowid)

    def write_market_snapshot(self, snapshot: dict) -> int:
        snapshot.setdefault("created_at", self._now())
        return self._insert_dict("trade_market_snapshots", snapshot,
                                 f"market_snapshot trade_id={snapshot.get('trade_id')}")

    def write_filter_evaluations(self, trade_id: str, rows: list[dict]) -> int:
        created = self._now()
        n = 0
        for r in rows:
            data = {"trade_id": trade_id, "created_at": created, **r}
            self._insert_dict("trade_filter_evaluations", data,
                              f"filter {r.get('filter_name')} trade_id={trade_id}")
            n += 1
        return n

    def write_candle_snapshots(self, trade_id: str, timeframe_seconds: int,
                               candles: list[dict]) -> int:
        """Guarda OHLCV alrededor de la señal. `candles` ya viene en orden
        cronológico; la última (índice 0) es la vela que disparó la señal."""
        created = self._now()
        total = len(candles)
        n = 0
        for i, c in enumerate(candles):
            data = {
                "trade_id": trade_id,
                "timeframe_seconds": timeframe_seconds,
                "candle_index": i - (total - 1),  # -N .. 0
                "candle_timestamp": c.get("candle_timestamp"),
                "open": c.get("open"), "high": c.get("high"),
                "low": c.get("low"), "close": c.get("close"),
                "volume": c.get("volume"),
                "created_at": created,
            }
            self._insert_dict("trade_candle_snapshots", data,
                              f"candle trade_id={trade_id}")
            n += 1
        return n

    def write_api_context(self, context: dict) -> int:
        context.setdefault("created_at", self._now())
        return self._insert_dict("trade_api_context", context,
                                 f"api_context trade_id={context.get('trade_id')}")

    def update_trade_market_type(self, trade_id: str, market_type: str) -> int:
        """Guarda en la propia tabla `trades` el tipo de instrumento usado
        (turbo/binary/binary_assumed/digital) para consultarlo sin JOIN."""
        cur = self._write(
            "UPDATE trades SET market_type = ? WHERE trade_id = ?",
            [market_type, trade_id],
            f"market_type trade_id={trade_id}",
        )
        return cur.rowcount

    def write_price_path(self, trade_id: str, points: list[dict]) -> int:
        """Recorrido del precio durante la operación (una fila por punto/vela)."""
        created = self._now()
        n = 0
        for p in points:
            data = {"trade_id": trade_id, "created_at": created, **p}
            self._insert_dict("trade_price_path", data, f"price_path trade_id={trade_id}")
            n += 1
        return n

    def write_outcome(self, outcome: dict) -> int:
        outcome.setdefault("created_at", self._now())
        return self._insert_dict("trade_outcomes", outcome,
                                 f"outcome trade_id={outcome.get('trade_id')}")

    def write_risk_management(self, data: dict) -> int:
        data.setdefault("created_at", self._now())
        return self._insert_dict("trade_risk_management", data,
                                 f"risk_mgmt trade_id={data.get('trade_id')}")

    # ------------------------------------------------------------------ #
    #  Contadores de gestión de riesgo (SOLO operaciones REALES cerradas)
    # ------------------------------------------------------------------ #
    def real_consecutive_losses(self) -> tuple[int, str | None]:
        """Pérdidas reales consecutivas más recientes y close_time de la última.

        Recorre las operaciones REALES ya cerradas (result win/loss) de más
        reciente a más antigua y cuenta las pérdidas seguidas hasta toparse con
        un win. Los 'draw'/'error'/'pending' NO entran (ni suman ni reinician):
        se ignoran porque la consulta filtra result IN ('win','loss').

        Devuelve (consecutivas, close_time_de_la_última_pérdida | None). El
        close_time sale de trade_outcomes; si faltara, cae a entry_time.
        Los paper-trades NO cuentan (kind='real')."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.result AS result,
                          COALESCE(o.close_time, t.entry_time) AS ct
                   FROM trades t
                   LEFT JOIN trade_outcomes o ON o.trade_id = t.trade_id
                   WHERE t.kind = 'real' AND t.result IN ('win','loss')
                   ORDER BY ct DESC
                   LIMIT 100"""
            ).fetchall()
        count = 0
        anchor: str | None = None
        for r in rows:
            if r["result"] == "loss":
                count += 1
                if anchor is None:
                    anchor = r["ct"]
            else:  # win -> corta la racha
                break
        return count, anchor

    def real_losses_in_window_today(self, start_hour: int, end_hour: int,
                                    today: str | None = None) -> int:
        """Pérdidas reales de HOY cuyo signal_time cae en [start_hour, end_hour).

        Se reinicia solo al cambiar de día (filtra por la fecha de hoy). Los
        paper-trades NO cuentan (kind='real')."""
        today = today or datetime.now().strftime("%Y-%m-%d")
        start = f"{start_hour:02d}:00:00"
        end = f"{end_hour:02d}:00:00"
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS n FROM trades
                   WHERE kind = 'real' AND result = 'loss'
                     AND substr(signal_time, 1, 10) = ?
                     AND substr(signal_time, 12, 8) >= ?
                     AND substr(signal_time, 12, 8) <  ?""",
                (today, start, end),
            ).fetchone()
        return int(row["n"] or 0)

    def _backfill_market_snapshots(self) -> int:
        """Migra las métricas ya recolectadas en `trades` a
        trade_market_snapshots (tf=60) sin perder nada. Idempotente: solo
        procesa trades que aún no tienen snapshot."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.* FROM trades t
                   WHERE t.trade_id IS NOT NULL AND t.trade_id != ''
                     AND NOT EXISTS (
                         SELECT 1 FROM trade_market_snapshots s
                         WHERE s.trade_id = t.trade_id)"""
            ).fetchall()
            pending = [dict(r) for r in rows]

        if not pending:
            return 0
        created = self._now()
        n = 0
        for row in pending:
            snapshot = build_backfill_snapshot(row, row["trade_id"], created)
            try:
                self._insert_dict("trade_market_snapshots", snapshot,
                                  f"backfill snapshot trade_id={row['trade_id']}")
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"No se pudo backfillear trade_id={row['trade_id']}: {exc}")
        logger.info(f"Backfill: {n} snapshots de mercado migrados desde trades.")
        return n

    # ------------------------------------------------------------------ #
    #  Introspección y exportación de la BD (pestaña DB del panel)
    # ------------------------------------------------------------------ #
    def allowed_tables(self) -> list[str]:
        """Nombres de tablas reales (excluye internas sqlite_*)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    def list_tables(self) -> list[dict]:
        """Resumen de cada tabla: nombre, nº de filas y columnas."""
        out = []
        for name in self.allowed_tables():
            with self._lock:
                count = self._conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
                cols = [r["name"] for r in self._conn.execute(f'PRAGMA table_info("{name}")')]
            out.append({"name": name, "rows": count, "columns": cols})
        return out

    def table_preview(self, name: str, limit: int = 100, offset: int = 0) -> dict:
        """Filas de una tabla (las más recientes primero). Valida el nombre."""
        if name not in self.allowed_tables():
            raise ValueError(f"Tabla desconocida: {name}")
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        with self._lock:
            total = self._conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
            cols = [r["name"] for r in self._conn.execute(f'PRAGMA table_info("{name}")')]
            rows = self._conn.execute(
                f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset)
            ).fetchall()
        return {"name": name, "columns": cols, "total": total,
                "limit": limit, "offset": offset, "rows": [dict(r) for r in rows]}

    def export_table(self, name: str) -> tuple[list[str], list[dict]]:
        """(columnas, todas las filas) de una tabla, para generar CSV. Valida nombre."""
        if name not in self.allowed_tables():
            raise ValueError(f"Tabla desconocida: {name}")
        with self._lock:
            cols = [r["name"] for r in self._conn.execute(f'PRAGMA table_info("{name}")')]
            rows = self._conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid ASC').fetchall()
        return cols, [dict(r) for r in rows]

    def snapshot_to(self, dest_path) -> None:
        """Copia consistente de la BD (incluye WAL) a un archivo, vía backup API."""
        dest = sqlite3.connect(str(dest_path))
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()

    # ------------------------------------------------------------------ #
    #  Settings (clave/valor)
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # --- Helpers de alto nivel para el panel ---
    MAX_ASSETS = 4

    def get_assets(self) -> list[str]:
        raw = self.get_setting("assets")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [a.strip().upper() for a in raw.split(",") if a.strip()]

    def set_assets(self, assets: list[str], max_assets: int | None = None) -> list[str]:
        cap = max_assets if max_assets and max_assets > 0 else self.MAX_ASSETS
        clean, seen = [], set()
        for a in assets:
            a = a.strip().upper()
            if a and a not in seen:
                seen.add(a)
                clean.append(a)
        clean = clean[:cap]
        self.set_setting("assets", json.dumps(clean))
        return clean

    def init_assets_if_empty(self, defaults: list[str]) -> None:
        if self.get_setting("assets") is None:
            self.set_assets(defaults)

    def get_bot_enabled(self) -> bool:
        return self.get_setting("bot_enabled", "0") == "1"

    def set_bot_enabled(self, enabled: bool) -> None:
        self.set_setting("bot_enabled", "1" if enabled else "0")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
