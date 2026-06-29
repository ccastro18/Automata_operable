"""Lógica de un ciclo del bot (escaneo de activos).

Ya NO contiene el bucle infinito: el BotController es quien decide cuándo
escanear (encender/apagar desde el panel). Aquí solo está:
  - conexión + forzado de PRACTICE
  - validación de activos abiertos
  - escaneo de un activo (señal -> filtros -> operar demo / paper-trade)
"""
from __future__ import annotations

import time
from datetime import datetime

from loguru import logger

from application.signal_service import SignalService
from application.trade_service import TradeService
from config.settings import Settings
from domain.models import TradeLogContext
from infrastructure.iqoption_client import IQOptionClient
from infrastructure.trade_executor import TradeExecutor, is_otc_asset


class BotRunner:
    def __init__(
        self,
        settings: Settings,
        client: IQOptionClient,
        signal_service: SignalService,
        trade_service: TradeService,
        executor: TradeExecutor,
    ):
        self.settings = settings
        self.client = client
        self.signal_service = signal_service
        self.trade_service = trade_service
        self.executor = executor

    # ------------------------------------------------------------------ #
    def connect_and_prepare(self) -> None:
        self.client.connect()
        self.client.ensure_practice()

    def validate_assets(self, assets: list[str]) -> list[str]:
        valid = []
        for asset in assets:
            if is_otc_asset(asset) and not self.settings.allow_otc:
                logger.warning(f"[{asset}] OTC ignorado (ALLOW_OTC=false).")
                continue
            if not self.client.is_open(asset):
                logger.warning(f"[{asset}] cerrado ahora mismo. Se omite.")
                continue
            valid.append(asset)
        return valid

    # ------------------------------------------------------------------ #
    def scan_once(self, active_assets: list[str], instrument_resolver=None,
                  market_meta=None) -> None:
        """Escanea cada activo. instrument_resolver(asset)->(payout, market_type)
        usa la caché (turbo/binary) y, si no hay, intenta digital.
        market_meta() -> dict con frescura/latencia de cachés (solo auditoría)."""
        for asset in active_assets:
            try:
                self._scan_asset(asset, instrument_resolver, market_meta)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{asset}] error al escanear: {exc}")

    def _scan_asset(self, asset: str, instrument_resolver=None, market_meta=None) -> None:
        result = self.signal_service.get_signal(asset)
        signal = result.signal

        # Sin velas válidas no hay nada que hacer (evita resolver paper-trades
        # contra un precio 0).
        if result.last_close <= 0:
            logger.warning(f"[{asset}] sin velas válidas; se omite este ciclo.")
            return

        # Resolver paper-trades vencidos de este activo con el precio actual.
        self.executor.resolve_due_virtuals(asset, result.last_close, datetime.now())

        if not signal.is_actionable:
            return

        entry_delay = time.time() - result.candle_close_ts
        if instrument_resolver:
            payout, market_type = instrument_resolver(asset)
        else:
            payout, market_type = self.client.get_payout(asset), "turbo"
        payout = payout or 0.0

        logger.info(
            f"[{asset}] SEÑAL {signal.direction.value.upper()} "
            f"({signal.strategy_reason}) | payout={payout:.2f} | tipo={market_type} | delay={entry_delay:.1f}s"
        )

        log_ctx = self._build_log_context(asset, payout, market_type, result, market_meta)
        self.trade_service.process_signal(signal, payout, entry_delay, log_ctx, market_type)

    def _build_log_context(self, asset: str, payout: float, market_type: str, result,
                           market_meta=None) -> TradeLogContext:
        """Empaqueta el contexto satélite. NO influye en la decisión."""
        normalized = asset.upper()
        api_context = {
            "asset": asset,
            "normalized_asset": normalized,
            "market_type": market_type,      # turbo | binary | digital | unavailable (real, no fijo)
            "asset_exists": 1,               # llegó tras el filtro de existencia del controlador
            "asset_open": 1,                 # idem: solo se escanean activos abiertos
            "payout": payout if payout > 0 else None,
            "payout_source": market_type if payout > 0 else "unavailable",
            "api_latency_candles_ms": result.candle_fetch_ms,
            "connection_ok": 1,
        }
        # Frescura/latencia de las cachés de mercado (auditoría: ¿payout rancio?).
        if market_meta is not None:
            try:
                api_context.update(market_meta())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{asset}] market_meta no disponible: {exc}")
        return TradeLogContext(
            timeframe_seconds=self.signal_service.repo.timeframe_seconds,
            market_snapshot=result.market_snapshot,
            candle_rows=result.candle_rows,
            api_context=api_context,
        )
