"""Controlador del bot: el "cerebro" que el panel web maneja.

Mantiene un hilo en segundo plano que, cuando el bot está ENCENDIDO y
CONECTADO, escanea los activos (leídos de la base de datos) en cada vela.
El panel puede encender/apagar, conectar y cambiar la lista de activos en
caliente.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from loguru import logger

from application.bot_runner import BotRunner
from application.signal_service import SignalService
from application.trade_service import TradeService
from config import runtime_config
from config.epoch import compute_config_epoch
from config.settings import Settings
from domain.asset_selection import RankedAsset, diff_selection, select_real_assets
from domain.risk import RiskEngine
from domain.risk_management import RiskManager
from domain.strategy import FibPullbackStrategy
from infrastructure.candle_repository import CandleRepository
from infrastructure.database import Database
from infrastructure.iqoption_client import IQOptionClient
from infrastructure.trade_executor import TradeExecutor


def _call_with_timeout(fn, timeout: float, default):
    """Ejecuta fn() con un límite de tiempo. Devuelve (resultado, timed_out).

    iqoptionapi puede colgarse en algunas llamadas (get_all_open_time, etc.).
    Esto evita que un cuelgue congele el hilo de fondo para siempre.
    """
    box: dict = {}

    def work():
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return default, True
    if "e" in box:
        raise box["e"]
    return box.get("v", default), False


def _gather(jobs: list[tuple]) -> dict:
    """Ejecuta varias llamadas de API EN PARALELO con límite de tiempo individual.

    jobs: [(key, fn, timeout), ...]. Cada fn corre en su propio hilo daemon (igual
    que _call_with_timeout: tolera que iqoptionapi se cuelgue). Al lanzarlas a la vez,
    el peor caso es max(timeouts) en vez de la suma.

    Devuelve {key: (value, timed_out, latency_ms)}; value es None si se colgó o lanzó.
    """
    boxes: dict = {}
    starts: dict = {}
    threads: dict = {}
    for key, fn, _timeout in jobs:
        box: dict = {}
        boxes[key] = box

        def work(fn=fn, box=box):
            try:
                box["v"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["e"] = exc

        th = threading.Thread(target=work, daemon=True)
        threads[key] = th
        starts[key] = time.perf_counter()
        th.start()

    out: dict = {}
    for key, _fn, timeout in jobs:
        remaining = timeout - (time.perf_counter() - starts[key])
        threads[key].join(max(0.0, remaining))
        latency_ms = (time.perf_counter() - starts[key]) * 1000
        box = boxes[key]
        if threads[key].is_alive():
            out[key] = (None, True, latency_ms)
        elif "e" in box:
            logger.warning(f"Llamada de mercado '{key}' falló: {box['e']}")
            out[key] = (None, False, latency_ms)
        else:
            out[key] = (box.get("v"), False, latency_ms)
    return out


def _build_strategy(cfg: dict) -> FibPullbackStrategy:
    return FibPullbackStrategy(
        ema_fast=cfg["ema_fast"], ema_slow=cfg["ema_slow"], rsi_period=cfg["rsi_period"],
        bb_period=cfg["bb_period"], bb_mult=cfg["bb_mult"], fib_lookback=cfg["fib_lookback"],
    )


def build_runner(
    settings: Settings,
    db: Database,
    cfg: dict,
    executor: TradeExecutor | None = None,
) -> tuple[BotRunner, IQOptionClient, TradeExecutor]:
    client = IQOptionClient(
        email=settings.iq_email,
        password=settings.iq_password,
        allow_real=settings.allow_real,
    )
    repo = CandleRepository(client, settings.timeframe_seconds)
    strategy = _build_strategy(cfg)
    signal_service = SignalService(
        repo, strategy, settings.candle_count,
        collect_multi_tf=settings.collect_multi_tf,
    )
    risk = RiskEngine(
        min_payout=cfg["min_payout"],
        squeeze_factor=cfg["squeeze_factor"],
        giant_candle_factor=cfg["giant_candle_factor"],
        lateral_factor=cfg["lateral_factor"],
        max_entry_delay_seconds=cfg["max_entry_delay_seconds"],
        reject_low_payout=cfg["reject_low_payout"],
        reject_bb_squeeze=cfg["reject_bb_squeeze"],
        reject_giant_candle=cfg["reject_giant_candle"],
        reject_lateral_market=cfg["reject_lateral_market"],
        reject_late_entry=cfg["reject_late_entry"],
    )
    # config_epoch vigente para esta config (ver config/epoch.py). Se recalcula
    # aquí (fuente única de verdad: se deriva de `cfg`) y también en caliente
    # en BotController._apply_config() cada vez que la config cambia desde el
    # panel, sin pasar por build_runner de nuevo.
    config_epoch = compute_config_epoch(cfg)
    if executor is None:
        executor = TradeExecutor(
            client=client,
            db=db,
            amount=cfg["base_amount"],
            expiration_minutes=cfg["expiration_minutes"],
            allow_digital=cfg["allow_digital"],
            config_epoch=config_epoch,
        )
    else:
        # Conserva open_assets y paper-trades pendientes al reconstruir la conexión.
        executor.client = client
        executor.amount = cfg["base_amount"]
        executor.expiration_minutes = cfg["expiration_minutes"]
        executor.allow_digital = cfg["allow_digital"]
        executor.config_epoch = config_epoch
    risk_manager = RiskManager(
        enabled=cfg["risk_mgmt_enabled"],
        cooldown_losses=cfg["risk_cooldown_losses"],
        cooldown_minutes=cfg["risk_cooldown_minutes"],
        window_start_hour=cfg["risk_window_start_hour"],
        window_end_hour=cfg["risk_window_end_hour"],
        window_max_losses=cfg["risk_window_max_losses"],
    )
    trade_service = TradeService(risk, executor, risk_manager)
    runner = BotRunner(settings, client, signal_service, trade_service, executor)
    return runner, client, executor


class BotController:
    MAX_ASSETS = Database.MAX_ASSETS  # tope por defecto si no hay config

    @property
    def max_assets(self) -> int:
        """Límite de activos vigente (editable desde Configuración, 1-8)."""
        try:
            v = int(self.config.get("max_assets") or self.MAX_ASSETS)
        except (TypeError, ValueError):
            v = self.MAX_ASSETS
        return max(1, min(v, 8))

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

        # Config editable: defaults del .env, sobrescritos por lo guardado en BD.
        self.config = runtime_config.defaults_from_settings(settings)
        self._load_config_from_db()
        # config_epoch vigente (ver config/epoch.py). build_runner() calcula el
        # suyo propio a partir de self.config (misma función pura, mismo
        # resultado); se guarda también aquí para exponerlo en status() sin
        # tener que ir a buscar el executor.
        self.config_epoch = compute_config_epoch(self.config)

        self.runner, self.client, self.executor = build_runner(settings, db, self.config)

        self.connected = False
        self.connecting = False
        self.recovering = False
        self.running = False
        self.last_error: str | None = None
        self._account_mode: str | None = None

        # Banderas de intención (las pone el panel, las ejecuta el hilo de fondo).
        self._connect_requested = False
        self._recover_requested = False
        self._want_running = False

        self._shutdown = False
        self._thread: threading.Thread | None = None
        self._conn_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._restored = False

        # Caché de mercado (estado de apertura + payouts + balance). El panel NUNCA
        # llama a la API de IQ directamente: solo lee de aquí, así /api/status es
        # instantáneo y no se cuelga aunque la conexión a IQ falle.
        self._open_cache: dict = {}
        self._profit_cache: dict = {}
        self._opcode_cache: dict = {}   # {nombre: id} catálogo de existencia
        self._balance: float = 0.0
        self._market_cache_ts: float = 0.0
        # Frescura por caché (cuándo se leyó con éxito) + latencia de la última
        # lectura, para auditar en trade_api_context cuán viejo era el payout/apertura
        # usado en cada entrada. Solo instrumentación: no cambia ninguna decisión.
        self._open_cache_ts: float = 0.0
        self._profit_cache_ts: float = 0.0
        self._open_latency_ms: float | None = None
        self._profit_latency_ms: float | None = None
        self._last_market_cache_save_ts: float = 0.0

        # Selección automática de activos de mercado REAL (no-OTC). Cuando
        # config["auto_asset_selection"] está activo, esta selección
        # reemplaza la whitelist manual (self.db.set_assets(...) se sobre-
        # escribe en cada rotación; ver _refresh_asset_selection()).
        self._auto_selection: list[str] = []
        self._auto_selection_ts: float = 0.0
        self._otc_fallback_active: bool = False
        self._asset_wait_logged_ts: float = 0.0   # throttle: "esperando snapshot"
        self._asset_idle_logged_ts: float = 0.0   # throttle: "sin mercado real abierto"

        # Inicializa la lista de activos desde el .env si la BD está vacía.
        self.db.init_assets_if_empty(self.settings.assets[: self.max_assets])
        self._load_market_cache_from_db()
        self._load_auto_selection_from_db()

    # ------------------------------------------------------------------ #
    #  Configuración editable
    # ------------------------------------------------------------------ #
    def _load_config_from_db(self) -> None:
        saved = self.db.get_setting("config")
        if not saved:
            return
        try:
            data = json.loads(saved)
        except json.JSONDecodeError:
            return
        for k, v in data.items():
            if k in self.config:
                try:
                    self.config[k] = runtime_config.coerce(k, v)
                except ValueError:
                    pass  # ignora valores inválidos guardados

    def _load_market_cache_from_db(self) -> None:
        saved = self.db.get_setting("market_cache")
        if not saved:
            return
        try:
            data = json.loads(saved)
        except json.JSONDecodeError:
            return

        open_cache = data.get("open_cache") or {}
        profit_cache = data.get("profit_cache") or {}
        if not isinstance(open_cache, dict) or not isinstance(profit_cache, dict):
            return

        self._open_cache = open_cache
        self._profit_cache = profit_cache
        saved_at = float(data.get("saved_at") or 0.0)
        self._open_cache_ts = float(data.get("open_cache_ts") or saved_at)
        self._profit_cache_ts = float(data.get("profit_cache_ts") or saved_at)
        self._last_market_cache_save_ts = saved_at
        n = sum(len(self._open_cache.get(c, {})) for c in ("turbo", "binary"))
        logger.info(
            f"Caché de mercado restaurada desde BD: {n} activos, "
            f"{len(self._profit_cache)} payouts."
        )

    def _save_market_cache_to_db(self) -> None:
        now = time.time()
        if self._last_market_cache_save_ts and now - self._last_market_cache_save_ts < 300:
            return
        n = sum(len(self._open_cache.get(c, {})) for c in ("turbo", "binary"))
        if not n and not self._profit_cache:
            return
        payload = {
            "saved_at": now,
            "open_cache_ts": self._open_cache_ts,
            "profit_cache_ts": self._profit_cache_ts,
            "open_cache": self._open_cache,
            "profit_cache": self._profit_cache,
        }
        self.db.set_setting("market_cache", json.dumps(payload))
        self._last_market_cache_save_ts = now

    # ------------------------------------------------------------------ #
    #  Selección automática de activos de mercado REAL (no-OTC)
    # ------------------------------------------------------------------ #
    def _load_auto_selection_from_db(self) -> None:
        """Restaura al arrancar la última selección conocida (solo para que
        el panel/estado no arranquen vacíos); se recalcula igual al conectar."""
        saved = self.db.get_setting("auto_asset_selection_state")
        if not saved:
            return
        try:
            data = json.loads(saved)
        except json.JSONDecodeError:
            return
        assets = data.get("assets")
        if isinstance(assets, list):
            self._auto_selection = [str(a) for a in assets]
        self._otc_fallback_active = bool(data.get("otc_fallback_active", False))
        if self._auto_selection:
            logger.info(
                f"Selección automática de activos restaurada desde BD: {self._auto_selection}"
            )

    def _persist_auto_selection(self, ranked: list[RankedAsset], reason: str) -> None:
        payload = {
            "ts": time.time(),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,  # real | otc_fallback | idle
            "otc_fallback_active": self._otc_fallback_active,
            "assets": [r.asset for r in ranked],
            "detail": [
                {"asset": r.asset, "payout": r.payout, "market_type": r.market_type,
                 "is_otc": r.is_otc}
                for r in ranked
            ],
        }
        try:
            self.db.set_setting("auto_asset_selection_state", json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo persistir la selección automática de activos: {exc}")

    def _refresh_asset_selection(self, force: bool = False) -> None:
        """Descubre/rota los activos de mercado REAL (no-OTC) a operar.

        Se llama SOLO desde el hilo de fondo, cada `asset_refresh_minutes` (o
        al conectar con force=True). NO golpea la API directamente: usa la
        caché de mercado (self._open_cache / self._profit_cache) que ya
        mantiene _refresh_market() vía get_market_snapshot().

        DECISIÓN DE DISEÑO: la consigna original pedía usar
        stable_api.get_all_open_time(), pero IQOptionClient.get_open_times()
        ya documenta por qué NO se usa ese método (lanza 3 hilos, incluida una
        suscripción digital por websocket, y puede colgarse >30s). En su lugar
        se reutiliza el snapshot rápido (~2s, get_api_option_init_all_v2) que
        el controlador ya refresca cada ciclo con caché de 10s. Así se cumple
        el espíritu del requisito ("no llamar la vía pesada por ciclo") sin
        reintroducir el cuelgue conocido en la Pi.
        """
        if not self.config.get("auto_asset_selection", True):
            self._otc_fallback_active = False
            return
        if not self.connected:
            return

        now = time.time()
        interval = max(1, int(self.config.get("asset_refresh_minutes") or 15)) * 60

        if not self._open_cache:
            # Sin snapshot de apertura todavía (recién conectado / falló el
            # último refresh): se conserva la última selección conocida y no
            # se marca el timestamp, para reintentar barato en el próximo
            # ciclo en vez de esperar el intervalo completo. No hay llamadas
            # de red aquí, solo se reintenta leer la caché ya existente.
            if now - self._asset_wait_logged_ts >= 60:
                logger.warning(
                    "Selección automática: sin snapshot de apertura todavía; "
                    "se conserva la selección vigente y se reintenta en el próximo ciclo."
                )
                self._asset_wait_logged_ts = now
            return

        if not force and self._auto_selection_ts and now - self._auto_selection_ts < interval:
            return

        try:
            ranked = select_real_assets(
                self._open_cache, self._profit_cache,
                min_payout=self.config["min_payout"], max_assets=self.max_assets,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"Selección automática de activos falló ({exc}); se conserva la selección vigente."
            )
            self._auto_selection_ts = now
            return

        reason = "real"
        self._otc_fallback_active = False
        if not ranked and self.config.get("otc_fallback", False):
            try:
                fallback = select_real_assets(
                    self._open_cache, self._profit_cache,
                    min_payout=self.config["min_payout"], max_assets=self.max_assets,
                    include_otc=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Selección automática (fallback OTC) falló: {exc}")
                fallback = []
            if fallback:
                ranked = fallback
                reason = "otc_fallback"
                self._otc_fallback_active = True
                logger.warning(
                    "Sin activos de mercado REAL abiertos; OTC_FALLBACK activo: "
                    f"operando OTC temporalmente -> {[r.asset for r in ranked]}"
                )

        new_list = [r.asset for r in ranked]

        if not new_list:
            reason = "idle"
            if now - self._asset_idle_logged_ts >= interval:
                logger.info(
                    "Sin activos de mercado REAL abiertos (fin de semana/festivo o payout "
                    f"insuficiente). Bot en IDLE explícito: no se opera. "
                    f"Próxima revisión en {interval // 60} min."
                )
                self._asset_idle_logged_ts = now
        else:
            entered, left = diff_selection(self._auto_selection, new_list)
            if entered or left:
                logger.info(
                    f"Rotación de activos ({reason}): entran={entered or '-'} "
                    f"salen={left or '-'} -> vigentes={new_list}"
                )

        self._auto_selection = new_list
        self._auto_selection_ts = now
        self.db.set_assets(new_list, max_assets=self.max_assets)
        self._persist_auto_selection(ranked, reason)

    def get_config(self) -> dict:
        return dict(self.config)

    def update_config(self, new: dict) -> dict:
        """Valida, persiste y aplica en caliente los cambios de configuración."""
        cfg = dict(self.config)
        for k, v in new.items():
            if k in cfg:
                cfg[k] = runtime_config.coerce(k, v)  # ValueError -> 400 en el endpoint
        self.config = cfg
        # Recalcula el epoch ANTES de _apply_config() para que el executor ya
        # quede etiquetado con la época nueva antes de que pueda registrarse
        # ningún trade con esta config. Es el único punto de recálculo en
        # caliente: cualquier cambio de config pasa por aquí (endpoint
        # /api/config -> BotController.update_config).
        self.config_epoch = compute_config_epoch(cfg)
        self.db.set_setting("config", json.dumps(cfg))
        self._apply_config()
        logger.info(f"Configuración actualizada: {cfg}")
        return self.config

    def _apply_config(self) -> None:
        """Aplica la config a los objetos vivos (sin reiniciar el bot)."""
        c = self.config
        self.executor.amount = c["base_amount"]
        self.executor.expiration_minutes = c["expiration_minutes"]
        self.executor.allow_digital = c["allow_digital"]
        # config_epoch vigente: se lee de self.config_epoch (ya recalculado en
        # update_config) para que quede en un único lugar la fuente de verdad.
        self.executor.config_epoch = self.config_epoch
        risk = self.runner.trade_service.risk
        risk.min_payout = c["min_payout"]
        risk.squeeze_factor = c["squeeze_factor"]
        risk.giant_candle_factor = c["giant_candle_factor"]
        risk.lateral_factor = c["lateral_factor"]
        risk.max_entry_delay_seconds = c["max_entry_delay_seconds"]
        risk.reject_low_payout = c["reject_low_payout"]
        risk.reject_bb_squeeze = c["reject_bb_squeeze"]
        risk.reject_giant_candle = c["reject_giant_candle"]
        risk.reject_lateral_market = c["reject_lateral_market"]
        risk.reject_late_entry = c["reject_late_entry"]
        # Gestión de riesgo (capa nueva) en caliente.
        rm = self.runner.trade_service.risk_manager
        if rm is not None:
            rm.enabled = c["risk_mgmt_enabled"]
            rm.cooldown_losses = c["risk_cooldown_losses"]
            rm.cooldown_minutes = c["risk_cooldown_minutes"]
            rm.window_start_hour = c["risk_window_start_hour"]
            rm.window_end_hour = c["risk_window_end_hour"]
            rm.window_max_losses = c["risk_window_max_losses"]
        # Reconstruye la estrategia con los nuevos parámetros de indicadores.
        self.runner.signal_service.strategy = _build_strategy(c)
        # Si se bajó el límite de activos, recorta la lista persistida.
        self.db.set_assets(self.db.get_assets(), max_assets=self.max_assets)

    # ------------------------------------------------------------------ #
    #  Conexión / encendido
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        # El lock evita que el botón "Encender/Reconectar" y la restauración
        # automática intenten hacer login a la vez.
        with self._conn_lock:
            if self.connected:
                return True
            try:
                self.runner.connect_and_prepare()
                self._account_mode = self.client.current_mode()
                # get_balance es rápido (ya respondió en ensure_practice).
                try:
                    self._balance = self.client.get_balance()
                except Exception:  # noqa: BLE001
                    pass
                self.last_error = None
                # Marcar conectado YA. Los datos de mercado (open_times/payouts,
                # que pueden tardar/bloquear justo tras conectar) los carga el
                # hilo de fondo con _refresh_market(), sin bloquear el panel.
                self.connected = True
                logger.success("Controlador conectado y en PRACTICE.")
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = str(exc)
                logger.error(f"No se pudo conectar/preparar: {exc}")
        return self.connected

    def _refresh_market(self, force: bool = False) -> None:
        """Refresca caché de mercado (open times, payouts, balance).

        Se llama SOLO desde el hilo de fondo (o desde connect). Nunca desde el
        endpoint web. Cachea ~10s para no saturar la API.
        """
        if not self.connected:
            return
        now = time.time()
        if not force and now - self._market_cache_ts < 10:
            return

        # Las lecturas van EN PARALELO (antes era en cadena: hasta ~50s si todas
        # iban lentas, bloqueando el hilo de trading). Cada dato es INDEPENDIENTE: si
        # uno se cuelga/falla, NO borra los demás (se conserva el último valor bueno).
        # Apertura y payouts salen de un solo snapshot para no saturar el websocket
        # con dos initialization requests simultáneos.
        results = _gather([
            ("market", self.client.get_market_snapshot, 18),
            ("opcode", self.client.get_actives_opcode, 10),
            ("balance", self.client.get_balance, 10),
        ])
        fetched_at = time.time()

        market, to, lat = results["market"]
        if to:
            logger.warning("Snapshot de mercado tardó demasiado; se conserva el valor previo.")
        elif isinstance(market, dict):
            open_times = market.get("open_times") or {}
            profits = market.get("profits") or {}
            open_count = sum(len(open_times.get(c, {})) for c in ("turbo", "binary"))
            if open_count:
                self._open_cache = open_times
                self._open_cache_ts = fetched_at
                self._open_latency_ms = lat
            elif not self._open_cache:
                logger.warning("Snapshot de mercado sin activos; se espera el siguiente refresh.")
            if profits:
                self._profit_cache = profits
                self._profit_cache_ts = fetched_at
                self._profit_latency_ms = lat
            elif not self._profit_cache:
                logger.warning("Snapshot de mercado sin payouts; se operará con fallback configurado si aplica.")
            self._save_market_cache_to_db()

        opcode, to, _lat = results["opcode"]
        if not to and opcode:
            self._opcode_cache = opcode

        balance, to, _lat = results["balance"]
        if not to and balance is not None:
            self._balance = balance

        self._market_cache_ts = now
        n = sum(len(self._open_cache.get(c, {})) for c in ("turbo", "binary"))
        logger.debug(
            f"Mercado: {n} activos turbo/binary, {len(self._profit_cache)} payouts, "
            f"balance={self._balance}"
        )

    def request_connect(self) -> None:
        """Pide conexión sin bloquear: el hilo de fondo hará el login."""
        if not self.connected:
            self._connect_requested = True

    def request_service_recover(self) -> dict:
        """Recrea cliente/runner y vuelve a conectar sin apagar la intención del bot."""
        self._queue_service_recover()
        logger.warning("Recuperación en caliente solicitada desde el panel.")
        return {"ok": True, "queued": True}

    def _queue_service_recover(self) -> bool:
        with self._control_lock:
            if self.recovering or self._recover_requested:
                return False
            if self.running or self._want_running or self.db.get_bot_enabled():
                self._want_running = True
                self.db.set_bot_enabled(True)
            self._recover_requested = True
            return True

    def start(self) -> dict:
        """Enciende el bot. NO bloquea: si falta conectar, lo hará el hilo de
        fondo y el bot quedará operando en cuanto conecte."""
        self.db.set_bot_enabled(True)
        self._want_running = True
        if self.connected:
            self.running = True
        else:
            self._connect_requested = True
        logger.info("Encendido solicitado desde el panel.")
        return {"ok": True}

    def stop(self) -> dict:
        self._want_running = False
        self.running = False
        self.db.set_bot_enabled(False)
        logger.info("Bot APAGADO desde el panel.")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    #  Hilo de fondo
    # ------------------------------------------------------------------ #
    def start_background(self) -> None:
        # Importante: NO conectar aquí (bloquearía el arranque del servidor).
        # La reconexión automática se hace dentro del hilo de fondo.
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Hilo del controlador iniciado.")

    def _loop(self) -> None:
        # Restaura el estado previo (si el bot quedó encendido) sin bloquear el
        # arranque del servidor: solo pone banderas, el login lo hace el bucle.
        if not self._restored:
            self._restored = True
            if self.db.get_bot_enabled():
                logger.info("Restaurando estado: el bot quedó encendido.")
                self._want_running = True
                self._connect_requested = True

        while not self._shutdown:
            try:
                if self._consume_recover_request():
                    self._recover_service()
                    continue

                # 1) ¿Hay que conectar? (no bloquea el panel; muestra "conectando")
                if self._connect_requested and not self.connected:
                    self.connecting = True
                    self._connect_requested = False
                    try:
                        self.connect()
                    finally:
                        self.connecting = False
                    if self.connected:
                        # Llenar la caché de mercado YA (en este hilo de fondo,
                        # no bloquea el panel) para que los activos no salgan
                        # "desconocido".
                        self._refresh_market(force=True)
                        # Descubrir activos de mercado real YA al conectar (no
                        # esperar el intervalo de rotación).
                        self._refresh_asset_selection(force=True)
                        # Resolver operaciones que quedaron 'pending' (p.ej. la app
                        # se cerró antes de obtener el resultado).
                        try:
                            self.executor.resolve_pending_real()
                            self.executor.resolve_pending_paper()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"No se pudieron resolver pendientes: {exc}")
                        if self._want_running:
                            self.running = True
                    continue

                if not self.connected:
                    time.sleep(0.5)
                    continue

                # 2) Conectado: refrescar caché y, si toca, escanear.
                self._refresh_market()  # balance + payouts (cacheado ~10s)
                self._refresh_asset_selection()  # rota activos cada asset_refresh_minutes
                if self.client.needs_rebuild():
                    if self._queue_service_recover():
                        logger.warning("Cliente IQ marcado para reconstrucción tras refresh de mercado.")
                    continue
                if self.running:
                    self._wait_for_next_candle()
                    if self._shutdown or not self.running:
                        continue
                    if self._consume_recover_request():
                        self._recover_service()
                        continue
                    # Filtra activos usando la caché (sin llamadas de red por activo).
                    # Si IQ no entregó snapshot de apertura todavía, no se bloquea
                    # todo: se escanea en modo degradado y la orden/fallback confirma.
                    assets = self.db.get_assets()[: self.max_assets]
                    active = [a for a in assets if self._asset_scannable(a)]
                    skipped = [a for a in assets if a not in active]
                    if skipped:
                        logger.info(f"Activos omitidos (no abiertos): {', '.join(skipped)}")
                    if active and not self._open_cache:
                        logger.warning(
                            "Sin snapshot de apertura; se escanean activos configurados en modo degradado."
                        )
                    if active:
                        self.runner.scan_once(active, instrument_resolver=self.resolve_instrument,
                                              market_meta=self.market_meta)
                    if self.client.needs_rebuild():
                        if self._queue_service_recover():
                            logger.warning("Cliente IQ marcado para reconstrucción tras timeouts de velas.")
                else:
                    time.sleep(2)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"Error en el ciclo del controlador: {exc}")
                time.sleep(5)

    def _wait_for_next_candle(self) -> None:
        period = self.settings.timeframe_seconds
        now = time.time()
        next_boundary = (now // period + 1) * period
        sleep_s = next_boundary - now + 2.0
        # Dormir en tramos para reaccionar rápido a un apagado.
        end = time.time() + max(sleep_s, 0.1)
        while time.time() < end and not self._shutdown and self.running:
            if self._recovery_pending():
                break
            time.sleep(0.25)

    def _recovery_pending(self) -> bool:
        with self._control_lock:
            return self._recover_requested

    def _consume_recover_request(self) -> bool:
        with self._control_lock:
            if not self._recover_requested:
                return False
            self._recover_requested = False
            return True

    def _clear_connection_state(self) -> None:
        self._opcode_cache = {}
        self._balance = 0.0
        self._market_cache_ts = 0.0
        self._open_latency_ms = None
        self._profit_latency_ms = None

    def _recover_service(self) -> None:
        """Recrea el cliente IQ y deja el bot listo para reconectar desde el loop."""
        self.recovering = True
        keep_running = self._want_running or self.running or self.db.get_bot_enabled()
        old_client = self.client
        open_assets = sorted(self.executor.open_assets)
        logger.warning("Iniciando recuperación en caliente del servicio interno.")
        try:
            self.running = False
            self.connected = False
            self.connecting = False
            self._connect_requested = False
            self._account_mode = None
            self._clear_connection_state()

            if open_assets:
                logger.warning(
                    "Hay operaciones abiertas; no se cierra el socket anterior "
                    f"para no interferir con resultados pendientes: {', '.join(open_assets)}"
                )
            else:
                old_client.close(timeout=2.0)

            self.runner, self.client, self.executor = build_runner(
                self.settings, self.db, self.config, executor=self.executor
            )
            if keep_running:
                self._want_running = True
                self.db.set_bot_enabled(True)
            self._connect_requested = True
            self.last_error = None
            logger.success("Servicio interno reconstruido; la reconexión queda en cola.")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"No se pudo recuperar el servicio: {exc}"
            logger.exception(self.last_error)
            if keep_running:
                self._want_running = True
                self.db.set_bot_enabled(True)
        finally:
            self.recovering = False

    def shutdown(self) -> None:
        self._shutdown = True
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------ #
    #  Estado de apertura de activos
    # ------------------------------------------------------------------ #
    def asset_info(self, assets: list[str]) -> dict[str, dict]:
        """Estado + payout de cada activo para el panel.

        status:
          - 'unknown'   -> bot desconectado (no se puede consultar)
          - 'open'      -> mercado abierto, operable
          - 'closed'    -> existe pero está cerrado ahora
          - 'not_found' -> el nombre no existe en IQ Option (¿typo?)
          - 'otc_off'   -> es OTC pero ALLOW_OTC=false (se ignorará)
          - 'degraded'  -> conectado, pero IQ no entregó snapshot de apertura
        payout: fracción 0-1 (o None si desconocido).
        """
        # Solo lee de la caché que mantiene el hilo de fondo (sin llamadas de red).
        if not self.connected:
            return {a: {"status": "unknown", "payout": None} for a in assets}
        if not self._open_cache:
            return {
                a: {
                    "status": "degraded" if self.asset_exists(a) else "not_found",
                    "payout": self._cached_payout(a),
                }
                for a in assets
            }
        return {a: {"status": self.classify(a),
                    "payout": self._cached_payout(a)} for a in assets}

    def classify(self, asset: str) -> str:
        """Estado de un activo usando SOLO la caché (sin red)."""
        if not self.connected or not self._open_cache:
            return "unknown"
        # OTC permitido si el usuario lo activó a mano (allow_otc) o si la
        # selección automática entró en OTC_FALLBACK (sin mercado real abierto).
        otc_allowed = self.config["allow_otc"] or self._otc_fallback_active
        if asset.upper().endswith("OTC") and not otc_allowed:
            return "otc_off"
        return self.client.classify_asset(asset, self._open_cache)

    def _asset_scannable(self, asset: str) -> bool:
        if not self.asset_exists(asset):
            return False
        status = self.classify(asset)
        if status == "open":
            return True
        return status == "unknown" and self.connected and not self._open_cache

    def market_meta(self) -> dict:
        """Frescura de las cachés de mercado y latencia de su última lectura, para
        rellenar trade_api_context en cada entrada. Solo auditoría: no decide nada."""
        now = time.time()
        return {
            "open_time_cache_age_seconds": round(now - self._open_cache_ts, 3) if self._open_cache_ts else None,
            "profit_cache_age_seconds": round(now - self._profit_cache_ts, 3) if self._profit_cache_ts else None,
            "api_latency_open_time_ms": round(self._open_latency_ms, 1) if self._open_latency_ms is not None else None,
            "api_latency_profit_ms": round(self._profit_latency_ms, 1) if self._profit_latency_ms is not None else None,
        }

    def _cached_payout(self, asset: str):
        payout = self.client.payout_from(asset, self._profit_cache)
        return payout if payout > 0 else None

    def resolve_instrument(self, asset: str) -> tuple[float, str]:
        """(payout, market_type) del activo para operar.

        Prioriza turbo/binary con payout real desde la caché. Si no hay, y
        operate_without_payout está activo, devuelve 'binary_assumed' para que el
        ejecutor intente la BINARIA PRIMERO (cadena binary -> digital -> paper).
        Solo si operate_without_payout está apagado se intenta digital directo.
        market_type: turbo|binary|binary_assumed|digital|unavailable.
        """
        info = self._profit_cache.get(asset, {}) or {}
        payout = self.client.payout_from(asset, self._profit_cache)
        if payout and payout > 0:
            market_type = "turbo" if (info.get("turbo") or 0) else "binary"
            return payout, market_type

        # Sin payout real turbo/binary. PRIORIDAD BINARIAS: asumir binaria y dejar
        # que el ejecutor la intente primero; si IQ la rechaza, recién prueba digital.
        # (No llamamos get_digital_payout aquí: ahorra ~8s y el payout real se
        # descubre del profit al cerrar.)
        if self.config.get("operate_without_payout", False):
            assumed = float(self.config.get("assumed_payout") or 0.85)
            if assumed > 0:
                return assumed, "binary_assumed"

        # operate_without_payout APAGADO: intentar solo digital si tiene payout.
        if self.config.get("allow_digital", True):
            dpay = self.client.get_digital_payout(asset)
            if dpay and dpay > 0:
                return dpay, "digital"
        return 0.0, "unavailable"

    def asset_exists(self, asset: str) -> bool:
        """Validación 1/3: ¿el activo existe en el catálogo de IQ Option?"""
        if not self._opcode_cache:
            return True  # sin catálogo aún, no bloquear
        return asset in self._opcode_cache

    def can_trade(self, asset: str) -> tuple[bool, str]:
        """Validación de 3 capas antes de operar:
        1) existe (ACTIVES_OPCODE)  2) abierto (init_v2)  3) payout suficiente.
        Devuelve (ok, motivo_si_no).
        """
        if not self.asset_exists(asset):
            return False, "not_found"
        if not self._asset_scannable(asset):
            return False, "closed"
        payout = self.client.payout_from(asset, self._profit_cache)
        if payout < self.config["min_payout"]:
            return False, "low_payout"
        return True, ""

    def available_assets(self) -> list[dict]:
        """Lista de activos para binarias (turbo/binary) desde la API.

        Cada item: {name, open, is_otc, payout, selected}. Ordenados: abiertos
        primero, luego por nombre. Si ALLOW_OTC=false, oculta los OTC.
        """
        if not self.connected or not self._open_cache:
            return []
        current = set(self.db.get_assets())
        merged: dict[str, bool] = {}
        for cat in ("turbo", "binary"):
            for name, info in self._open_cache.get(cat, {}).items():
                if name.upper().endswith("OTC") and not self.config["allow_otc"]:
                    continue
                opened = bool(info.get("open"))
                # Abierto si lo está en cualquier categoría.
                merged[name] = merged.get(name, False) or opened

        out = []
        for name, opened in merged.items():
            out.append({
                "name": name,
                "open": opened,
                "is_otc": name.upper().endswith("OTC"),
                "payout": self._cached_payout(name),
                "selected": name in current,
            })
        # Orden: operables AHORA (abierto + con payout) primero, luego abiertos
        # sin payout, luego cerrados; alfabético dentro de cada grupo.
        out.sort(key=lambda x: (not (x["open"] and x["payout"]), not x["open"], x["name"]))
        return out

    # ------------------------------------------------------------------ #
    #  Estado para el panel
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        balance = self._balance if self.connected else 0.0
        assets = self.db.get_assets()
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "recovering": self.recovering or self._recovery_pending(),
            "running": self.running,
            "wanted_running": self._want_running,
            "account_mode": self._account_mode or self.settings.balance_mode,
            "allow_real": self.settings.allow_real,
            "balance": round(balance, 2),
            "assets": assets,
            "asset_info": self.asset_info(assets),
            "max_assets": self.max_assets,
            "open_positions": sorted(self.executor.open_assets),
            "base_amount": self.config["base_amount"],
            "expiration_minutes": self.config["expiration_minutes"],
            "min_payout": self.config["min_payout"],
            "allow_otc": self.config["allow_otc"],
            "auto_asset_selection": self.config.get("auto_asset_selection", True),
            "asset_refresh_minutes": self.config.get("asset_refresh_minutes", 15),
            "otc_fallback": self.config.get("otc_fallback", False),
            "otc_fallback_active": self._otc_fallback_active,
            "last_error": self.last_error,
            "config_epoch": self.config_epoch,
        }
