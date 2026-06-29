"""Convierte las velas crudas de iqoptionapi en un DataFrame limpio.

Mapea las claves de la librería (min/max) a las que usa la estrategia
(low/high) y descarta la vela en formación.
"""
from __future__ import annotations

import time

import pandas as pd

from infrastructure.iqoption_client import IQOptionClient


class CandleRepository:
    def __init__(self, client: IQOptionClient, timeframe_seconds: int):
        self.client = client
        self.timeframe_seconds = timeframe_seconds

    def get_dataframe(self, asset: str, count: int) -> pd.DataFrame:
        raw = self.client.get_candles(asset, self.timeframe_seconds, count)
        if not raw:
            return pd.DataFrame()

        rows = []
        for c in raw:
            rows.append({
                "timestamp": c.get("from"),
                "open": c.get("open"),
                "close": c.get("close"),
                # La librería usa min/max; mapear a low/high.
                "low": c.get("min", c.get("low")),
                "high": c.get("max", c.get("high")),
                "volume": c.get("volume", 0),
            })

        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

        # Descartar la vela en formación (su periodo aún no terminó).
        now = time.time()
        if not df.empty:
            last_close_ts = df.iloc[-1]["timestamp"] + self.timeframe_seconds
            if last_close_ts > now:
                df = df.iloc[:-1].reset_index(drop=True)

        return df
