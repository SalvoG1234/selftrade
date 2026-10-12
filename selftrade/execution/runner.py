"""The forward session at order level: the research engine's rules, minus the shortcut.

:func:`selftrade.backtest.simulate` walks the bars and books its own fills.  This
module walks the *same* bars and submits orders instead:

* a signal at the close of bar ``i`` becomes an :class:`OrderRequest` that fills at
  the **open of bar ``i + 1``**;
* the size is ``position_size * equity`` at the signal bar, expressed as
  ``quote_qty``, so the broker resolves it at the fill price - precisely the
  ``position_size * equity / effective_price`` the engine computes;
* the exit plan armed on the fill (hard stop, take profit, trailing) is evaluated
  intrabar by the broker with the engine's ladder: a gap through the stop fills at
  the worse open, the stop is checked before the target, and the trailing level
  follows the best price seen since the entry;
* costs are ``fee_bps + slippage_bps`` per side, against you;
* shorts need **both** ``allow_short`` and the genome's ``trade_shorts``;
* a position still open at the last bar is reported as ``end_of_data`` and is not a
  closed trade - the engine's rule, kept for the same reason: it measures nothing.

What is *not* the same is the path.  Every order goes through the pre-trade
:class:`~selftrade.execution.risk_gate.RiskGate` and the kill switch first; a refusal
comes back with its reasons, is recorded as a risk event and is counted in
:attr:`OrderRunResult.blocked` - never silently skipped.  That is what makes the
paper session a rehearsal for the live rail instead of a second simulator.

Equivalence with the engine is asserted by ``tests/test_execution_runner.py``.  The
two ledgers agree to floating-point associativity - the engine adds ``pnl`` to its
running equity, the broker adds and subtracts cash flows - but the *decisions* (trade
times, directions, exit reasons, entry and exit prices) depend only on the prices,
so those are compared exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..backtest import TRADE_COLUMNS, compute_metrics
from ..genome import validate_genome
from ..strategies import generate_signals
from .base import Fill, Order, OrderRequest, OrderStatus, Side
from .paper_broker import PaperBroker


@dataclass
class OrderRunResult:
    """What a session run produced, in the shape the backtester returns."""

    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict[str, float]
    signals: pd.DataFrame | None = None
    orders: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    open_at_end: bool = False
    cash: float = 0.0
    """The broker's cash at the last bar (equity also counts an open position)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": dict(self.metrics),
            "n_trades": int(self.metrics.get("n_trades", 0)),
            "equity_points": int(len(self.equity)),
            "orders": len(self.orders),
            "blocked": len(self.blocked),
            "fills": len(self.fills),
            "open_at_end": bool(self.open_at_end),
        }


def _trade_row(
    open_trade: dict[str, Any],
    *,
    exit_price: float,
    exit_time: Any,
    exit_reason: str,
    bars_held: int,
) -> dict[str, Any]:
    """One closed trade, in the engine's own column order and arithmetic.

    ``pnl`` is ``size * (exit - entry)`` for a long, the mirror for a short: the same
    expression :func:`selftrade.backtest.simulate` uses, so the numbers agree bit for
    bit whenever the sizes do.
    """
    size = float(open_trade["size"])
    entry_eff = float(open_trade["entry_price"])
    is_long = open_trade["direction"] == "long"
    pnl = size * (exit_price - entry_eff) if is_long else size * (entry_eff - exit_price)
    before = float(open_trade["equity_before"])
    return {
        "direction": open_trade["direction"],
        "entry_time": open_trade["entry_time"],
        "exit_time": exit_time,
        "entry_price": entry_eff,
        "exit_price": exit_price,
        "size": size,
        "pnl": pnl,
        "return_pct": pnl / before if before else 0.0,
        "bars_held": int(bars_held),
        "exit_reason": exit_reason,
    }


def run_orders(
    df: pd.DataFrame,
    genome: dict[str, Any],
    *,
    initial_cash: float = 10_000.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    position_size: float = 0.95,
    allow_short: bool = False,
    timeframe: str = "1h",
    signals: pd.DataFrame | None = None,
    symbol: str = "BTC/USDT",
    gate: Any = None,
    kill_switch: Any = None,
    memory: Any = None,
    run_id: int | None = None,
    context: dict[str, Any] | None = None,
    client_prefix: str = "pt",
) -> OrderRunResult:
    """Run ``genome`` over ``df`` by submitting orders, not by simulating bars.

    ``signals`` overrides the genome's own signals (the tests and the freqtrade
    parity check drive the engine that way).  ``gate``/``kill_switch``/``memory`` are
    handed to the broker, so refusals are logged where they happen; ``context`` is
    attached to every audit line (the paper session puts its id there).
    """
    g = validate_genome(genome)
    cost = (float(fee_bps) + float(slippage_bps)) / 10_000.0

    if len(df) < 2:
        equity = pd.Series(float(initial_cash), index=df.index, dtype="float64")
        trades = pd.DataFrame(columns=TRADE_COLUMNS)
        return OrderRunResult(
            equity,
            trades,
            compute_metrics(equity, trades, float(initial_cash), timeframe),
            cash=float(initial_cash),
        )

    if signals is None:
        sig = generate_signals(g, df)
    else:
        sig = signals.reindex(df.index).fillna(False).astype(bool)

    risk = g["risk"]
    sl_pct = float(risk["stop_loss_pct"])  # negative
    tp_pct = risk["take_profit_pct"]
    trail_pct = risk["trailing_pct"]
    shorts_ok = bool(allow_short and risk["trade_shorts"])

    broker = PaperBroker(
        df,
        symbol=symbol,
        cash=float(initial_cash),
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        gate=gate,
        kill_switch=kill_switch,
        memory=memory,
        run_id=run_id,
        context=context,
    )

    n = len(df)
    times = df.index
    long_entry = sig["long_entry"].to_numpy()
    long_exit = sig["long_exit"].to_numpy()
    short_entry = sig["short_entry"].to_numpy()
    short_exit = sig["short_exit"].to_numpy()

    orders: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []

    # The one trade the session is following, and the order that opened it.  One
    # symbol, one position: the gate's `max_positions` and the strategy both say so.
    open_trade: dict[str, Any] | None = None
    entry_order_id: str | None = None
    entry_equity_before = float(initial_cash)
    closed_at_end = False
    fills_seen = 0

    for i in range(n):
        if i:
            broker.advance(1)
            new_fills: list[Fill] = broker.fills_since(fills_seen)
            fills_seen += len(new_fills)
            for fill in new_fills:
                if open_trade is None:
                    if fill.order_id == entry_order_id:
                        open_trade = {
                            "direction": "long" if fill.side is Side.BUY else "short",
                            "entry_side": fill.side,
                            "entry_time": pd.Timestamp(fill.ts),
                            "entry_price": float(fill.price),
                            "size": float(fill.qty),
                            "equity_before": entry_equity_before,
                            "entry_bar": i,
                        }
                    continue
                if fill.side is open_trade["entry_side"]:
                    continue  # a fill that grew the position, not a close
                trades.append(
                    _trade_row(
                        open_trade,
                        exit_price=float(fill.price),
                        exit_time=pd.Timestamp(fill.ts),
                        exit_reason=fill.reason,
                        bars_held=i - int(open_trade["entry_bar"]),
                    )
                )
                open_trade = None
                entry_order_id = None

        # Equity at the close of this bar: the number the engine sizes from.
        equity_now = broker.equity_value()
        positions = broker.get_positions()

        request: OrderRequest | None = None
        intent = ""
        if not positions:
            if bool(long_entry[i]):
                side, intent = Side.BUY, "long"
            elif shorts_ok and bool(short_entry[i]):
                side, intent = Side.SELL, "short"
            else:
                side = None
            if side is not None:
                request = OrderRequest(
                    client_id=f"{client_prefix}-{i}-entry",
                    symbol=symbol,
                    side=side,
                    quote_qty=float(position_size) * equity_now,
                    stop_loss_pct=sl_pct,
                    take_profit_pct=None if tp_pct is None else float(tp_pct),
                    trailing_pct=None if trail_pct is None else float(trail_pct),
                    note=f"{g['family']} {intent} entry at {times[i]}",
                )
        else:
            is_long = positions[0].qty > 0
            flagged = bool(long_exit[i]) if is_long else bool(short_exit[i])
            if flagged:
                intent = "exit"
                request = OrderRequest(
                    client_id=f"{client_prefix}-{i}-exit",
                    symbol=symbol,
                    side=Side.SELL if is_long else Side.BUY,
                    qty=abs(float(positions[0].qty)),
                    note=f"flat at {times[i]}",
                )

        if request is None:
            continue

        order: Order = broker.submit_order(request)
        record = {
            "bar": i,
            "ts": str(times[i]),
            "client_id": request.client_id,
            "intent": intent,
            "side": order.side.value,
            "qty": float(request.qty),
            "quote_qty": request.quote_qty,
            "status": order.status.value,
            "reason": order.reject_reason or None,
            "equity": equity_now,
        }
        orders.append(record)
        if order.status is OrderStatus.REJECTED:
            blocked.append(record)
        elif intent in ("long", "short"):
            # Accepted: it will fill at the next open, so remember what opened it.
            entry_order_id = order.id
            entry_equity_before = equity_now

    if open_trade is not None:
        # Same closing-out rule as the engine: marked at the last close, reported as
        # end_of_data, and kept out of the closed-trade metrics.  The broker is not
        # asked to fill it: the position is still open, and the engine pays no exit
        # cost on it either.
        close = float(df["close"].iloc[-1])
        is_long = open_trade["direction"] == "long"
        trades.append(
            _trade_row(
                open_trade,
                exit_price=close * (1.0 - cost) if is_long else close * (1.0 + cost),
                exit_time=times[-1],
                exit_reason="end_of_data",
                bars_held=n - 1 - int(open_trade["entry_bar"]),
            )
        )
        open_trade = None
        closed_at_end = True

    equity = broker.equity_series()
    trades_df = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    metrics = compute_metrics(equity, trades_df, float(initial_cash), timeframe)
    if closed_at_end:
        metrics["open_at_end"] = 1.0

    account = broker.get_account_balance()
    return OrderRunResult(
        equity=equity,
        trades=trades_df,
        metrics=metrics,
        signals=sig,
        orders=orders,
        blocked=blocked,
        fills=broker.get_trade_history(limit=1_000_000),
        open_at_end=closed_at_end,
        cash=float(account.cash),
    )
