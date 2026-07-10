"""Script one-off (idempotente) para reparar/anular exit_price corrompidos.

CONTEXTO (2026-07-10): un bug de condición de carrera en
`infrastructure/iqoption_client.py` (`_request_candles_raw`) corrompía
`trade_outcomes.exit_price` (y `trade_price_path`) en ~16-20% de los trades
reales. La librería `iqoptionapi` expone `raw.candles.candles_data` como UN
ÚNICO atributo compartido por TODOS los activos e hilos (sin id de
correlación por request). Cuando dos fetches de velas coincidían (p.ej. el
escaneo de un activo en el hilo principal mientras un hilo en background
resolvía el cierre de un trade de OTRO activo), un hilo podía leer la
respuesta que llegó para el activo equivocado: `reconstruct_outcome()`
terminaba calculando exit_price con la escala de precio de OTRO activo
(ej. USDCHF con entry_price=0.82 y exit_price=4327, escala de XAUUSD).

El resultado (win/loss/draw) y el profit vienen de la API de IQ Option por
otra vía (get_optioninfo) y NO están afectados por este bug: este script
JAMÁS toca `result` ni `profit`.

El fix de raíz (serializar el acceso a `raw.candles.candles_data` con un
lock) ya está aplicado en `infrastructure/iqoption_client.py` y evita nuevas
corrupciones. Este script es la reparación de los datos YA escritos.

USO:
    python3 tools/repair_exit_prices.py /ruta/a/bot.sqlite [--dry-run]

HEURÍSTICA DE DETECCIÓN:
    exit_price/entry_price fuera de [0.5, 2.0], con entry_price > 0.
    (Un exit_price corrupto viene de OTRO activo, así que su escala de precio
    queda muy lejos de la del propio entry_price salvo coincidencia extrema.)

REPARACIÓN (por outcome corrupto detectado), en este orden:
    1. `trade_price_path` del PROPIO trade: se toma el último punto (más
       cercano al vencimiento). Si su precio SÍ cae dentro de rango razonable
       respecto a entry_price (o sea, NO viene contaminado por el mismo
       bug: recuérdese que trade_price_path se construye en el mismo
       `_log_outcome()` con las MISMAS velas que exit_price, así que si
       ambos vienen del mismo fetch corrupto, este punto también estará
       fuera de rango y no servirá) -> se usa como exit_price recuperado.
    2. `trade_candle_snapshots` (tf=60) del PROPIO trade: SOLO se acepta una
       vela cuyo `candle_timestamp` caiga dentro de +-90s del `close_time`
       del outcome (proximidad temporal real al vencimiento). En este
       esquema esa tabla guarda las velas HASTA la señal de ENTRADA (nunca
       llega al vencimiento), así que en la práctica casi nunca hay una vela
       que pase ese filtro; se deja como red de seguridad honesta. Se
       descartó deliberadamente "tomar la última vela disponible sin más":
       esa vela es siempre la de ENTRADA (candle_index=0), así que su close
       es trivialmente ~= entry_price y "repararía" el outcome con un
       exit_price ficticio sin relación con el cierre real de la operación.
    3. Si ninguna fuente sirve: `exit_price = NULL` y
       `outcome_price_suspect = 1` (columna nueva, migración idempotente
       en `infrastructure/schema.py` / `infrastructure/database.py`), y se
       anulan también las columnas derivadas contaminadas (ver más abajo).

`result`, `profit` y `realized_payout` NO se tocan nunca: vienen de
`get_optioninfo` (otra vía de la API), no están afectados por este bug.

Cuando el outcome queda ANULADO (paso 3, exit_price -> NULL,
outcome_price_suspect = 1), este script también anula las columnas
derivadas que se calculan a partir del `exit_price` corrupto en el mismo
`_log_outcome()` (misma fuente contaminada, mismo bug):
`raw_price_delta`, `directional_price_delta`, `win_margin`,
`max_price_during_trade`, `min_price_during_trade`,
`max_favorable_excursion`, `max_adverse_excursion`. Cuando el outcome se
REPARA (paso 1 o 2, con un exit_price recuperado y confiable), esas
columnas derivadas NO se tocan: se dejan para que quien las necesite las
recalcule a partir del exit_price ya reparado (este script solo repara el
precio, no re-deriva métricas).

IDEMPOTENCIA: una fila con `outcome_price_suspect = 1` o con exit_price ya
dentro de rango se cuenta como 'intacta' y no se vuelve a tocar (tampoco
se vuelven a anular sus derivados: si ya están NULL, quedan igual). Correr
el script dos veces seguidas produce el mismo resultado la segunda vez
(0 reparados, 0 anulados adicionales).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime

# Fuera de [MIN_RATIO, MAX_RATIO] respecto a entry_price se considera corrupto
# (escala de otro activo). Dentro de rango se considera un movimiento de
# precio normal para una opción binaria/turbo de corta duración.
MIN_RATIO = 0.5
MAX_RATIO = 2.0

# Tolerancia para aceptar una vela de trade_candle_snapshots como "cercana"
# al cierre real de la operación (ver _recover_from_candles).
_CANDLE_PROXIMITY_SECONDS = 90

# Columnas derivadas de exit_price (misma fuente contaminada que el bug de
# condición de carrera, ver docstring del módulo). Se anulan junto con
# exit_price cuando un outcome no se puede reparar (paso 3). result/profit/
# realized_payout NUNCA se incluyen aquí: vienen de otra vía de la API.
_CONTAMINATED_DERIVED_COLUMNS = (
    "raw_price_delta",
    "directional_price_delta",
    "win_margin",
    "max_price_during_trade",
    "min_price_during_trade",
    "max_favorable_excursion",
    "max_adverse_excursion",
)

_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, _FMT)
    except (TypeError, ValueError):
        return None


def _is_corrupt(exit_price, entry_price) -> bool:
    if entry_price is None or entry_price <= 0 or exit_price is None:
        return False
    ratio = exit_price / entry_price
    return ratio < MIN_RATIO or ratio > MAX_RATIO


def _in_range(price, entry_price) -> bool:
    if price is None or entry_price is None or entry_price <= 0 or price <= 0:
        return False
    ratio = price / entry_price
    return MIN_RATIO <= ratio <= MAX_RATIO


def _ensure_column(cur: sqlite3.Cursor) -> None:
    """Migración idempotente: añade outcome_price_suspect si falta."""
    cols = {r[1] for r in cur.execute("PRAGMA table_info(trade_outcomes)")}
    if "outcome_price_suspect" not in cols:
        cur.execute("ALTER TABLE trade_outcomes ADD COLUMN outcome_price_suspect INTEGER")


def _recover_from_price_path(cur: sqlite3.Cursor, trade_id: str, entry_price: float):
    """Último punto de trade_price_path del propio trade, si NO está
    también contaminado (misma fuente que exit_price: ver docstring)."""
    row = cur.execute(
        "SELECT price, candle_close FROM trade_price_path "
        "WHERE trade_id = ? ORDER BY seconds_from_entry DESC LIMIT 1",
        (trade_id,),
    ).fetchone()
    if row is None:
        return None
    for candidate in (row["price"], row["candle_close"]):
        if _in_range(candidate, entry_price):
            return candidate
    return None


def _recover_from_candles(cur: sqlite3.Cursor, trade_id: str, entry_price: float,
                          close_time: str | None):
    """Vela M1 (tf=60) de trade_candle_snapshots del propio trade, SOLO si su
    candle_timestamp está a <= _CANDLE_PROXIMITY_SECONDS del close_time real.

    Importante: NO se toma "la última vela disponible sin más". Esa tabla
    guarda las ~60 velas HASTA la señal de entrada (candle_index 0 = vela de
    entrada); su close es casi idéntico a entry_price por construcción, así
    que usarla sin el filtro de cercanía temporal "repararía" el outcome con
    un exit_price ficticio (≈ entry_price) sin relación con el cierre real.
    Con el filtro, en la práctica esta función casi nunca encuentra nada
    (queda como red de seguridad honesta para el caso raro en que sí cubra
    el vencimiento)."""
    close_dt = _parse_dt(close_time)
    if close_dt is None:
        return None
    rows = cur.execute(
        "SELECT close, candle_timestamp FROM trade_candle_snapshots "
        "WHERE trade_id = ? AND timeframe_seconds = 60 AND candle_timestamp IS NOT NULL",
        (trade_id,),
    ).fetchall()
    best = None
    for r in rows:
        ts = _parse_dt(r["candle_timestamp"])
        if ts is None or not _in_range(r["close"], entry_price):
            continue
        delta = abs((ts - close_dt).total_seconds())
        if delta <= _CANDLE_PROXIMITY_SECONDS and (best is None or delta < best[0]):
            best = (delta, r["close"])
    return best[1] if best else None


def repair(db_path: str, dry_run: bool = False) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    _ensure_column(cur)
    if not dry_run:
        con.commit()

    rows = cur.execute(
        "SELECT id, trade_id, entry_price, exit_price, outcome_price_suspect, close_time "
        "FROM trade_outcomes"
    ).fetchall()

    counts = {"reparados": 0, "anulados": 0, "intactos": 0}

    for row in rows:
        if row["outcome_price_suspect"] == 1:
            counts["intactos"] += 1
            continue
        if not _is_corrupt(row["exit_price"], row["entry_price"]):
            counts["intactos"] += 1
            continue

        entry_price = row["entry_price"]
        trade_id = row["trade_id"]
        recovered = _recover_from_price_path(cur, trade_id, entry_price)
        if recovered is None:
            recovered = _recover_from_candles(cur, trade_id, entry_price, row["close_time"])

        if recovered is not None:
            if not dry_run:
                cur.execute(
                    "UPDATE trade_outcomes SET exit_price = ?, outcome_price_suspect = 0 "
                    "WHERE id = ?",
                    (recovered, row["id"]),
                )
            counts["reparados"] += 1
        else:
            if not dry_run:
                set_clause = ", ".join(f"{col} = NULL" for col in _CONTAMINATED_DERIVED_COLUMNS)
                cur.execute(
                    "UPDATE trade_outcomes SET exit_price = NULL, outcome_price_suspect = 1, "
                    f"{set_clause} WHERE id = ?",
                    (row["id"],),
                )
            counts["anulados"] += 1

    if not dry_run:
        con.commit()
    con.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repara/anula exit_price corrompidos en trade_outcomes (bug de "
                     "condición de carrera en get_candles, ver docstring del módulo).",
    )
    parser.add_argument("db_path", help="Ruta a la BD SQLite (bot.sqlite).")
    parser.add_argument("--dry-run", action="store_true",
                        help="No escribe cambios; solo reporta los conteos.")
    args = parser.parse_args()

    counts = repair(args.db_path, dry_run=args.dry_run)
    total = sum(counts.values())
    print(f"BD: {args.db_path}")
    print(f"Total outcomes evaluados: {total}")
    print(f"  reparados: {counts['reparados']}")
    print(f"  anulados:  {counts['anulados']}")
    print(f"  intactos:  {counts['intactos']}")
    if args.dry_run:
        print("(--dry-run: no se escribió ningún cambio en la BD)")


if __name__ == "__main__":
    sys.exit(main())
