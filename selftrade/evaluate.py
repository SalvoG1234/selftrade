"""Walk-forward evaluation, fitness, hard gates and the holdout guard.

Fitness (documented in docs/ARCHITECTURE.md)::

    fitness = mean(test_metric over folds)
              - complexity_penalty * complexity(genome)
              - INSTABILITY_WEIGHT * std(test_metric over folds)

The metric is always measured on the **test** slice of each fold, never on the
train slice.  The holdout tail of the series is never used during evolution; it
is read exactly once, at the end of a run, through :class:`Holdout`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .backtest import BacktestResult, compute_metrics, simulate
from .genome import complexity, validate_genome

INSTABILITY_WEIGHT = 0.5


class HoldoutError(Exception):
    """Raised when the holdout is read more than once per run."""


@dataclass
class Holdout:
    """The untouchable tail of the series.  Readable exactly once."""

    df: pd.DataFrame
    _reads: int = 0
    _read: bool = False

    def read(self) -> pd.DataFrame:
        if self._read:
            raise HoldoutError(
                "holdout already read once in this run; it must not be reused"
            )
        self._read = True
        self._reads += 1
        return self.df

    @property
    def reads(self) -> int:
        return self._reads

    @property
    def consumed(self) -> bool:
        return self._read


def metric_value(metrics: dict[str, Any], name: str) -> float:
    """Extract a finite metric value (profit_factor inf is capped)."""
    v = float(metrics.get(name, 0.0))
    if not math.isfinite(v):
        # Cap infinite profit factor so it cannot dominate the fitness.
        return 10.0
    return v


def make_folds(
    dev: pd.DataFrame, folds: int, train_frac: float
) -> list[dict[str, Any]]:
    """Split ``dev`` into ``folds`` consecutive chunks.

    Each fold's chunk has its first ``train_frac`` as development context and
    the remainder as the (subsequent) test window.
    """
    n = len(dev)
    folds = max(1, int(folds))
    if n < folds * 4:
        folds = max(1, n // 4)
    L = max(1, n // folds)
    out: list[dict[str, Any]] = []
    for k in range(folds):
        start = k * L
        end = n if k == folds - 1 else (k + 1) * L
        chunk = dev.iloc[start:end]
        cut = int(round(len(chunk) * train_frac))
        cut = max(1, min(cut, len(chunk) - 1))
        out.append({"train": chunk.iloc[:cut], "test": chunk.iloc[cut:], "chunk": chunk})
    return out


def _test_slice_metrics(
    res: BacktestResult, test_start, initial_cash: float, timeframe: str
) -> dict[str, float]:
    eq = res.equity.loc[res.equity.index >= test_start]
    if len(eq) == 0:
        return compute_metrics(res.equity.iloc[0:0], res.trades.iloc[0:0], initial_cash, timeframe)
    base = float(eq.iloc[0])
    trades = res.trades[res.trades["entry_time"] >= test_start] if len(res.trades) else res.trades
    return compute_metrics(eq, trades, base if base > 0 else initial_cash, timeframe)


def evaluate_folds(
    dev: pd.DataFrame,
    genome: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Walk-forward evaluation of ``genome`` on the development series."""
    g = validate_genome(genome)
    ev = settings.evaluation
    bt = settings.backtest
    timeframe = settings.data.timeframe

    folds = make_folds(dev, ev.folds, ev.train_frac)
    fold_metrics: list[dict[str, float]] = []
    for f in folds:
        chunk = f["chunk"]
        res = simulate(
            chunk,
            g,
            initial_cash=bt.initial_cash,
            fee_bps=bt.fee_bps,
            slippage_bps=bt.slippage_bps,
            position_size=bt.position_size,
            allow_short=bt.allow_short,
            timeframe=timeframe,
        )
        m = _test_slice_metrics(res, f["test"].index[0], bt.initial_cash, timeframe)
        fold_metrics.append(m)

    values = [metric_value(m, ev.metric) for m in fold_metrics]
    metric_mean = float(np.mean(values)) if values else 0.0
    metric_std = float(np.std(values, ddof=0)) if len(values) > 1 else 0.0
    n_trades_total = int(sum(int(m["n_trades"]) for m in fold_metrics))
    worst_dd = max((float(m["max_drawdown"]) for m in fold_metrics), default=0.0)
    comp = complexity(g)

    fitness = metric_mean - float(ev.complexity_penalty) * comp - INSTABILITY_WEIGHT * metric_std

    reasons: list[str] = []
    if n_trades_total < int(ev.min_trades):
        reasons.append(f"n_trades={n_trades_total} < min_trades={ev.min_trades}")
    if worst_dd > float(ev.max_drawdown):
        reasons.append(f"max_drawdown={worst_dd:.3f} > {ev.max_drawdown:.3f}")
    passed = not reasons

    return {
        "family": g["family"],
        "metric": ev.metric,
        "folds": fold_metrics,
        "fold_values": values,
        "metric_mean": metric_mean,
        "metric_std": metric_std,
        "n_trades": n_trades_total,
        "max_drawdown": worst_dd,
        "complexity": comp,
        "fitness": fitness,
        "passed": passed,
        "gate_reason": "" if passed else "; ".join(reasons),
    }


def holdout_eval(
    holdout_df: pd.DataFrame,
    genome: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Full-period metrics on the holdout slice (called once per run)."""
    bt = settings.backtest
    res = simulate(
        holdout_df,
        validate_genome(genome),
        initial_cash=bt.initial_cash,
        fee_bps=bt.fee_bps,
        slippage_bps=bt.slippage_bps,
        position_size=bt.position_size,
        allow_short=bt.allow_short,
        timeframe=settings.data.timeframe,
    )
    return {
        "metrics": res.metrics,
        "n_trades": int(res.metrics.get("n_trades", 0)),
        # Kept so the autopsy and the critic can look at the same single run
        # instead of evaluating the holdout a second time.
        "equity": res.equity,
        "trades": res.trades,
    }


def regime_luck_warning(
    fold_values: list[float], holdout_value: float
) -> str | None:
    """Warn when the holdout is much better than the average fold."""
    if not fold_values:
        return None
    mean = float(np.mean(fold_values))
    std = float(np.std(fold_values, ddof=0))
    if mean <= 0 and holdout_value > 0:
        return (
            f"holdout {holdout_value:.3f} is positive while the fold mean is {mean:.3f}: "
            "possible regime luck"
        )
    if std > 1e-9 and holdout_value > mean + 2.0 * std:
        return (
            f"holdout {holdout_value:.3f} exceeds fold mean {mean:.3f} by >2 std "
            f"({std:.3f}): possible regime luck"
        )
    return None
