"""Selección automática de activos de MERCADO REAL (no-OTC).

Módulo de dominio: funciones PURAS (sin I/O, sin llamadas a IQ Option) que
deciden qué activos operar a partir de dos snapshots ya obtenidos por la capa
de infraestructura:

  - `open_times`: {"turbo": {"EURUSD": {"open": True}, ...}, "binary": {...}}
    (misma forma que devuelve IQOptionClient.get_open_times() /
    stable_api.get_all_open_time() para las categorías turbo/binary).
  - `profits`: {"EURUSD": {"turbo": 0.85, "binary": 0.80}, ...}
    (misma forma que IQOptionClient.get_profits() / stable_api.get_all_profit(),
    ya normalizado a fracción 0-1).

Al ser puras y sin dependencias de infraestructura, se pueden testear con
diccionarios simulados (ver tools/test_asset_selection.py o el test unitario
del cambio 2026-07-10).
"""
from __future__ import annotations

from dataclasses import dataclass

# Nota: aunque el snapshot de entrada trae también "binary", la selección solo
# usa "turbo" (ver docstring de select_real_assets): binary = expiraciones
# largas, no ejecutables con la expiración 1-5 min de este bot.


def is_otc_name(asset: str) -> bool:
    """True si el nombre del activo es OTC.

    Replica infrastructure.trade_executor.is_otc_asset() (mismo criterio:
    sufijo "-OTC" o "OTC") y añade, defensivamente, el sufijo "-OP" por si
    algún catálogo de IQ marca así una variante OTC/con comisión especial que
    no pasó por IQOptionClient._clean_active_name (que ya limpia "-op" en
    circunstancias normales). El dominio no importa infraestructura, así que
    el criterio se duplica aquí a propósito (capas separadas).
    """
    a = (asset or "").upper()
    return a.endswith("-OTC") or a.endswith("OTC") or a.endswith("-OP")


@dataclass(frozen=True)
class RankedAsset:
    asset: str
    payout: float          # fracción 0-1, el mejor payout disponible del activo
    market_type: str       # "turbo" | "binary" (de dónde salió el payout usado)
    is_otc: bool


def _payout_for(name: str, kind: str, profits: dict) -> float:
    """Payout (fracción 0-1) de `name` en la categoría `kind`, con fallback
    cruzado turbo<->binary si esa categoría concreta no trae payout."""
    info = profits.get(name) or {}
    val = info.get(kind)
    if val is None:
        val = info.get("turbo") if kind != "turbo" else info.get("binary")
    try:
        val = float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if val <= 0:
        return 0.0
    # Defensivo: algunas versiones de la API devuelven porcentaje (85) en vez
    # de fracción (0.85), igual que IQOptionClient.payout_from().
    return val / 100 if val > 1 else val


def select_real_assets(
    open_times: dict,
    profits: dict,
    *,
    min_payout: float,
    max_assets: int,
    include_otc: bool = False,
    universe: set[str] | None = None,
) -> list[RankedAsset]:
    """Rankea y filtra activos con TURBO ABIERTO por payout descendente.

    Reglas:
      1. SOLO activos abiertos en la categoría "turbo". Motivo: el bot opera
         expiraciones de 1-5 min y en IQ Option eso se ejecuta como opción
         turbo; "binary" son expiraciones largas (15 min+), así que un activo
         abierto solo-binary NUNCA es ejecutable para este bot y no debe
         ocupar un slot de max_assets. (Digital tampoco se auto-selecciona:
         su disponibilidad/payout solo puede consultarse por-activo y es
         lenta en la Pi; sigue existiendo como fallback de ejecución
         por-activo vía allow_digital.)
      2. Excluye OTC salvo que `include_otc=True` (lo usa el controlador
         SOLO cuando `otc_fallback` está activo y no hay ningún real abierto).
      3. Si `universe` se pasa (nombres permitidos, p.ej. forex+metales+
         índices), restringe a esos nombres. Si es None, no restringe: hoy
         el snapshot turbo/binary no trae metadatos de clase de activo, así
         que no hay con qué distinguir forex/metal/índice de forma fiable.
      4. Payout: preferentemente el de turbo; si falta, se usa el de binary
         solo como ESTIMACIÓN del payout (el instrumento sigue siendo turbo).
         Se descarta si es menor que `min_payout`.
      5. Ordena por payout desc y, a igualdad, alfabético (determinista).
      6. Recorta a los primeros `max_assets` (mínimo 1).

    Devuelve una lista de RankedAsset (vacía si no hay nada que cumpla).
    """
    candidates: dict[str, RankedAsset] = {}
    bucket = open_times.get("turbo") or {}
    for name, info in bucket.items():
        if not isinstance(info, dict) or not info.get("open"):
            continue
        otc = is_otc_name(name)
        if otc and not include_otc:
            continue
        if universe is not None and name not in universe:
            continue
        payout = _payout_for(name, "turbo", profits)
        if payout < min_payout:
            continue
        candidates[name] = RankedAsset(
            asset=name, payout=payout, market_type="turbo", is_otc=otc,
        )

    ranked = sorted(candidates.values(), key=lambda r: (-r.payout, r.asset))
    cap = max(1, int(max_assets or 1))
    return ranked[:cap]


def diff_selection(previous: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """(entraron, salieron) entre dos selecciones, para loguear la rotación."""
    prev_set, new_set = set(previous), set(new)
    entered = [a for a in new if a not in prev_set]
    left = [a for a in previous if a not in new_set]
    return entered, left
