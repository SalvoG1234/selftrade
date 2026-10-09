"""Bar-by-bar backtester.

Exact mechanics (see docs/ARCHITECTURE.md):

* a signal is computed at the **close** of bar ``i`` and filled at the
  **open** of bar ``i + 1``;
* costs (``fee_bps + slippage_bps``) are applied **on every side**, against you;
* stop / take-profit / trailing are evaluated **intrabar**, with the **stop
  firing before the target** when both are touched in the same bar;
* a gap through the stop fills at the (worse) open, not at the stop level;
* position size is a fraction ``position_size`` of current equity;
* shorts require **both** ``backtest.allow_short`` and ``risk.trade_shorts``;
* a final open position is marked ``end_of_data`` and is **not** a closed trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .data import TIMEFRAME_BARS_PER_YEAR
from .genome import validate_genome
from .strategies import generate_signals


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict[str, float]
    signals: pd.DataFrame | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": dict(self.metrics),
            "n_trades": int(self.metrics.get("n_trades", 0)),
            "equity_points": int(len(self.equity)),
        }


TRADE_COLUMNS = [
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "size",
    "pnl",
    "return_pct",
    "bars_held",
    "exit_reason",
]


def _empty_equity(df: pd.DataFrame, initial_cash: float) -> pd.Series:
    return pd.Series(initial_cash, index=df.index, dtype="float64")


def simulate(
    df: pd.DataFrame,
    genome: dict[str, Any],
    *,
    initial_cash: float = 10000.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    position_size: float = 0.95,
    allow_short: bool = False,
    timeframe: str = "1h",
    signals: pd.DataFrame | None = None,
) -> BacktestResult:
    """Run the engine over ``df`` and return equity, trades and metrics.

    Pass ``signals`` to drive the engine directly (used by tests and by the
    freqtrade parity test); otherwise they are generated from the genome.
    """
    if len(df) < 2:
        return BacktestResult(
            _empty_equity(df, initial_cash),
            pd.DataFrame(columns=TRADE_COLUMNS),
            _flat_metrics(initial_cash),
            None,
        )

    g = validate_genome(genome)
    if signals is None:
        signals = generate_signals(g, df)
    else:
        signals = signals.reindex(df.index).fillna(False).astype(bool)

    n = len(df)
    op = df["open"].to_numpy(dtype="float64")
    hi = df["high"].to_numpy(dtype="float64")
    lo = df["low"].to_numpy(dtype="float64")
    cl = df["close"].to_numpy(dtype="float64")
    times = df.index

    long_entry = signals["long_entry"].to_numpy()
    long_exit = signals["long_exit"].to_numpy()
    short_entry = signals["short_entry"].to_numpy()
    short_exit = signals["short_exit"].to_numpy()

    risk = g["risk"]
    sl_pct = float(risk["stop_loss_pct"])  # negative
    tp_pct = risk["take_profit_pct"]
    trail_pct = risk["trailing_pct"]
    shorts_ok = bool(allow_short and risk["trade_shorts"])

    cost = (float(fee_bps) + float(slippage_bps)) / 10000.0

    equity = np.empty(n, dtype="float64")
    equity_value = float(initial_cash)

    # Open position state
    pos: dict[str, Any] | None = None
    pending_dir: str | None = None
    pending_exit = False
    trades: list[dict[str, Any]] = []

    for i in range(n):
        # --- 1. Fill a pending entry at this bar's open -----------------
        if pos is None and pending_dir is not None:
            raw = op[i]
            if pending_dir == "long":
                eff = raw * (1.0 + cost)
                stop = eff * (1.0 + sl_pct)
                target = None if tp_pct is None else eff * (1.0 + float(tp_pct))
            else:
                eff = raw * (1.0 - cost)
                stop = eff * (1.0 - sl_pct)  # sl_pct negative -> stop above entry
                target = None if tp_pct is None else eff * (1.0 - float(tp_pct))
            size = (position_size * equity_value) / eff
            pos = {
                "dir": pending_dir,
                "entry_i": i,
                "entry_time": times[i],
                "entry_eff": eff,
                "entry_raw": raw,
                "size": size,
                "stop": stop,
                "target": target,
                "trail_ref": eff,
                "equity_before": equity_value,
            }
            pending_dir = None

        # --- 2. Exit logic ---------------------------------------------
        if pos is not None:
            exited = False
            raw_exit = None
            reason = None

            if pending_exit:
                exited = True
                raw_exit = op[i]
                reason = "signal"
            else:
                is_long = pos["dir"] == "long"
                stop_level = pos["stop"]
                if trail_pct is not None:
                    if is_long:
                        trail_level = pos["trail_ref"] * (1.0 - float(trail_pct))
                        stop_level = max(stop_level, trail_level)
                    else:
                        trail_level = pos["trail_ref"] * (1.0 + float(trail_pct))
                        stop_level = min(stop_level, trail_level)

                if is_long:
                    # Gap through the stop fills at the worse open.
                    if op[i] <= stop_level:
                        exited, raw_exit, reason = True, op[i], "stop"
                    elif lo[i] <= stop_level:
                        exited, raw_exit, reason = True, stop_level, "stop"
                    elif pos["target"] is not None:
                        tgt = pos["target"]
                        if op[i] >= tgt:
                            exited, raw_exit, reason = True, op[i], "target"
                        elif hi[i] >= tgt:
                            exited, raw_exit, reason = True, tgt, "target"
                else:
                    if op[i] >= stop_level:
                        exited, raw_exit, reason = True, op[i], "stop"
                    elif hi[i] >= stop_level:
                        exited, raw_exit, reason = True, stop_level, "stop"
                    elif pos["target"] is not None:
                        tgt = pos["target"]
                        if op[i] <= tgt:
                            exited, raw_exit, reason = True, op[i], "target"
                        elif lo[i] <= tgt:
                            exited, raw_exit, reason = True, tgt, "target"

            if exited:
                is_long = pos["dir"] == "long"
                exit_eff = raw_exit * (1.0 - cost) if is_long else raw_exit * (1.0 + cost)
                pnl = (
                    pos["size"] * (exit_eff - pos["entry_eff"])
                    if is_long
                    else pos["size"] * (pos["entry_eff"] - exit_eff)
                )
                equity_value = pos["equity_before"] + pnl
                trades.append(
                    {
                        "direction": pos["dir"],
                        "entry_time": pos["entry_time"],
                        "exit_time": times[i],
                        "entry_price": pos["entry_eff"],
                        "exit_price": exit_eff,
                        "size": pos["size"],
                        "pnl": pnl,
                        "return_pct": pnl / pos["equity_before"] if pos["equity_before"] else 0.0,
                        "bars_held": i - pos["entry_i"],
                        "exit_reason": reason,
                    }
                )
                pos = None

        # --- 3. Mark to market -----------------------------------------
        if pos is not None:
            is_long = pos["dir"] == "long"
            if is_long:
                equity[i] = pos["equity_before"] + pos["size"] * (cl[i] - pos["entry_eff"])
                if trail_pct is not None:
                    pos["trail_ref"] = max(pos["trail_ref"], hi[i])
            else:
                equity[i] = pos["equity_before"] + pos["size"] * (pos["entry_eff"] - cl[i])
                if trail_pct is not None:
                    pos["trail_ref"] = min(pos["trail_ref"], lo[i])
            equity_value = equity[i]
        else:
            equity[i] = equity_value

        # --- 4. Signals at this close decide the next bar --------------
        pending_dir = None
        pending_exit = False
        if pos is None:
            if long_entry[i]:
                pending_dir = "long"
            elif shorts_ok and short_entry[i]:
                pending_dir = "short"
        else:
            if pos["dir"] == "long":
                pending_exit = bool(long_exit[i])
            else:
                pending_exit = bool(short_exit[i])

    # --- Close out any position still open at the last bar -------------
    exit_reason_final = None
    if pos is not None:
        is_long = pos["dir"] == "long"
        exit_eff = cl[-1] * (1.0 - cost) if is_long else cl[-1] * (1.0 + cost)
        pnl = (
            pos["size"] * (exit_eff - pos["entry_eff"])
            if is_long
            else pos["size"] * (pos["entry_eff"] - exit_eff)
        )
        trades.append(
            {
                "direction": pos["dir"],
                "entry_time": pos["entry_time"],
                "exit_time": times[-1],
                "entry_price": pos["entry_eff"],
                "exit_price": exit_eff,
                "size": pos["size"],
                "pnl": pnl,
                "return_pct": pnl / pos["equity_before"] if pos["equity_before"] else 0.0,
                "bars_held": n - 1 - pos["entry_i"],
                "exit_reason": "end_of_data",
            }
        )
        exit_reason_final = "end_of_data"
        pos = None

    equity_series = pd.Series(equity, index=df.index, dtype="float64")
    trades_df = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    metrics = compute_metrics(equity_series, trades_df, initial_cash, timeframe)
    if exit_reason_final:
        metrics["open_at_end"] = 1.0
    return BacktestResult(equity_series, trades_df, metrics, signals)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _flat_metrics(initial_cash: float) -> dict[str, float]:
    return {
        "total_return": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "calmar": 0.0,
        "max_drawdown": 0.0,
        "n_trades": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_trade_return": 0.0,
        "final_equity": float(initial_cash),
    }


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    if len(equity) == 0:
        return 0.0
    dd = drawdown_series(equity)
    return float(max(0.0, -dd.min()))


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    initial_cash: float,
    timeframe: str,
) -> dict[str, float]:
    bpy = float(TIMEFRAME_BARS_PER_YEAR.get(timeframe, 8760.0))
    metrics = _flat_metrics(initial_cash)
    n = len(equity)
    if n == 0:
        return metrics

    final = float(equity.iloc[-1])
    metrics["final_equity"] = final
    metrics["total_return"] = final / initial_cash - 1.0

    rets = equity.pct_change().dropna()
    std = float(rets.std(ddof=0)) if len(rets) else 0.0
    mean = float(rets.mean()) if len(rets) else 0.0
    metrics["sharpe"] = (mean / std * np.sqrt(bpy)) if std > 1e-12 else 0.0

    downside = rets[rets < 0]
    dstd = float(downside.std(ddof=0)) if len(downside) else 0.0
    metrics["sortino"] = (mean / dstd * np.sqrt(bpy)) if dstd > 1e-12 else 0.0

    mdd = max_drawdown(equity)
    metrics["max_drawdown"] = mdd
    years = n / bpy if bpy else 0.0
    if years > 0 and final > 0:
        annual = (final / initial_cash) ** (1.0 / years) - 1.0
    else:
        annual = 0.0
    metrics["calmar"] = (annual / mdd) if mdd > 1e-9 else 0.0

    closed = trades[trades["exit_reason"] != "end_of_data"] if len(trades) else trades
    metrics["n_trades"] = float(len(closed))
    if len(closed):
        wins = closed[closed["pnl"] > 0]
        losses = closed[closed["pnl"] <= 0]
        metrics["win_rate"] = float(len(wins) / len(closed))
        gross_win = float(wins["pnl"].sum())
        gross_loss = float(-losses["pnl"].sum())
        metrics["profit_factor"] = (
            (gross_win / gross_loss) if gross_loss > 1e-9 else (float("inf") if gross_win > 0 else 0.0)
        )
        metrics["avg_trade_return"] = float(closed["return_pct"].mean())
    return metrics


def trade_return_std(trades: pd.DataFrame) -> float:
    closed = trades[trades["exit_reason"] != "end_of_data"] if len(trades) else trades
    if len(closed) < 2:
        return 0.0
    return float(closed["return_pct"].std(ddof=0))
