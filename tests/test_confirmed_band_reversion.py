from __future__ import annotations

import unittest

import pandas as pd

from config.epoch import STRATEGY_REVISION, compute_config_epoch
from domain.models import Direction
from domain.strategy import ConfirmedBandReversionStrategy


class ConfirmedBandReversionStrategyTest(unittest.TestCase):
    @staticmethod
    def frame(direction: str) -> pd.DataFrame:
        common = {
            "bb_width": 0.04,
            "bb_width_median": 0.035,
            "ema_distance": 0.2,
            "avg_range_20": 1.0,
            "atr": 1.0,
            "adx": 20.0,
            "range_atr_ratio": 1.5,
            "body_ratio": 0.4,
        }
        if direction == "call":
            rows = [
                {
                    **common,
                    "timestamp": 1_700_000_000,
                    "open": 99.2, "high": 99.5, "low": 98.5, "close": 98.8,
                    "bb_top": 103.0, "bb_bottom": 99.0, "rsi": 30.0,
                    "range": 1.0, "upper_wick_ratio": 0.3, "lower_wick_ratio": 0.3,
                },
                {
                    **common,
                    "timestamp": 1_700_000_060,
                    "open": 98.7, "high": 99.7, "low": 98.2, "close": 99.4,
                    "bb_top": 103.0, "bb_bottom": 99.0, "rsi": 32.0,
                    "range": 1.5, "upper_wick_ratio": 0.2, "lower_wick_ratio": 0.33,
                },
            ]
        else:
            rows = [
                {
                    **common,
                    "timestamp": 1_700_000_000,
                    "open": 100.8, "high": 101.5, "low": 100.5, "close": 101.2,
                    "bb_top": 101.0, "bb_bottom": 97.0, "rsi": 70.0,
                    "range": 1.0, "upper_wick_ratio": 0.3, "lower_wick_ratio": 0.3,
                },
                {
                    **common,
                    "timestamp": 1_700_000_060,
                    "open": 101.3, "high": 101.8, "low": 100.3, "close": 100.6,
                    "bb_top": 101.0, "bb_bottom": 97.0, "rsi": 68.0,
                    "range": 1.5, "upper_wick_ratio": 0.33, "lower_wick_ratio": 0.2,
                },
            ]
        return pd.DataFrame(rows)

    def test_emits_call_only_after_confirmed_lower_band_reentry(self) -> None:
        strategy = ConfirmedBandReversionStrategy()

        signal = strategy.signal_from_frame("EURUSD", self.frame("call"))

        self.assertEqual(Direction.CALL, signal.direction)
        self.assertEqual("confirmed_band_reversion_call", signal.strategy_reason)
        self.assertIn("range_atr=1.500", signal.notes)

    def test_emits_put_only_after_confirmed_upper_band_reentry(self) -> None:
        strategy = ConfirmedBandReversionStrategy()

        signal = strategy.signal_from_frame("EURUSD", self.frame("put"))

        self.assertEqual(Direction.PUT, signal.direction)
        self.assertEqual("confirmed_band_reversion_put", signal.strategy_reason)

    def test_rejects_reversion_during_strong_trend(self) -> None:
        strategy = ConfirmedBandReversionStrategy()
        frame = self.frame("call")
        frame.loc[frame.index[-1], "adx"] = 35.0

        signal = strategy.signal_from_frame("EURUSD", frame)
        evaluation = strategy.evaluation_from_frame("EURUSD", frame, signal)

        self.assertEqual(Direction.NONE, signal.direction)
        self.assertEqual("call:adx_ok", signal.strategy_reason)
        self.assertFalse(evaluation["adx_ok"])
        self.assertEqual("call", evaluation["candidate_direction"])

    def test_config_epoch_identifies_new_strategy_and_parameters(self) -> None:
        config = {"bb_mult": 2.0, "reversion_max_adx": 28.0}
        changed = {**config, "reversion_max_adx": 32.0}

        self.assertEqual("confirmed_band_reversion_v1", STRATEGY_REVISION)
        self.assertNotEqual(compute_config_epoch(config), compute_config_epoch(changed))


if __name__ == "__main__":
    unittest.main()
