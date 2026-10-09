from __future__ import annotations

from typing import Any

import pandas as pd

from ..indicators import atr, ema, rsi
from .base import Strategy, signals_frame


class TrendMomentum(Strategy):
    family = "trend_momentum"

    def generate(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        close = df["close"]
        fast = ema(close, params["ema_fast"])
        slow = ema(close, params["ema_slow"])
        r = rsi(close, params["rsi_period"])

        up = (fast > slow) & (r > params["rsi_min"])
        down = (fast < slow) & (r < (100.0 - params["rsi_min"]))

        if params.get("use_atr_filter"):
            # Skip entries in abnormally volatile regimes (causal rolling median).
            ratio = atr(df, params["atr_period"]) / close
            ok = ratio <= ratio.rolling(100, min_periods=20).median()
            up = up & ok
            down = down & ok

        return signals_frame(
            df.index,
            long_entry=up,
            long_exit=~up,
            short_entry=down,
            short_exit=~down,
        )
