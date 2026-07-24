"""Test unitario de la función PURA de selección automática de activos.

No requiere conexión a IQ Option: simula los diccionarios que devolvería
IQOptionClient.get_open_times() / get_profits() (misma forma que
stable_api.get_all_open_time() / get_all_profit() para turbo/binary) y
verifica domain.asset_selection.select_real_assets() / diff_selection().

Ejecutar desde la raíz del proyecto (Mac o Pi):
    python3 tools/test_asset_selection.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Raíz del proyecto = carpeta padre de tools/; se añade a sys.path para poder
# importar domain.asset_selection sin instalar el paquete.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from domain.asset_selection import diff_selection, is_otc_name, select_real_assets  # noqa: E402


def _open_times(**kinds):
    """Azúcar sintáctico: _open_times(turbo={"EURUSD": True, "GBPUSD-OTC": True})."""
    out = {}
    for kind, assets in kinds.items():
        out[kind] = {name: {"open": is_open} for name, is_open in assets.items()}
    return out


class SelectRealAssetsTests(unittest.TestCase):
    def test_ranking_por_payout_desc_y_top_max_assets(self):
        open_times = _open_times(turbo={"EURUSD": True, "GBPUSD": True, "AUDCAD": True})
        profits = {
            "EURUSD": {"turbo": 0.82},
            "GBPUSD": {"turbo": 0.91},
            "AUDCAD": {"turbo": 0.75},
        }
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=2)
        self.assertEqual([r.asset for r in ranked], ["GBPUSD", "EURUSD"])
        self.assertEqual(ranked[0].payout, 0.91)

    def test_excluye_otc_por_defecto(self):
        open_times = _open_times(turbo={"EURUSD": True, "EURUSD-OTC": True, "GBPJPY-op": True})
        profits = {
            "EURUSD": {"turbo": 0.80},
            "EURUSD-OTC": {"turbo": 0.95},  # payout más alto pero es OTC
            "GBPJPY-op": {"turbo": 0.99},   # sufijo -op también excluido
        }
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual([r.asset for r in ranked], ["EURUSD"])

    def test_incluye_otc_solo_si_include_otc(self):
        open_times = _open_times(turbo={"EURUSD-OTC": True})
        profits = {"EURUSD-OTC": {"turbo": 0.90}}
        # Sin include_otc: nada (mercado real vacío).
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual(ranked, [])
        # Con include_otc (otc_fallback activo): sí aparece.
        ranked_fallback = select_real_assets(
            open_times, profits, min_payout=0.0, max_assets=8, include_otc=True,
        )
        self.assertEqual([r.asset for r in ranked_fallback], ["EURUSD-OTC"])
        self.assertTrue(ranked_fallback[0].is_otc)

    def test_filtra_por_min_payout(self):
        open_times = _open_times(turbo={"EURUSD": True, "AUDCAD": True})
        profits = {"EURUSD": {"turbo": 0.60}, "AUDCAD": {"turbo": 0.85}}
        ranked = select_real_assets(open_times, profits, min_payout=0.80, max_assets=8)
        self.assertEqual([r.asset for r in ranked], ["AUDCAD"])

    def test_ignora_activos_cerrados(self):
        open_times = _open_times(turbo={"EURUSD": True, "AUDCAD": False})
        profits = {"EURUSD": {"turbo": 0.85}, "AUDCAD": {"turbo": 0.99}}
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual([r.asset for r in ranked], ["EURUSD"])

    def test_sin_activos_abiertos_devuelve_lista_vacia(self):
        open_times = _open_times(turbo={"EURUSD": False}, binary={"GBPUSD": False})
        profits = {"EURUSD": {"turbo": 0.85}, "GBPUSD": {"binary": 0.85}}
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual(ranked, [])

    def test_turbo_manda_aunque_binary_pague_mas(self):
        # El instrumento ejecutable a 1-5 min es turbo: su payout es el que
        # rankea, aunque binary pague mas (semantica 2026-07-10).
        open_times = _open_times(turbo={"EURUSD": True}, binary={"EURUSD": True})
        profits = {"EURUSD": {"turbo": 0.70, "binary": 0.88}}
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].payout, 0.70)
        self.assertEqual(ranked[0].market_type, "turbo")

    def test_solo_binary_queda_excluido(self):
        # Un activo abierto SOLO en binary (expiraciones largas) no es
        # ejecutable a 5 min: no debe ocupar slot de max_assets.
        open_times = _open_times(turbo={"EURUSD": True}, binary={"GBPJPY": True})
        profits = {"EURUSD": {"turbo": 0.84}, "GBPJPY": {"binary": 0.92}}
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual([r.asset for r in ranked], ["EURUSD"])

    def test_payout_binary_solo_como_estimacion_si_turbo_no_trae(self):
        # Turbo abierto pero sin payout turbo publicado: se usa binary como
        # estimacion, el instrumento sigue siendo turbo.
        open_times = _open_times(turbo={"EURUSD": True})
        profits = {"EURUSD": {"binary": 0.80}}
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].payout, 0.80)
        self.assertEqual(ranked[0].market_type, "turbo")

    def test_max_assets_minimo_uno(self):
        open_times = _open_times(turbo={"EURUSD": True})
        profits = {"EURUSD": {"turbo": 0.85}}
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=0)
        self.assertEqual(len(ranked), 1)  # max_assets se fuerza a >= 1

    def test_payout_en_formato_porcentaje_se_normaliza(self):
        open_times = _open_times(turbo={"EURUSD": True})
        profits = {"EURUSD": {"turbo": 85}}  # 85 en vez de 0.85
        ranked = select_real_assets(open_times, profits, min_payout=0.0, max_assets=8)
        self.assertAlmostEqual(ranked[0].payout, 0.85)

    def test_universe_excluye_nombre_promocional_sin_opcode(self):
        open_times = _open_times(turbo={"Bitcoin/Gold": True, "EURUSD": True})
        profits = {
            "Bitcoin/Gold": {"turbo": 0.90},
            "EURUSD": {"turbo": 0.85},
        }
        ranked = select_real_assets(
            open_times,
            profits,
            min_payout=0.0,
            max_assets=8,
            universe={"EURUSD"},
        )
        self.assertEqual([r.asset for r in ranked], ["EURUSD"])


class IsOtcNameTests(unittest.TestCase):
    def test_variantes_otc(self):
        self.assertTrue(is_otc_name("EURUSD-OTC"))
        self.assertTrue(is_otc_name("EURUSDOTC"))
        self.assertTrue(is_otc_name("GBPJPY-op"))
        self.assertFalse(is_otc_name("EURUSD"))
        self.assertFalse(is_otc_name("AUDCAD"))


class DiffSelectionTests(unittest.TestCase):
    def test_entran_y_salen(self):
        entered, left = diff_selection(["EURUSD", "GBPUSD"], ["GBPUSD", "AUDCAD"])
        self.assertEqual(entered, ["AUDCAD"])
        self.assertEqual(left, ["EURUSD"])

    def test_sin_cambios(self):
        entered, left = diff_selection(["EURUSD"], ["EURUSD"])
        self.assertEqual(entered, [])
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
