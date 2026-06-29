"""Panel de administración (FastAPI) en localhost.

Expone:
  - estadísticas (real vs paper): ganado/perdido/win-rate/profit, por activo,
    por dirección, por día y por motivo de rechazo.
  - gestión de activos (añadir/quitar, máximo 4).
  - encender / apagar el bot y ver su estado.
"""
from __future__ import annotations

import csv
import io
import os
import signal
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from loguru import logger
from pydantic import BaseModel
from starlette.background import BackgroundTask

from application.controller import BotController
from config.settings import DATA_DIR, settings
from infrastructure.database import Database
from infrastructure.system_monitor import SystemMonitor

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _csv_response(filename: str, sections: list[tuple[str, list[str], list[dict]]]) -> Response:
    """Genera un CSV (posiblemente multi-sección) y lo devuelve como descarga.

    Cada sección es (título, columnas, filas). Si hay varias, se separan por una
    línea de título y una en blanco, para informes legibles en Excel.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    multi = len(sections) > 1
    for title, cols, rows in sections:
        if multi:
            writer.writerow([f"# {title}"])
        writer.writerow(cols)
        for r in rows:
            writer.writerow([r.get(c, "") for c in cols])
        if multi:
            writer.writerow([])
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _since_filter(value: str | None) -> str | None:
    if not value or value.strip().lower() == "all":
        return None
    raw = value.strip().replace("T", " ")
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise HTTPException(
        status_code=400,
        detail="from debe tener formato YYYY-MM-DDTHH:MM o usar all.",
    )

# --- Singletons de la app ---
db = Database(DATA_DIR / "bot.db")
controller = BotController(settings, db)
system_monitor = SystemMonitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    controller.start_background()
    yield
    controller.shutdown()
    db.close()


app = FastAPI(title="IQ Binary Bot — Panel", lifespan=lifespan)


# ---------------------------------------------------------------------- #
#  Modelos de request
# ---------------------------------------------------------------------- #
class AssetsBody(BaseModel):
    assets: list[str]


class AssetBody(BaseModel):
    asset: str


# ---------------------------------------------------------------------- #
#  UI
# ---------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


# ---------------------------------------------------------------------- #
#  Estado y control del bot
# ---------------------------------------------------------------------- #
@app.get("/api/status")
def get_status():
    return controller.status()


@app.get("/api/system")
def get_system_status():
    return system_monitor.snapshot()


@app.post("/api/bot/start")
def bot_start():
    # No bloquea: si falta conectar, el hilo de fondo conecta y el panel
    # muestra "conectando…" hasta que termine.
    controller.start()
    return controller.status()


@app.post("/api/bot/stop")
def bot_stop():
    controller.stop()
    return controller.status()


@app.post("/api/connect")
def connect():
    # No bloquea: dispara la conexión en segundo plano.
    controller.request_connect()
    return controller.status()


@app.post("/api/service/recover")
def service_recover():
    controller.request_service_recover()
    return controller.status()


@app.post("/api/shutdown")
def shutdown():
    """Salida segura: apaga el bot y cierra el proceso de forma ordenada.

    Envía SIGINT al propio proceso (equivale a Ctrl+C), lo que dispara el cierre
    elegante de uvicorn -> lifespan shutdown -> controller.shutdown() + db.close().
    """
    logger.info("Salida segura solicitada desde el panel.")
    controller.stop()  # deja el bot apagado y persistido

    def _terminate():
        time.sleep(0.6)  # da tiempo a responder al navegador
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=_terminate, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------------- #
#  Activos
# ---------------------------------------------------------------------- #
@app.get("/api/assets")
def get_assets():
    return {"assets": db.get_assets(), "max_assets": controller.max_assets}


@app.get("/api/available-assets")
def available_assets():
    """Activos para binarias (turbo/binary) listados desde la API de IQ Option."""
    return {"assets": controller.available_assets(), "connected": controller.connected}


@app.put("/api/assets")
def set_assets(body: AssetsBody):
    cap = controller.max_assets
    if len(body.assets) > cap:
        raise HTTPException(status_code=400, detail=f"Máximo {cap} activos.")
    saved = db.set_assets(body.assets, max_assets=cap)
    return {"assets": saved, "max_assets": cap}


@app.post("/api/assets/add")
def add_asset(body: AssetBody):
    current = db.get_assets()
    asset = body.asset.strip().upper()
    if not asset:
        raise HTTPException(status_code=400, detail="Activo vacío.")
    if asset in current:
        raise HTTPException(status_code=400, detail=f"{asset} ya está en la lista.")
    cap = controller.max_assets
    if len(current) >= cap:
        raise HTTPException(status_code=400, detail=f"Máximo {cap} activos.")

    # Validación de existencia con el catálogo ACTIVES_OPCODE (atrapa typos).
    # Usa la caché del controlador (sin llamadas de red en el request).
    if controller.connected and not controller.asset_exists(asset):
        raise HTTPException(
            status_code=400,
            detail=f"'{asset}' no existe en IQ Option. ¿Typo? Ej: EURUSD o EURUSD-OTC.",
        )

    saved = db.set_assets(current + [asset], max_assets=cap)
    return {"assets": saved, "max_assets": cap}


@app.delete("/api/assets/{asset}")
def remove_asset(asset: str):
    asset = asset.strip().upper()
    current = db.get_assets()
    if asset not in current:
        raise HTTPException(status_code=404, detail=f"{asset} no está en la lista.")
    saved = db.set_assets([a for a in current if a != asset], max_assets=controller.max_assets)
    return {"assets": saved, "max_assets": controller.max_assets}


# ---------------------------------------------------------------------- #
#  Estadísticas y trades
# ---------------------------------------------------------------------- #
@app.get("/api/config")
def get_config():
    from config import runtime_config
    return {"config": controller.get_config(), "schema": runtime_config.schema()}


@app.put("/api/config")
def update_config(body: dict):
    try:
        cfg = controller.update_config(body or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"config": cfg}


@app.get("/api/stats")
def get_stats(kind: str = "all", from_: str | None = Query(default=None, alias="from")):
    if kind not in ("all", "real", "paper"):
        raise HTTPException(status_code=400, detail="kind debe ser all|real|paper")
    return db.stats(kind, since=_since_filter(from_))


@app.get("/api/analytics")
def get_analytics(kind: str = "all", from_: str | None = Query(default=None, alias="from")):
    if kind not in ("all", "real", "paper"):
        raise HTTPException(status_code=400, detail="kind debe ser all|real|paper")
    return db.analytics(kind, since=_since_filter(from_))


@app.get("/api/logs")
def get_logs(after: int = 0):
    from infrastructure import log_buffer
    logs = log_buffer.get_logs(after)
    last_id = logs[-1]["id"] if logs else after
    return {"logs": logs, "last_id": last_id}


@app.get("/api/trades")
def get_trades(kind: str = "all", limit: int = 50,
               from_: str | None = Query(default=None, alias="from")):
    if kind not in ("all", "real", "paper"):
        raise HTTPException(status_code=400, detail="kind debe ser all|real|paper")
    limit = max(1, min(limit, 500))
    return {"trades": db.recent_trades(kind, limit, since=_since_filter(from_))}


# ---------------------------------------------------------------------- #
#  Descarga de informes de estadísticas (CSV)
# ---------------------------------------------------------------------- #
def _check_kind(kind: str) -> None:
    if kind not in ("all", "real", "paper"):
        raise HTTPException(status_code=400, detail="kind debe ser all|real|paper")


@app.get("/api/stats/export")
def export_stats(kind: str = "all",
                 from_: str | None = Query(default=None, alias="from")):
    _check_kind(kind)
    since = _since_filter(from_)
    s = db.stats(kind, since=since)
    sections = [
        ("Totales", ["trades", "wins", "losses", "draws", "pending", "errors", "win_rate", "net_profit"], [s["totals"]]),
        ("Por activo", ["asset", "trades", "wins", "losses", "win_rate", "net_profit"], s["by_asset"]),
        ("Por dirección", ["direction", "trades", "wins", "losses", "win_rate", "net_profit"], s["by_direction"]),
        ("Por día", ["date", "trades", "wins", "losses", "win_rate", "net_profit"], s["by_day"]),
        ("Por motivo de rechazo (paper)", ["reject_reason", "trades", "wins", "losses", "win_rate"], s["by_reject_reason"]),
    ]
    return _csv_response(f"estadisticas_{kind}_{_stamp()}.csv", sections)


@app.get("/api/analytics/export")
def export_analytics(kind: str = "all",
                     from_: str | None = Query(default=None, alias="from")):
    _check_kind(kind)
    since = _since_filter(from_)
    a = db.analytics(kind, since=since)
    m = a.get("market", {})
    sections = [
        ("Cobertura de datos", list(a["coverage"].keys()), [a["coverage"]]),
        ("Por activo", ["asset", "trades", "wins", "losses", "win_rate", "net_profit",
                        "avg_squeeze_ratio", "avg_rsi", "avg_candle_ratio", "avg_mfe", "avg_mae"], a["by_asset"]),
        ("Filtros evaluados", ["filter_name", "filter_type", "evaluations", "triggered", "blocked",
                               "trigger_rate", "triggered_win_rate", "avg_value", "avg_threshold"], a["filter_performance"]),
        ("Tendencia", ["bucket", "trades", "wins", "losses", "win_rate", "net_profit"], m.get("trend", [])),
        ("Squeeze", ["bucket", "trades", "wins", "losses", "win_rate", "net_profit"], m.get("squeeze", [])),
        ("RSI", ["bucket", "trades", "wins", "losses", "win_rate", "net_profit"], m.get("rsi", [])),
        ("Posición en BB", ["bucket", "trades", "wins", "losses", "win_rate", "net_profit"], m.get("bb_position", [])),
        ("Tamaño de vela", ["bucket", "trades", "wins", "losses", "win_rate", "net_profit"], m.get("candle_ratio", [])),
        ("Outcomes", ["result", "outcomes", "avg_directional_delta", "avg_win_margin",
                      "avg_mfe", "avg_mae", "avg_mfe_pct", "avg_mae_pct",
                      "avg_realized_payout", "avg_seconds_to_first_profit"], a["outcomes"]),
    ]
    return _csv_response(f"analytics_{kind}_{_stamp()}.csv", sections)


# ---------------------------------------------------------------------- #
#  Exploración y descarga de la base de datos (pestaña DB)
# ---------------------------------------------------------------------- #
@app.get("/api/db/tables")
def db_tables():
    return {"tables": db.list_tables()}


@app.get("/api/db/table")
def db_table(name: str, limit: int = 100, offset: int = 0):
    try:
        return db.table_preview(name, limit, offset)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/db/table/export")
def db_table_export(name: str):
    try:
        cols, rows = db.export_table(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _csv_response(f"{name}_{_stamp()}.csv", [(name, cols, rows)])


@app.get("/api/db/download")
def db_download():
    """Descarga una copia consistente de toda la BD (.sqlite)."""
    fd, tmp = tempfile.mkstemp(prefix="bot_snapshot_", suffix=".sqlite", dir=str(DATA_DIR))
    os.close(fd)
    try:
        db.snapshot_to(tmp)
    except Exception as exc:  # noqa: BLE001
        os.unlink(tmp)
        raise HTTPException(status_code=500, detail=f"No se pudo exportar la BD: {exc}")
    return FileResponse(
        tmp, media_type="application/x-sqlite3",
        filename=f"bot_{_stamp()}.sqlite",
        background=BackgroundTask(lambda: os.path.exists(tmp) and os.unlink(tmp)),
    )
