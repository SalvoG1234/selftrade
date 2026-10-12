"""The simulated broker: orders, positions and fills without a network.

This is the piece the brief puts first - "implementa inizialmente un broker
simulato" - and it is deliberately built to agree with the research engine rather
than to be a second, friendlier reality:

* a signal (order) submitted while looking at bar ``i`` fills at the **open of bar
  ``i + 1``**, the same rule as :func:`selftrade.backtest.simulate`;
* costs are ``fee_bps + slippage_bps`` **per side**, applied to the effective
  price against you;
* ``quote_qty`` sizes the order at the fill price, so a caller that thinks in
  equity fractions (the paper session) gets the same size the research engine
  would have used;
* the exit plan armed on a fill - hard stop, take profit, trailing - is evaluated
  intrabar with the **same ladder** as the research engine: a gap through the stop
  fills at the worse open, the stop is checked before the target, and the trailing
  level follows the best price seen since the entry;
* nothing here imports ``requests`` or touches the network - a test asserts it.

Everything the caller sees is the plain :class:`~selftrade.execution.base.BrokerAdapter`
contract, so a real adapter can be dropped in later without the caller noticing.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    Fill,
    Mismatch,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    utc_now_iso,
)
from .kill_switch import arming_detail
from .risk_gate import ALERT_KIND, RiskGate, describe_decision


class PaperBroker(BrokerAdapter):
    """Order-level simulation on a local OHLCV frame.  No money, no network."""

    name = "paper"
    mode = "paper"

    def __init__(
        self,
        df: pd.DataFrame,
        *,
        symbol: str = "BTC/USDT",
        cash: float = 10_000.0,
        fee_bps: float = 5.0,
        slippage_bps: float = 2.0,
        gate: RiskGate | None = None,
        kill_switch: Any = None,
        memory: Any = None,
        run_id: int | None = None,
        context: dict[str, Any] | None = None,
    ):
        if df is None or len(df) < 2:
            raise BrokerError("paper broker needs at least two bars of market data")
        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                raise BrokerError(f"market data is missing the '{col}' column")
        self.df = df
        # The bar columns as arrays: the session walks every bar, and `.iloc` on a
        # frame row is the one avoidable cost in that loop.
        self._op = df["open"].to_numpy(dtype="float64")
        self._hi = df["high"].to_numpy(dtype="float64")
        self._lo = df["low"].to_numpy(dtype="float64")
        self._cl = df["close"].to_numpy(dtype="float64")
        self.symbol = symbol
        self.initial_cash = float(cash)
        self.cash = float(cash)
        self._cost = (float(fee_bps) + float(slippage_bps)) / 10_000.0
        self.gate = gate
        self.kill_switch = kill_switch
        self.memory = memory
        self.run_id = run_id
        # Extra facts every audit line should carry (e.g. which paper session).
        self.context = dict(context or {})

        self.orders_submitted = 0
        self._i = 0
        self._orders: dict[str, Order] = {}
        self._client_ids: dict[str, str] = {}
        self._pending: list[str] = []
        self._pending_stops: dict[str, float] = {}
        self._pending_plans: dict[str, dict[str, float]] = {}
        self._positions: dict[str, Position] = {}
        self._stops: dict[str, float] = {}
        self._targets: dict[str, float] = {}
        self._trails: dict[str, tuple[float, float]] = {}
        self._fills: list[Fill] = []
        self._ledger: list[tuple[str, Side, float, float, float]] = []
        self._equity: list[float] = [float(cash)]
        self._realized = 0.0
        self._fees = 0.0
        self._peak = float(cash)
        self._day: Any = None
        self._day_start_equity = float(cash)
        self._day_pnl_pct = 0.0
        self._last_order_at: float | None = None
        self._counter = 0

    # ------------------------------------------------------------- utilities
    @property
    def index(self) -> int:
        """Index of the bar being looked at."""
        return self._i

    @property
    def now_ts(self) -> Any:
        return self.df.index[self._i]

    def _price(self) -> float:
        return float(self._cl[self._i])

    def _next_id(self, prefix: str = "o") -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _effective(self, side: Side, raw: float) -> float:
        return raw * (1.0 + self._cost) if side is Side.BUY else raw * (1.0 - self._cost)

    def _alert(self, kind: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        if self.memory is None:
            return
        try:
            self.memory.add_risk_event(self.run_id, kind, reason, {**self.context, **(detail or {})})
        except Exception:  # pragma: no cover - auditing must never break execution
            pass

    # ------------------------------------------------------------- the clock
    def advance(self, bars: int = 1) -> int:
        """Move forward in time: fill pending orders, check exits, mark to market."""
        for _ in range(max(0, int(bars))):
            if self._i >= len(self.df) - 1:
                break
            self._i += 1
            ts = self.df.index[self._i]
            self._process_pending(ts)
            self._check_exits(ts)
            self._mark(ts)
        return self._i

    def _process_pending(self, ts: Any) -> None:
        if not self._pending:
            return
        i = self._i
        open_, high, low = float(self._op[i]), float(self._hi[i]), float(self._lo[i])
        remaining: list[str] = []
        for oid in self._pending:
            order = self._orders[oid]
            fill_at: float | None = None
            if order.type is OrderType.MARKET:
                fill_at = open_
            elif order.side is Side.BUY:
                if low <= float(order.limit_price or 0.0):
                    fill_at = min(open_, float(order.limit_price))
            else:
                if high >= float(order.limit_price or 0.0):
                    fill_at = max(open_, float(order.limit_price))
            if fill_at is None:
                remaining.append(oid)
                continue
            self._execute(order, fill_at, ts, reason="signal")
        self._pending = remaining

    def _forget_plan(self, symbol: str) -> None:
        """Drop the armed exit plan of a flat symbol."""
        self._stops.pop(symbol, None)
        self._targets.pop(symbol, None)
        self._trails.pop(symbol, None)

    def _check_exits(self, ts: Any) -> None:
        """Evaluate the armed exit plan intrabar, with the research engine's ladder.

        Same order of decisions as :func:`selftrade.backtest.simulate`: the hard
        stop (trailed up from the best price seen, if a trailing stop is armed) is
        checked first, a gap through it fills at the worse open, and only then the
        take profit.
        """
        i = self._i
        open_, high, low = float(self._op[i]), float(self._hi[i]), float(self._lo[i])
        for symbol in list({*self._stops, *self._targets, *self._trails}):
            pos = self._positions.get(symbol)
            if pos is None or abs(pos.qty) < 1e-12:
                self._forget_plan(symbol)
                continue
            is_long = pos.qty > 0
            stop_level: float | None = self._stops.get(symbol)
            trail = self._trails.get(symbol)
            if trail is not None:
                pct, ref = trail
                level = ref * (1.0 - pct) if is_long else ref * (1.0 + pct)
                if stop_level is None:
                    stop_level = level
                else:
                    stop_level = max(stop_level, level) if is_long else min(stop_level, level)
            target = self._targets.get(symbol)
            raw: float | None = None
            reason = "stop"
            if is_long:
                if stop_level is not None and open_ <= stop_level:
                    raw = open_
                elif stop_level is not None and low <= stop_level:
                    raw = stop_level
                elif target is not None and open_ >= target:
                    raw, reason = open_, "target"
                elif target is not None and high >= target:
                    raw, reason = target, "target"
            else:
                if stop_level is not None and open_ >= stop_level:
                    raw = open_
                elif stop_level is not None and high >= stop_level:
                    raw = stop_level
                elif target is not None and open_ <= target:
                    raw, reason = open_, "target"
                elif target is not None and low <= target:
                    raw, reason = target, "target"
            if raw is None:
                continue
            order = Order(
                id=self._next_id("stop"),
                client_id=f"{reason}-{symbol}",
                symbol=symbol,
                side=Side.SELL if is_long else Side.BUY,
                qty=abs(pos.qty),
                type=OrderType.MARKET,
                status=OrderStatus.NEW,
                created_at=utc_now_iso(),
                note="hard stop" if reason == "stop" else "take profit",
            )
            self._orders[order.id] = order
            self._execute(order, raw, ts, reason=reason)

    def _execute(self, order: Order, raw_price: float, ts: Any, *, reason: str) -> None:
        eff = self._effective(order.side, float(raw_price))
        # A quote-sized order becomes a base-asset size at this very price, which
        # is what the research engine's "fraction of equity / effective price" is.
        qty = float(order.quote_qty) / eff if order.quote_qty else float(order.qty)
        order.qty = qty
        fee = abs(eff - float(raw_price)) * qty
        if order.side is Side.BUY:
            self.cash -= qty * eff
        else:
            self.cash += qty * eff
        self._fees += fee
        self._apply_fill(order.symbol, order.side, qty, eff, ts)
        self._fills.append(
            Fill(
                ts=str(ts),
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                qty=qty,
                price=eff,
                fee=fee,
                reason=reason,
            )
        )
        self._ledger.append((order.symbol, order.side, qty, eff, fee))
        # Arm the exit plan on the resulting position, if the request carried one.
        stop_pct = self._pending_stops.pop(order.id, None)
        plan = self._pending_plans.pop(order.id, None)
        pos = self._positions.get(order.symbol)
        if pos is None or abs(pos.qty) < 1e-12:
            self._forget_plan(order.symbol)
        else:
            self._targets.pop(order.symbol, None)
            self._trails.pop(order.symbol, None)
            if stop_pct is not None:
                self._stops[order.symbol] = (
                    pos.avg_price * (1.0 + stop_pct)
                    if pos.qty > 0
                    else pos.avg_price * (1.0 - stop_pct)
                )
            if plan and plan.get("target_pct") is not None:
                tp = float(plan["target_pct"])
                self._targets[order.symbol] = (
                    pos.avg_price * (1.0 + tp) if pos.qty > 0 else pos.avg_price * (1.0 - tp)
                )
            if plan and plan.get("trail_pct") is not None:
                self._trails[order.symbol] = (float(plan["trail_pct"]), pos.avg_price)
        order.status = OrderStatus.FILLED
        order.filled_qty = qty
        order.avg_price = eff
        order.updated_at = utc_now_iso()

    def _apply_fill(self, symbol: str, side: Side, qty: float, price: float, ts: Any) -> None:
        pos = self._positions.get(symbol)
        signed = side.sign * qty
        if pos is None or abs(pos.qty) < 1e-12:
            self._positions[symbol] = Position(symbol, signed, price, str(ts))
            return
        new_qty = pos.qty + signed
        if pos.qty * signed > 0:  # increasing
            total = abs(pos.qty) + abs(signed)
            avg = (abs(pos.qty) * pos.avg_price + abs(signed) * price) / total
            self._positions[symbol] = Position(symbol, new_qty, avg, pos.opened_at)
            return
        closed = min(abs(pos.qty), abs(signed))
        direction = 1.0 if pos.qty > 0 else -1.0
        self._realized += closed * (price - pos.avg_price) * direction
        if abs(new_qty) < 1e-12:
            del self._positions[symbol]
        else:
            self._positions[symbol] = Position(
                symbol, new_qty, pos.avg_price if pos.qty * new_qty > 0 else price, pos.opened_at
            )

    def _mark(self, ts: Any) -> float:
        close = self._price()
        # The trailing level follows the best price seen since the entry - the same
        # update the research engine does after its own exit check.
        i = self._i
        for symbol, pos in self._positions.items():
            trail = self._trails.get(symbol)
            if trail is None:
                continue
            pct, ref = trail
            self._trails[symbol] = (
                pct,
                max(ref, float(self._hi[i])) if pos.qty > 0 else min(ref, float(self._lo[i])),
            )
        pos_value = sum(p.qty * close for p in self._positions.values())
        equity = self.cash + pos_value
        day = pd.Timestamp(ts).date()
        if self._day != day:
            self._day = day
            self._day_start_equity = equity
        self._peak = max(self._peak, equity)
        self._day_pnl_pct = equity / self._day_start_equity - 1.0 if self._day_start_equity else 0.0
        self._equity.append(equity)
        return equity

    # -------------------------------------------------------- the interface
    def get_account_balance(self) -> AccountSnapshot:
        close = self._price()
        pos_value = sum(p.qty * close for p in self._positions.values())
        equity = self.cash + pos_value
        unreal = sum(p.unrealized(close) for p in self._positions.values())
        drawdown = 1.0 - equity / self._peak if self._peak > 0 else 0.0
        return AccountSnapshot(
            ts=str(self.now_ts),
            cash=self.cash,
            positions_value=pos_value,
            equity=equity,
            realized_pnl=self._realized,
            unrealized_pnl=unreal,
            day_pnl_pct=self._day_pnl_pct,
            drawdown=max(0.0, drawdown),
            open_positions=len([p for p in self._positions.values() if abs(p.qty) > 0]),
            fees_paid=self._fees,
        )

    def get_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if abs(p.qty) > 0]

    def equity_value(self) -> float:
        """Equity at the current bar, without building a snapshot.

        The same number ``get_account_balance().equity`` reports; a caller walking
        thousands of bars wants it without the string and the sums it does not need.
        """
        close = float(self._cl[self._i])
        return self.cash + sum(p.qty * close for p in self._positions.values())

    def fills_since(self, index: int) -> list[Fill]:
        """Fills after ``index`` (the length of the list a caller already has).

        For stepping callers, so they can diff the tape instead of rebuilding it.
        """
        return self._fills[max(0, int(index)) :]

    def get_market_data(self, symbol: str | None = None, limit: int = 100) -> pd.DataFrame:
        return self.df.iloc[: self._i + 1].tail(int(limit))

    def open_orders(self) -> list[Order]:
        return [self._orders[oid] for oid in self._pending]

    def submit_order(self, request: OrderRequest) -> Order:
        """Validate, gate the order, then queue it for the next open.

        Idempotent on ``client_id``: a retried submission returns the existing
        order instead of doubling the position.
        """
        existing_id = self._client_ids.get(request.client_id)
        if existing_id is not None:
            return self._orders[existing_id]

        def reject(reason: str, checks: list[dict[str, Any]] | None = None) -> Order:
            order = Order(
                id=self._next_id("r"),
                client_id=request.client_id,
                symbol=request.symbol,
                side=request.side,
                qty=float(request.qty),
                type=request.type,
                status=OrderStatus.REJECTED,
                created_at=utc_now_iso(),
                limit_price=request.limit_price,
                reject_reason=reason,
                note=request.note,
            )
            self._orders[order.id] = order
            self._client_ids[request.client_id] = order.id
            self._alert(
                ALERT_KIND,
                reason,
                {"checks": checks or [], "request": order.as_dict()},
            )
            return order

        if request.sized_at_fill and float(request.qty) > 0:
            return reject("give either qty or quote_qty, not both")
        if float(request.qty) <= 0 and not request.sized_at_fill:
            return reject(f"qty must be positive, got {request.qty}")
        if request.type is OrderType.LIMIT and not request.limit_price:
            return reject("a limit order needs limit_price")
        if request.symbol != self.symbol:
            return reject(
                f"{request.symbol} is not available here (this broker serves {self.symbol})"
            )

        self.orders_submitted += 1
        price = float(request.limit_price) if request.type is OrderType.LIMIT else self._price()

        # A buy must be payable: this rail will not invent cash.  A sell larger than
        # the position is allowed and becomes a short (the risk gate caps exposure);
        # ``backtest`` treats it the same way when ``allow_short`` is on.
        if request.side is Side.BUY:
            # A quote-sized buy costs exactly its quote amount, whatever the price.
            needed = (
                abs(float(request.quote_qty))
                if request.sized_at_fill
                else float(request.qty) * float(price) * (1.0 + self._cost)
            )
            if needed > self.cash + 1e-9:
                return reject(
                    f"not enough cash for this order: {needed:,.2f} needed, "
                    f"{self.cash:,.2f} available"
                )

        if self.gate is not None:
            account = self.get_account_balance()
            decision = self.gate.check(
                request,
                account,
                self.get_positions(),
                price=price,
                last_order_at=self._last_order_at,
                now=pd.Timestamp(self.now_ts).timestamp(),
                kill_switch=self.kill_switch,
            )
            if not decision.ok:
                reason = describe_decision(decision)
                if self.kill_switch is not None and decision.alerts:
                    # The program stopped itself: the switch keeps the margin the gate saw
                    # at this instant, which is what the alert (and the panel) then report.
                    self.kill_switch.trigger(
                        reason,
                        by="risk_gate",
                        detail=arming_detail(account, decision.checks, alerts=decision.alerts),
                    )
                # reject() records the refusal once, with the gate's own checks.
                return reject(reason, decision.checks)

        order = Order(
            id=self._next_id("o"),
            client_id=request.client_id,
            symbol=request.symbol,
            side=request.side,
            qty=float(request.qty),
            type=request.type,
            status=OrderStatus.NEW,
            created_at=utc_now_iso(),
            limit_price=request.limit_price,
            quote_qty=float(request.quote_qty) if request.sized_at_fill else None,
            note=request.note,
        )
        self._orders[order.id] = order
        self._client_ids[request.client_id] = order.id
        self._pending.append(order.id)
        self._last_order_at = pd.Timestamp(self.now_ts).timestamp()
        if request.stop_loss_pct is not None and abs(float(request.stop_loss_pct)) > 0:
            self._pending_stops[order.id] = float(request.stop_loss_pct)
        plan: dict[str, float] = {}
        for key, value in (
            ("target_pct", request.take_profit_pct),
            ("trail_pct", request.trailing_pct),
        ):
            if value is not None and abs(float(value)) > 0:
                plan[key] = float(value)
        if plan:
            self._pending_plans[order.id] = plan
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise BrokerError(f"unknown order {order_id}")
        if order.status is OrderStatus.FILLED:
            raise BrokerError(f"order {order_id} is already filled and cannot be canceled")
        if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
            return order
        order.status = OrderStatus.CANCELED
        order.updated_at = utc_now_iso()
        if order_id in self._pending:
            self._pending.remove(order_id)
        return order

    def get_order_status(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_trade_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self._fills[-int(limit) :]]

    # ------------------------------------------------------------ integrity
    def reconcile(self) -> list[Mismatch]:
        """Rebuild positions and cash from the fill ledger and compare.

        A paper broker that contradicts its own ledger is worse than useless, and
        the same check is what a live adapter needs after a reconnect.
        """
        qty: dict[str, float] = {}
        cost: dict[str, float] = {}
        cash = self.initial_cash
        fees = 0.0
        for symbol, side, filled, price, fee in self._ledger:
            signed = side.sign * filled
            qty[symbol] = qty.get(symbol, 0.0) + signed
            cost[symbol] = cost.get(symbol, 0.0) + signed * price
            cash += -filled * price if side is Side.BUY else filled * price
            fees += fee
        out: list[Mismatch] = []
        for symbol, signed in qty.items():
            internal = self._positions.get(symbol)
            internal_qty = internal.qty if internal else 0.0
            if abs(internal_qty - signed) > 1e-9:
                out.append(
                    Mismatch("position_qty", internal_qty, signed, f"{symbol} differs from the ledger")
                )
        for symbol, pos in self._positions.items():
            if symbol not in qty:
                out.append(Mismatch("position_qty", pos.qty, 0.0, f"{symbol} is not in the ledger"))
        if abs(cash - self.cash) > 1e-6:
            out.append(Mismatch("cash", self.cash, cash, "cash differs from the ledger"))
        if abs(fees - self._fees) > 1e-9:
            out.append(Mismatch("fees_paid", self._fees, fees, "fees differ from the ledger"))
        return out

    def equity_series(self) -> pd.Series:
        """Equity at each bar visited so far (for reports and comparisons)."""
        idx = self.df.index[: len(self._equity)]
        return pd.Series(self._equity, index=idx, dtype="float64")

    def describe(self) -> dict[str, Any]:
        out = super().describe()
        out.update(
            {
                "symbol": self.symbol,
                "bar": int(self._i),
                "ts": str(self.now_ts),
                "fill_rule": "next bar open, costs on both sides",
                "network": "none",
            }
        )
        return out
