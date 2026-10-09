from __future__ import annotations

from typing import Any

import pandas as pd

from ..indicators import rsi, sma
from .base import Strategy, signals_frame


class RsiReversion(Strategy):
    family = "rsi_reversion"

    def generate(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        close = df["close"]
        r = rsi(close, params["rsi_period"])

        long_entry = r < params["rsi_buy"]
        long_exit = r > params["rsi_exit"]
        short_entry = r > (100.0 - params["rsi_buy"])
        short_exit = r < (100.0 - params["rsi_exit"])

        if params.get("use_sma_filter"):
            trend = close > sma(close, params["sma_filter_period"])
            long_entry = long_entry & trend
            short_entry = short_entry & ~trend

        return signals_frame(
            df.index,
            long_entry=long_entry,
            long_exit=long_exit,
            short_entry=short_entry,
            short_exit=short_exit,
        )
