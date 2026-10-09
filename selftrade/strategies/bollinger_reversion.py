from __future__ import annotations

from typing import Any

import pandas as pd

from ..indicators import bollinger, rsi
from .base import Strategy, signals_frame


class BollingerReversion(Strategy):
    family = "bollinger_reversion"

    def generate(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        close = df["close"]
        lower, mid, upper = bollinger(close, params["bb_period"], params["bb_std"])

        long_entry = close < lower
        short_entry = close > upper

        if params.get("exit_at_mid"):
            long_exit = close >= mid
            short_exit = close <= mid
        else:
            long_exit = close > upper
            short_exit = close < lower

        if params.get("use_rsi_filter"):
            r = rsi(close, params["rsi_period"])
            long_entry = long_entry & (r < 50.0)
            short_entry = short_entry & (r > 50.0)

        return signals_frame(
            df.index,
            long_entry=long_entry,
            long_exit=long_exit,
            short_entry=short_entry,
            short_exit=short_exit,
        )
