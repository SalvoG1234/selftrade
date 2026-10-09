"""Parameter spaces for the four strategy families and the risk rules.

Each family declares a mapping ``name -> Param``.  ``genome.py`` uses these to
sample, clamp, mutate and cross genomes, and to compute complexity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass(frozen=True)
class IntParam:
    low: int
    high: int
    default: int
    kind: str = "int"


@dataclass(frozen=True)
class FloatParam:
    low: float
    high: float
    default: float
    kind: str = "float"


@dataclass(frozen=True)
class BoolParam:
    default: bool
    kind: str = "bool"


@dataclass(frozen=True)
class ChoiceParam:
    choices: tuple[str, ...]
    default: str
    kind: str = "choice"


Param = Union[IntParam, FloatParam, BoolParam, ChoiceParam]


FAMILY_ORDER = [
    "trend_momentum",
    "rsi_reversion",
    "donchian_breakout",
    "bollinger_reversion",
]


# Family constraints applied after clamping: a callable that fixes illegal
# combinations so an LLM cannot produce an illegal genome.
FAMILY_PARAMS: dict[str, dict[str, Param]] = {
    "trend_momentum": {
        "ema_fast": IntParam(4, 60, 20),
        "ema_slow": IntParam(20, 250, 60),
        "rsi_period": IntParam(5, 30, 14),
        "rsi_min": FloatParam(45.0, 70.0, 52.0),
        "atr_period": IntParam(5, 30, 14),
        "use_atr_filter": BoolParam(False),
    },
    "rsi_reversion": {
        "rsi_period": IntParam(4, 30, 14),
        "rsi_buy": FloatParam(5.0, 45.0, 30.0),
        "rsi_exit": FloatParam(50.0, 95.0, 60.0),
        "sma_filter_period": IntParam(20, 250, 100),
        "use_sma_filter": BoolParam(False),
    },
    "donchian_breakout": {
        "entry_period": IntParam(10, 150, 24),
        "exit_period": IntParam(4, 80, 10),
        "atr_period": IntParam(5, 30, 14),
        "use_atr_filter": BoolParam(False),
    },
    "bollinger_reversion": {
        "bb_period": IntParam(10, 80, 20),
        "bb_std": FloatParam(1.0, 3.5, 2.0),
        "rsi_period": IntParam(4, 30, 14),
        "exit_at_mid": BoolParam(True),
        "use_rsi_filter": BoolParam(False),
    },
}


# Risk knobs.  stop_loss_pct is always present; tp/trailing may be null.
RISK_STOP_LOW = -0.25
RISK_STOP_HIGH = -0.008
RISK_TARGET_LOW = 0.02
RISK_TARGET_HIGH = 0.35

RISK_DEFAULTS: dict[str, Any] = {
    "stop_loss_pct": -0.05,
    "take_profit_pct": None,
    "trailing_pct": None,
    "trade_shorts": False,
}


def family_params(family: str) -> dict[str, Param]:
    if family not in FAMILY_PARAMS:
        raise KeyError(family)
    return FAMILY_PARAMS[family]


def clamp_param(spec: Param, value: Any) -> Any:
    """Clamp/coerce a single value to its parameter spec."""
    if isinstance(spec, BoolParam):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if isinstance(spec, IntParam):
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            return spec.default
        return max(spec.low, min(spec.high, v))
    if isinstance(spec, FloatParam):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return spec.default
        if v != v:  # NaN
            return spec.default
        return max(spec.low, min(spec.high, v))
    if isinstance(spec, ChoiceParam):
        return value if value in spec.choices else spec.default
    return value  # pragma: no cover
