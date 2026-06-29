"""Diagnóstico de payouts: turbo/binary vs digital, por activo.

USO (con el bot DETENIDO para no chocar la sesión):
    .venv/bin/python -m tools.probe_digital
    .venv/bin/python -m tools.probe_digital EURUSD EURJPY GBPUSD

Solo lee: NO compra nada. Conecta en PRACTICE, y para cada activo imprime:
  - si existe en el catálogo (ACTIVES_OPCODE)
  - payout turbo/binary (get_all_profit)
  - payout digital (get_digital_payout, ventana 8s)

Así confirmas si IQ Option ofrece opciones (de cualquier tipo) en esos pares.
"""
from __future__ import annotations

import sys

from config.settings import settings
from infrastructure.iqoption_client import IQOptionClient


def main(assets: list[str]) -> None:
    client = IQOptionClient(settings.iq_email, settings.iq_password, allow_real=False)
    client.connect()
    client.ensure_practice()

    profits = client.get_profits()
    opcode = client.get_actives_opcode()

    if not assets:
        assets = settings.assets

    print(f"\n{'ACTIVO':<12} {'existe':<7} {'turbo':<8} {'binary':<8} {'digital':<8}  diagnóstico")
    print("-" * 72)
    for asset in assets:
        asset = asset.strip().upper()
        info = profits.get(asset, {}) or {}
        turbo = info.get("turbo")
        binary = info.get("binary")
        exists = "sí" if asset in opcode else "NO"

        # Digital: suscripción en vivo (ventana 8s). Puede tardar.
        digital = client.get_digital_payout(asset, timeout=8.0)

        def fmt(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return "-"
            v = v / 100 if v > 1 else v
            return f"{v:.2f}" if v > 0 else "0"

        any_payout = max(
            float(turbo or 0), float(binary or 0), float(digital or 0)
        ) > 0
        diag = "OPERABLE" if any_payout else "sin opciones (solo gráfico)"
        print(f"{asset:<12} {exists:<7} {fmt(turbo):<8} {fmt(binary):<8} {fmt(digital):<8}  {diag}")

    print("\nNota: 'digital' usa una suscripción websocket de 8s por activo; si todo\n"
          "sale 0, IQ Option no ofrece binarias/digitales en esos pares para tu cuenta ahora.\n")


if __name__ == "__main__":
    main(sys.argv[1:])
