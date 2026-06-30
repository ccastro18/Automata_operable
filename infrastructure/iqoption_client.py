"""Adaptador para la librería comunitaria `iqoptionapi`.

TODA la dependencia de iqoptionapi vive aquí. Si la librería cambia o se
rompe, solo se toca este archivo.

BLINDAJES DE SEGURIDAD (cuenta DEMO):
  1. Nunca se llama change_balance("REAL").
  2. Antes de cualquier compra se verifica que el modo activo sea PRACTICE.
  3. Si allow_real=False y el modo no es PRACTICE -> se aborta.
"""
from __future__ import annotations

import threading
import time

from loguru import logger

from infrastructure.iqoption_active_ids import apply_active_id_patch


def _run_with_timeout(fn, timeout: float):
    """Ejecuta fn() con límite de tiempo. Devuelve (resultado, timed_out)."""
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
        return None, True
    if "e" in box:
        raise box["e"]
    return box.get("v"), False


class IQOptionClient:
    PRACTICE = "PRACTICE"

    # Tras esta cantidad de timeouts seguidos de velas, se asume que el websocket
    # quedó mudo (TCP vivo pero sin datos) y se fuerza una reconexión.
    _CANDLE_TIMEOUT_RECONNECT = 3
    _INIT_TIMEOUT = 12.0

    def __init__(self, email: str, password: str, allow_real: bool = False):
        self._email = email
        self._password = password
        self._allow_real = allow_real
        self._api = None  # instancia de IQ_Option
        self._candle_timeouts = 0  # timeouts de velas consecutivos (watchdog)
        self._ws_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Conexión
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        # Import diferido: así el resto del código no depende de la librería
        from iqoptionapi.stable_api import IQ_Option

        logger.info("Conectando a IQ Option...")
        self._api = IQ_Option(self._email, self._password)
        check, reason = self._api.connect()
        if not check:
            raise RuntimeError(f"No se pudo conectar a IQ Option: {reason}")
        patched_count = apply_active_id_patch()
        logger.info(f"IDs de activos iqoptionapi actualizados localmente: {patched_count}.")
        logger.success("Conectado a IQ Option.")

    def close(self, timeout: float = 3.0) -> None:
        """Cierra el websocket actual si la librería lo permite, sin colgar el bot."""
        api = self._api
        if api is None:
            return

        def _close():
            raw_api = getattr(api, "api", None)
            close = getattr(raw_api, "close", None)
            if callable(close):
                close()

        try:
            _result, timed_out = _run_with_timeout(_close, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo cerrar la conexión anterior de IQ Option: {exc}")
            return
        if timed_out:
            logger.warning(f"Cierre de conexión IQ Option tardó más de {timeout:.0f}s; se abandona en segundo plano.")
        else:
            logger.info("Conexión anterior de IQ Option cerrada.")
        self._api = None

    def ensure_practice(self) -> None:
        """Blindaje #2: fuerza y verifica cuenta PRACTICE."""
        if self._allow_real:
            logger.warning("ALLOW_REAL=true detectado. Aun así se fuerza PRACTICE.")

        self._api.change_balance(self.PRACTICE)
        time.sleep(1)

        mode = self.current_mode()
        if mode != self.PRACTICE:
            raise RuntimeError(
                f"SEGURIDAD: el modo activo es '{mode}', no PRACTICE. Abortando."
            )
        logger.success(f"Cuenta forzada a PRACTICE. Balance demo: {self.get_balance()}")

    def current_mode(self) -> str:
        try:
            return str(self._api.get_balance_mode()).upper()
        except Exception:  # noqa: BLE001 - algunas versiones no exponen el método
            return self.PRACTICE

    def assert_practice(self) -> None:
        """Se llama justo antes de cada compra real."""
        mode = self.current_mode()
        if mode != self.PRACTICE:
            raise RuntimeError(
                f"SEGURIDAD: intento de operar en modo '{mode}'. Solo PRACTICE."
            )

    # ------------------------------------------------------------------ #
    #  Datos de mercado
    # ------------------------------------------------------------------ #
    def _raw_api(self):
        return getattr(self._api, "api", None)

    def _ensure_ws_connected(self) -> bool:
        """Verifica/reconecta el websocket antes de pedir snapshots de mercado."""
        if self._api is None:
            return False
        try:
            if self._api.check_connect():
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo verificar websocket IQ Option: {exc}")

        with self._ws_lock:
            try:
                if self._api.check_connect():
                    return True
            except Exception:  # noqa: BLE001
                pass

            try:
                logger.warning("Websocket IQ Option no conectado; se intenta reconectar antes del refresh de mercado.")
                check, reason = self._api.connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Reconexión websocket IQ Option falló: {exc}")
                return False

            if not check:
                logger.warning(f"Reconexión websocket IQ Option rechazada: {reason}")
                return False

            try:
                self._api.change_balance(self.PRACTICE)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"No se pudo reafirmar cuenta PRACTICE tras reconectar: {exc}")
                return False
            time.sleep(0.5)
            logger.success("Websocket IQ Option reconectado para refresh de mercado.")
            return True

    def _wait_for_attr(self, attr: str, timeout: float):
        raw = self._raw_api()
        if raw is None:
            return None, True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = getattr(raw, attr, None)
            if value is not None:
                return value, False
            time.sleep(0.05)
        return None, True

    def _request_init_v2(self, timeout: float = _INIT_TIMEOUT) -> dict:
        """Lee initialization-data v2 sin usar el busy-wait de stable_api."""
        if not self._ensure_ws_connected():
            return {}
        raw = self._raw_api()
        if raw is None:
            return {}
        try:
            raw.api_option_init_all_result_v2 = None
            raw.get_api_option_init_all_v2()
            data, timed_out = self._wait_for_attr("api_option_init_all_result_v2", timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo solicitar get_api_option_init_all_v2(): {exc}")
            return {}
        if timed_out:
            # Si alguna llamada vieja de stable_api quedó esperando, esto evita que
            # siga girando en `while ... == None: pass`.
            try:
                raw.api_option_init_all_result_v2 = {}
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"get_api_option_init_all_v2() TIMEOUT ({timeout:.0f}s).")
            return {}
        return data if isinstance(data, dict) else {}

    def _request_init_all(self, timeout: float = _INIT_TIMEOUT) -> dict:
        """Lee api_option_init_all sin el bucle activo de stable_api.get_all_init()."""
        if not self._ensure_ws_connected():
            return {}
        raw = self._raw_api()
        if raw is None:
            return {}
        try:
            raw.api_option_init_all_result = None
            raw.get_api_option_init_all()
            data, timed_out = self._wait_for_attr("api_option_init_all_result", timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo solicitar api_option_init_all(): {exc}")
            return {}
        if timed_out:
            try:
                raw.api_option_init_all_result = {}
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"api_option_init_all() TIMEOUT ({timeout:.0f}s).")
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _init_root(init: dict) -> dict:
        result = init.get("result") if isinstance(init, dict) else None
        return result if isinstance(result, dict) else (init or {})

    def _iter_binary_sections(self, init: dict):
        root = self._init_root(init)
        for kind in ("turbo", "binary"):
            actives = (root.get(kind) or {}).get("actives", {})
            if isinstance(actives, dict):
                for data in actives.values():
                    if isinstance(data, dict):
                        yield kind, data

    def _open_times_from_init(self, init: dict) -> dict:
        result: dict = {"turbo": {}, "binary": {}}
        for kind, data in self._iter_binary_sections(init):
            name = self._clean_active_name(data.get("name", ""))
            if not name:
                continue
            enabled = data.get("enabled", True)
            suspended = data.get("is_suspended", False)
            result[kind][name] = {"open": bool(enabled) and not bool(suspended)}
        return result

    def _profits_from_init(self, init: dict) -> dict:
        result: dict = {}
        for kind, data in self._iter_binary_sections(init):
            name = self._clean_active_name(data.get("name", ""))
            if not name:
                continue
            profit = ((data.get("option") or {}).get("profit") or {})
            commission = profit.get("commission")
            if commission is None:
                continue
            try:
                payout = (100.0 - float(commission)) / 100.0
            except (TypeError, ValueError):
                continue
            result.setdefault(name, {})[kind] = payout
        return result

    def get_candles(self, asset: str, interval: int, count: int,
                    endtime: float | None = None,
                    timeout: float = 20.0) -> list[dict]:
        """Velas con BLINDAJE anti-cuelgue.

        La librería implementa get_candles con `while ...: pass` (busy-wait SIN
        timeout): si el websocket queda mudo, ese bucle gira al 100% de CPU para
        siempre y congela TODO el bot (acapara el GIL). Aquí lo acotamos con un
        timeout duro, liberamos el hilo huérfano y, si los timeouts se acumulan,
        forzamos una reconexión para que el flujo de velas se recupere solo.
        """
        return self._get_candles_guarded(asset, interval, count,
                                          endtime or time.time(), timeout)

    def _get_candles_guarded(self, asset, interval, count, endtime,
                             timeout: float) -> list[dict]:
        try:
            candles, timed_out = _run_with_timeout(
                lambda: self._api.get_candles(asset, interval, count, endtime), timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"get_candles({asset}) excepción: {exc}")
            return []

        if timed_out:
            # 1) Liberar el hilo huérfano: su bucle es `while ...candles_data == None: pass`.
            #    Poniendo candles_data a un valor no-None, la condición se rompe y el
            #    hilo daemon termina en vez de seguir girando al 100% de CPU.
            try:
                self._api.candles.candles_data = []
            except Exception:  # noqa: BLE001
                pass
            self._candle_timeouts += 1
            logger.warning(
                f"get_candles({asset}) TIMEOUT duro ({timeout:.0f}s) "
                f"[{self._candle_timeouts}/{self._CANDLE_TIMEOUT_RECONNECT}] -> sin velas."
            )
            # 2) Si se acumulan, el socket está mudo: reconectar para auto-sanar.
            if self._candle_timeouts >= self._CANDLE_TIMEOUT_RECONNECT:
                self._reconnect_after_stall()
            return []

        self._candle_timeouts = 0  # respondió: el socket está vivo
        return candles or []

    def _reconnect_after_stall(self) -> None:
        """Reconecta el websocket cuando se detecta que quedó mudo (varios
        timeouts seguidos de velas). Acotado con timeout para no colgarse aquí."""
        logger.warning("Websocket mudo: forzando reconexión de IQ Option…")
        try:
            res, timed_out = _run_with_timeout(lambda: self._api.connect(), 30)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Reconexión falló: {exc}")
            self._candle_timeouts = 0  # evitar reconectar en bucle cada llamada
            return
        if timed_out:
            logger.error("Reconexión TIMEOUT (30s); se reintentará más tarde.")
        else:
            ok = res[0] if isinstance(res, (tuple, list)) and res else res
            logger.success(f"Reconexión completada (ok={ok}).")
        self._candle_timeouts = 0

    def price_at(self, asset: str, when_epoch: float, interval: int = 60):
        """Precio (close) de la vela que cubre `when_epoch`. Para resolver
        paper-trades vencidos tras un reinicio. Devuelve None si no hay datos."""
        try:
            candles = self._get_candles_guarded(
                asset, interval, 5, when_epoch + interval + 1, timeout=20.0
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"price_at({asset}) falló: {exc}")
            return None
        if not candles:
            return None
        for c in candles:
            frm = c.get("from")
            to = c.get("to", (frm or 0) + interval)
            if frm is not None and frm <= when_epoch < to:
                return c.get("close")
        return candles[-1].get("close")

    def get_open_times(self) -> dict:
        """Activos turbo/binary con su estado de apertura.

        IMPORTANTE: NO se usa get_all_open_time() porque se cuelga (>30s): ese
        método se suscribe a opciones digitales por websocket. En su lugar se
        usa get_all_init_v2() (rápido, ~2s), que trae turbo/binary con el flag
        is_suspended.

        Devuelve la misma estructura que get_all_open_time:
            {"turbo": {"EURUSD": {"open": True}, ...}, "binary": {...}}
        """
        return self._open_times_from_init(self._request_init_v2())

    def get_actives_opcode(self) -> dict:
        """Catálogo {nombre: id} de TODOS los activos (instantáneo, estático).

        Sirve para validar que un nombre existe / es operable. No trae estado
        de apertura ni payout.
        """
        try:
            return dict(self._api.get_all_ACTIVES_OPCODE() or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"No se pudo leer get_all_ACTIVES_OPCODE(): {exc}")
            return {}

    @staticmethod
    def _clean_active_name(raw: str) -> str:
        """'front.EURUSD' -> 'EURUSD'; 'front.CHFJPY-op' -> 'CHFJPY'.
        Conserva el sufijo -OTC."""
        name = (raw or "").replace("front.", "")
        if name.endswith("-op"):
            name = name[:-3]
        return name

    @staticmethod
    def classify_asset(asset: str, open_times: dict) -> str:
        """Clasifica un activo según el dict de get_all_open_time().

        Devuelve: 'open' | 'closed' | 'not_found'.
        """
        found = False
        for category in ("turbo", "binary", "forex", "digital"):
            cat = open_times.get(category, {})
            if asset in cat:
                found = True
                if cat[asset].get("open"):
                    return "open"
        return "closed" if found else "not_found"

    def is_open(self, asset: str) -> bool:
        """Revisa turbo/binary/forex/digital en get_all_open_time()."""
        data = self.get_open_times()
        if not data:
            return True  # no bloquear por un fallo de lectura
        return self.classify_asset(asset, data) == "open"

    def get_profits(self) -> dict:
        """Payouts turbo/binary sin usar el busy-wait de stable_api.get_all_profit()."""
        return self._profits_from_init(self._request_init_all())

    @staticmethod
    def payout_from(asset: str, profits: dict) -> float:
        """Payout (fracción 0-1) de un activo desde el dict de get_all_profit().

        Para 5M se usa 'turbo' con fallback a 'binary'.
        """
        info = profits.get(asset, {})
        payout = info.get("turbo") or info.get("binary") or 0.0
        try:
            payout = float(payout)
        except (TypeError, ValueError):
            payout = 0.0
        # Algunas versiones devuelven porcentaje (85) y otras fracción (0.85)
        return payout / 100 if payout > 1 else payout

    def get_payout(self, asset: str) -> float:
        """Payout (fracción 0-1) para opciones de 5M (turbo). Fallback a binary."""
        return self.payout_from(asset, self.get_profits())

    # ------------------------------------------------------------------ #
    #  Opciones DIGITALES (para no-OTC sin turbo/binary)
    # ------------------------------------------------------------------ #
    def get_digital_payout(self, asset: str, timeout: float = 8.0) -> float:
        """Payout (fracción 0-1) de opción digital. 0.0 si no hay/falla.

        BLINDAJE: la función de la librería hace busy-wait SIN fin si no se le
        pasa `seconds`; le ponemos límite y además la envolvemos con timeout
        duro por si la suscripción al price-splitter se cuelga (websocket).
        """
        try:
            val, timed_out = _run_with_timeout(
                lambda: self._api.get_digital_payout(asset, seconds=timeout), timeout + 3
            )
        except Exception as exc:  # noqa: BLE001 (KeyError si el activo no está en ACTIVES)
            logger.warning(f"get_digital_payout({asset}) EXCEPCIÓN: {exc}")
            return 0.0
        # Diagnóstico explícito: distingue 'no llegó cotización' de 'timeout duro'.
        if timed_out:
            logger.warning(f"get_digital_payout({asset}) TIMEOUT duro ({timeout + 3:.0f}s) -> sin payout digital.")
            return 0.0
        if not val:
            logger.info(f"get_digital_payout({asset}) sin cotización digital tras {timeout:.0f}s (raw={val!r}) "
                        "-> IQ no ofrece digital en este activo ahora.")
            return 0.0
        payout = float(val) if isinstance(val, (int, float)) else 0.0
        payout = payout / 100 if payout > 1 else payout
        logger.success(f"get_digital_payout({asset}) = {payout:.2f} (raw={val!r}) -> DIGITAL disponible.")
        return payout

    def buy_digital(self, amount: float, asset: str, action: str, duration: int,
                    timeout: float = 15.0):
        """Compra digital. Devuelve (ok, order_id). Nunca se cuelga (timeout duro).

        La librería usa `while ...: pass` esperando el id; lo acotamos.
        """
        self.assert_practice()  # Blindaje #3
        try:
            res, timed_out = _run_with_timeout(
                lambda: self._api.buy_digital_spot_v2(asset, amount, action, duration), timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"buy_digital({asset}) excepción: {exc}")
            return False, None
        if timed_out or not res:
            logger.warning(f"buy_digital({asset}) timeout/sin respuesta.")
            return False, None
        status, order_id = res
        return (bool(status) and isinstance(order_id, int)), order_id

    def check_result_digital(self, order_id, timeout: float = 360.0,
                             poll_interval: float = 3.0) -> tuple[str, float]:
        """Resultado de una digital. Polling de get_async_order CON sleeps
        (la librería hace busy-wait infinito; aquí no). Devuelve (result, profit)."""
        from domain.models import TradeResult

        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._digital_position_msg(order_id)
            if msg is not None and msg.get("status") == "closed":
                invest = float(msg.get("invest") or 0)
                if msg.get("close_reason") == "expired":
                    profit = float(msg.get("close_profit") or 0) - invest
                else:
                    profit = float(msg.get("pnl_realized") or 0)
                profit = round(profit, 2)
                if profit > 0:
                    return TradeResult.WIN.value, profit
                if profit < 0:
                    return TradeResult.LOSS.value, profit
                return TradeResult.DRAW.value, 0.0
            time.sleep(poll_interval)
        logger.error(f"No se obtuvo el resultado digital de id={order_id} en {timeout:.0f}s.")
        return TradeResult.ERROR.value, 0.0

    def _digital_position_msg(self, order_id):
        """Mensaje 'position-changed' de una orden digital (o None si aún no llega)."""
        try:
            data, timed_out = _run_with_timeout(
                lambda: self._api.get_async_order(order_id), 10
            )
        except Exception:  # noqa: BLE001 - KeyError si la posición aún no está en order_async
            return None
        if timed_out or not isinstance(data, dict):
            return None
        pos = data.get("position-changed")
        if isinstance(pos, dict):
            return pos.get("msg")
        return None

    def get_balance(self) -> float:
        try:
            return float(self._api.get_balance())
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------ #
    #  Operación
    # ------------------------------------------------------------------ #
    def buy(self, amount: float, asset: str, action: str, expiration: int):
        """Compra binaria. Devuelve (ok, order_id).

        IMPORTANTE: el order_id se devuelve TAL CUAL (numérico). NO convertir a
        str: check_win_v2/v3 lo necesitan en su tipo original o no encuentran la
        orden (y un WIN quedaría como 'error').
        """
        self.assert_practice()  # Blindaje #3
        status, order_id = self._api.buy(amount, asset, action, expiration)
        return bool(status), order_id

    def check_result(self, order_id, timeout: float = 360.0, poll_interval: float = 3.0) -> tuple[str, float]:
        """Consulta cómo cerró la operación. Devuelve (result, profit).

        NO se usa check_win_v2/v3/v4: todos dependen de get_betinfo(), que en
        muchas cuentas se cuelga ("get_betinfo time out need reconnect"). En su
        lugar se hace polling de get_optioninfo(), que sí responde y trae las
        operaciones cerradas con su resultado.

        Interpreta: win='win' -> WIN, 'loose' -> LOSS, otro -> DRAW.
        profit = win_amount - amount (neto) en WIN; -amount en LOSS.
        """
        from domain.models import TradeResult

        try:
            target = int(order_id)
        except (TypeError, ValueError):
            target = order_id

        deadline = time.time() + timeout
        while time.time() < deadline:
            option = self._find_closed_option(target)
            if option is not None:
                return self._interpret_option(option)
            time.sleep(poll_interval)

        logger.error(f"No se obtuvo el resultado de id={order_id} en {timeout:.0f}s.")
        return TradeResult.ERROR.value, 0.0

    def _find_closed_option(self, target, fetch: int = 100):
        """Busca la orden en las operaciones cerradas (get_optioninfo)."""
        data, timed_out = _run_with_timeout(lambda: self._api.get_optioninfo(fetch), 10)
        if timed_out or not data:
            return None
        try:
            closed = data["msg"]["result"]["closed_options"]
        except (KeyError, TypeError):
            return None
        for o in closed or []:
            ids = o.get("id")
            if isinstance(ids, (list, tuple, set)):
                if any(self._same_order_id(candidate, target) for candidate in ids):
                    return o
            elif self._same_order_id(ids, target):
                return o
        return None

    @staticmethod
    def _same_order_id(candidate, target) -> bool:
        return str(candidate) == str(target)

    def _interpret_option(self, o: dict) -> tuple[str, float]:
        from domain.models import TradeResult

        win = str(o.get("win", "")).lower()
        amount = float(o.get("amount") or 0)
        win_amount = float(o.get("win_amount") or 0)
        if win == "win":
            return TradeResult.WIN.value, round(win_amount - amount, 2)
        if win in ("loose", "loss", "lose"):
            return TradeResult.LOSS.value, round(-amount, 2)
        return TradeResult.DRAW.value, 0.0
