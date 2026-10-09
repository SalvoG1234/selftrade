"""Strategy families and the registry used by the backtester."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..genome import validate_genome
from .base import SIGNAL_COLUMNS, Strategy, signals_frame
from .bollinger_reversion import BollingerReversion
from .donchian_breakout import DonchianBreakout
from .rsi_reversion import RsiReversion
from .trend_momentum import TrendMomentum

STRATEGIES: dict[str, Strategy] = {
    "trend_momentum": TrendMomentum(),
    "rsi_reversion": RsiReversion(),
    "donchian_breakout": DonchianBreakout(),
    "bollinger_reversion": BollingerReversion(),
}


def get_strategy(family: str) -> Strategy:
    if family not in STRATEGIES:
        raise KeyError(
            f"unknown family {family!r}. Known: {', '.join(sorted(STRATEGIES))}"
        )
    return STRATEGIES[family]


def generate_signals(genome: dict[str, Any], df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``genome`` and generate its signal frame on ``df``."""
    g = validate_genome(genome)
    return get_strategy(g["family"]).generate(df, g["params"])


__all__ = [
    "SIGNAL_COLUMNS",
    "STRATEGIES",
    "Strategy",
    "generate_signals",
    "get_strategy",
    "signals_frame",
]
