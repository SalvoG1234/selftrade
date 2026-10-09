"""Strategy base class.

A strategy turns an OHLCV frame plus a genome's ``params`` into four boolean
signal series, decided **at the close of each bar**:

  ``long_entry`` / ``long_exit`` / ``short_entry`` / ``short_exit``

The backtester is responsible for filling at the next bar's open and for all
risk management (stop / take-profit / trailing); strategies never look ahead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

SIGNAL_COLUMNS = ["long_entry", "long_exit", "short_entry", "short_exit"]


def signals_frame(
    index: pd.Index,
    *,
    long_entry: pd.Series | None = None,
    long_exit: pd.Series | None = None,
    short_entry: pd.Series | None = None,
    short_exit: pd.Series | None = None,
) -> pd.DataFrame:
    def norm(series: pd.Series | None) -> pd.Series:
        if series is None:
            return pd.Series(False, index=index, dtype="bool")
        s = series.reindex(index)
        s = s.where(s.notna(), False)
        return s.astype(bool)

    return pd.DataFrame(
        {
            "long_entry": norm(long_entry),
            "long_exit": norm(long_exit),
            "short_entry": norm(short_entry),
            "short_exit": norm(short_exit),
        },
        index=index,
    )


class Strategy(ABC):
    family: str = ""

    @abstractmethod
    def generate(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        """Return a :data:`SIGNAL_COLUMNS` boolean frame aligned to ``df``."""
        raise NotImplementedError
