from __future__ import annotations

from typing import Any

import pandas as pd

from ..indicators import atr, donchian
from .base import Strategy, signals_frame


class DonchianBreakout(Strategy):
    family = "donchian_breakout"

    def generate(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        close = df["close"]
        # Prior-bar channel: a close above the *previous* N-bar high is a fresh breakout.
        _, _, upper = donchian(df, params["entry_period"])
        lower_exit, _, _ = donchian(df, params["exit_period"])
        upper_prev = upper.shift(1)
        lower_exit_prev = lower_exit.shift(1)

        long_entry = close > upper_prev
        long_exit = close < lower_exit_prev
        short_entry = close < lower_exit_prev
        short_exit = close > upper_prev

        if params.get("use_atr_filter"):
            ratio = atr(df, params["atr_period"]) / close
            ok = ratio <= ratio.rolling(100, min_periods=20).median()
            long_entry = long_entry & ok
            short_entry = short_entry & ok

        return signals_frame(
            df.index,
            long_entry=long_entry,
            long_exit=long_exit,
            short_entry=short_entry,
            short_exit=short_exit,
        )
