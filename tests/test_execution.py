"""Tests for the order-level execution layer.

Four things are being pinned here, and they are the acceptance criteria of the
brief rather than implementation details:

* an order fills at the **next open**, with costs on both sides - the same rule as
  the backtester, so the two rails cannot tell different stories - and `quote_qty`
  resolves into a size at that very price;
* the exit plan armed on a fill (hard stop, take profit, trailing) is evaluated
  intrabar with the engine's ladder: gap fills at the open, stop before target;
* the **risk gate blocks** an order before it exists, records why and raises an
  alert, with the portfolio limits taken from the sealed layer - while an order that
  *closes* a position is not treated as new exposure;
* the **kill switch** stops new orders and survives a restart (and a corrupt state
  file fails *closed*);
* the simulated broker's books **reconcile with its own fill ledger**.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from selftrade.execution import (
    AccountSnapshot,
    BrokerError,
    KillSwitch,
    Mismatch,
    OrderRequest,
    OrderStatus,
    OrderType,
    PaperBroker,
    Position,
    RiskGate,
    Side,
    arming_detail,
    describe_decision,
    kill_switch_path,
)
from selftrade.execution.base import BrokerAdapter
from selftrade.memory import Memory
from selftrade.risk_rules import LIMITS, SEALED_DIGEST, digest_of

COST = (5.0 + 2.0) / 10_000.0  # fee + slippage per side


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def frame(rows, start="2024-01-01", freq="1h") -> pd.DataFrame:
    """OHLCV from explicit ``(open, high, low, close)`` tuples."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def broker(rows, **kw) -> PaperBroker:
    df = frame(rows)
    df["volume"] = 1.0
    return PaperBroker(
        df, symbol="BTC/USDT", cash=kw.pop("cash", 10_000.0), fee_bps=5.0, slippage_bps=2.0, **kw
    )


def free_broker(rows, **kw) -> PaperBroker:
    """A broker with no costs, for the assertions that have to be exact."""
    df = frame(rows)
    df["volume"] = 1.0
    return PaperBroker(
        df, symbol="BTC/USDT", cash=kw.pop("cash", 10_000.0), fee_bps=0.0, slippage_bps=0.0, **kw
    )


def buy(qty, stop=None, client_id="c1", symbol="BTC/USDT") -> OrderRequest:
    return OrderRequest(
        client_id=client_id, symbol=symbol, side=Side.BUY, qty=qty, stop_loss_pct=stop
    )


def snapshot(**kw) -> AccountSnapshot:
    base = dict(
        ts="2024-01-01T00:00:00+00:00",
        cash=10_000.0,
        positions_value=0.0,
        equity=10_000.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        day_pnl_pct=0.0,
        drawdown=0.0,
        open_positions=0,
    )
    base.update(kw)
    return AccountSnapshot(**base)


# --------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------

def test_adapter_is_abstract_and_defaults_to_paper():
    with pytest.raises(TypeError):
        BrokerAdapter()  # the seven calls are abstract
    for name in (
        "get_account_balance",
        "get_positions",
        "get_market_data",
        "submit_order",
        "cancel_order",
        "get_order_status",
        "get_trade_history",
    ):
        assert name in BrokerAdapter.__abstractmethods__


def test_paper_broker_declares_itself_paper_and_offline():
    b = broker([(100, 101, 99, 100), (101, 102, 100, 101)])
    info = b.describe()
    assert info["mode"] == "paper"
    assert info["network"] == "none"
    assert info["fill_rule"].startswith("next bar open")


def test_execution_package_has_no_network_imports():
    """"No order reali all'avvio" has to be structural, not a promise."""
    forbidden = {"requests", "urllib", "http", "socket", "websocket", "aiohttp", "httpx"}
    root = Path(__file__).resolve().parents[1] / "selftrade" / "execution"
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"{path.name} imports {name}"


# --------------------------------------------------------------------------
# fills, costs, positions
# --------------------------------------------------------------------------

def test_order_fills_at_next_open_with_costs_on_the_buy_side():
    b = broker([(100, 101, 99, 100), (110, 112, 109, 111), (111, 113, 110, 112)])
    order = b.submit_order(buy(10))
    assert order.status is OrderStatus.NEW
    assert b.get_positions() == []
    assert b.open_orders()[0].id == order.id

    b.advance()
    filled = b.get_order_status(order.id)
    assert filled.status is OrderStatus.FILLED
    assert filled.avg_price == pytest.approx(110.0 * (1 + COST))
    assert filled.filled_qty == pytest.approx(10.0)
    assert b.cash == pytest.approx(10_000.0 - 10 * 110.0 * (1 + COST))
    pos = b.get_positions()[0]
    assert (pos.qty, pos.avg_price) == (pytest.approx(10.0), pytest.approx(110.0 * (1 + COST)))
    assert b.get_account_balance().fees_paid == pytest.approx(10 * 110.0 * COST)
    assert b.reconcile() == []


def test_sell_side_pays_the_cost_too():
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100), (105, 106, 104, 105)])
    b.submit_order(buy(5))
    b.advance()
    b.submit_order(OrderRequest("c2", "BTC/USDT", Side.SELL, 5))
    b.advance()
    fills = b.get_trade_history()
    assert fills[-1]["price"] == pytest.approx(105.0 * (1 - COST))
    # entry cost + exit cost: the round trip is worse than the raw price move
    raw_gain = 5 * (105.0 - 100.0)
    assert b.get_account_balance().realized_pnl < raw_gain
    assert b.reconcile() == []


def test_limit_order_waits_for_the_price():
    b = broker([(100, 101, 99, 100), (105, 106, 104, 105), (98, 99, 97, 98)])
    order = b.submit_order(
        OrderRequest("c1", "BTC/USDT", Side.BUY, 1.0, type=OrderType.LIMIT, limit_price=99.0)
    )
    b.advance()
    assert b.get_order_status(order.id).status is OrderStatus.NEW  # 105 never trades at 99 (low 104)
    b.advance()
    filled = b.get_order_status(order.id)
    assert filled.status is OrderStatus.FILLED
    # the bar gapped below the limit: a buy fills at the open, which is better than 99
    assert filled.avg_price == pytest.approx(98.0 * (1 + COST))


def test_cancel_stops_a_working_order():
    b = broker([(100, 101, 99, 100), (105, 106, 104, 105), (98, 99, 97, 98)])
    order = b.submit_order(
        OrderRequest("c1", "BTC/USDT", Side.BUY, 1.0, type=OrderType.LIMIT, limit_price=99.0)
    )
    assert b.cancel_order(order.id).status is OrderStatus.CANCELED
    b.advance(2)
    assert b.get_order_status(order.id).status is OrderStatus.CANCELED
    assert b.get_positions() == []
    with pytest.raises(BrokerError):
        b.cancel_order("does-not-exist")


def test_filled_order_cannot_be_canceled():
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100)])
    order = b.submit_order(buy(1))
    b.advance()
    with pytest.raises(BrokerError):
        b.cancel_order(order.id)


def test_retried_client_id_is_idempotent():
    """Protezione da ordini duplicati: the same client_id is the same order."""
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    first = b.submit_order(buy(1.0, client_id="same"))
    again = b.submit_order(buy(1.0, client_id="same"))
    assert again.id == first.id
    assert b.orders_submitted == 1
    b.advance(2)
    assert len(b.get_positions()) == 1
    assert b.get_positions()[0].qty == pytest.approx(1.0)


# --------------------------------------------------------------------------
# rejections
# --------------------------------------------------------------------------

def test_broker_rejects_impossible_orders():
    b = broker([(100, 101, 99, 100)] * 2)
    assert b.submit_order(buy(0, client_id="c0")).reject_reason.startswith("qty must be positive")
    wrong = b.submit_order(buy(1, symbol="ETH/USDT", client_id="c1"))
    assert "not available here" in wrong.reject_reason
    no_limit = OrderRequest("c3", "BTC/USDT", Side.BUY, 1.0, type=OrderType.LIMIT)
    assert b.submit_order(no_limit).reject_reason == "a limit order needs limit_price"
    broke = broker([(100, 101, 99, 100)] * 2, cash=50.0).submit_order(buy(1, client_id="c4"))
    assert broke.status is OrderStatus.REJECTED
    assert "not enough cash" in broke.reject_reason


def test_rejected_order_carries_its_reason_and_is_not_open():
    b = broker([(100, 101, 99, 100)] * 2)
    order = b.submit_order(buy(0))
    assert not order.is_open
    assert b.open_orders() == []
    assert order.as_dict()["reject_reason"]


# --------------------------------------------------------------------------
# the hard stop
# --------------------------------------------------------------------------

def test_hard_stop_exits_the_position():
    b = broker([(100, 101, 99, 100), (100, 102, 98, 101), (96, 97, 90, 91)])
    order = b.submit_order(buy(10, stop=-0.05))
    b.advance()  # fills at 100, stop sits 5% under the paid price
    entry = b.get_positions()[0].avg_price
    assert b.get_positions()[0].qty == pytest.approx(10.0)
    b.advance()  # bar 3 dips to 90, through the stop
    assert b.get_positions() == []
    stop_fill = b.get_trade_history()[-1]
    assert stop_fill["reason"] == "stop"
    assert stop_fill["side"] == "sell"
    assert stop_fill["price"] == pytest.approx(entry * 0.95 * (1 - COST))
    assert b.get_account_balance().realized_pnl < 0
    assert b.reconcile() == []


def test_gap_through_the_stop_fills_at_the_open_not_the_stop():
    """A stop is not a guaranteed price - the module says so and behaves so."""
    b = broker([(100, 101, 99, 100), (100, 102, 98, 101), (90, 92, 88, 89)])
    b.submit_order(buy(10, stop=-0.05))
    b.advance()
    entry = b.get_positions()[0].avg_price
    b.advance()
    assert b.get_positions() == []
    fill = b.get_trade_history()[-1]
    assert fill["reason"] == "stop"
    assert fill["price"] == pytest.approx(90.0 * (1 - COST))
    assert fill["price"] < entry * 0.95  # the gap cost real money, as it would live


def test_stop_is_armed_only_on_the_resulting_position():
    b = broker([(100, 101, 99, 100), (100, 100.5, 99.5, 100), (100, 101, 99, 100)])
    b.submit_order(buy(1, stop=-0.02))  # a 2% stop on a 1-unit entry
    b.advance()
    entry = b.get_positions()[0].avg_price
    assert b._stops == {"BTC/USDT": pytest.approx(entry * 0.98)}
    assert b._pending_stops == {}


def test_a_quote_sized_order_is_resolved_at_the_fill_price():
    """``quote_qty`` spends a given amount: the size follows the price the fill gets."""
    b = free_broker([(100, 101, 99, 100), (200, 210, 190, 205)])
    order = b.submit_order(OrderRequest("q1", "BTC/USDT", Side.BUY, quote_qty=1_000.0))
    assert order.status is OrderStatus.NEW
    assert order.quote_qty == pytest.approx(1_000.0)
    b.advance()  # fills at the open of bar 2, which is 200 - not the price it saw
    fill = b.get_trade_history()[0]
    assert fill["qty"] == pytest.approx(5.0)
    assert fill["price"] == pytest.approx(200.0)
    assert b.cash == pytest.approx(9_000.0)
    assert b.get_positions()[0].qty == pytest.approx(5.0)
    assert b.reconcile() == []


def test_a_quote_sized_order_keeps_the_costs_inside_the_amount():
    b = broker([(100, 101, 99, 100), (200, 210, 190, 205)])
    b.submit_order(OrderRequest("q2", "BTC/USDT", Side.BUY, quote_qty=1_000.0))
    b.advance()
    fill = b.get_trade_history()[0]
    assert fill["qty"] * fill["price"] == pytest.approx(1_000.0)  # costs included
    assert fill["price"] == pytest.approx(200.0 * (1 + COST))
    assert b.cash == pytest.approx(9_000.0)


def test_sizing_an_order_two_ways_at_once_is_refused():
    b = broker([(100, 101, 99, 100)] * 2)
    order = b.submit_order(OrderRequest("both", "BTC/USDT", Side.BUY, qty=1.0, quote_qty=100.0))
    assert order.status is OrderStatus.REJECTED
    assert "either qty or quote_qty" in order.reject_reason


def test_take_profit_and_trailing_are_armed_with_the_stop():
    b = broker([(100, 101, 99, 100), (100, 100.5, 99.5, 100)])
    b.submit_order(
        OrderRequest(
            "plan",
            "BTC/USDT",
            Side.BUY,
            qty=1.0,
            stop_loss_pct=-0.05,
            take_profit_pct=0.10,
            trailing_pct=0.03,
        )
    )
    b.advance()
    entry = b.get_positions()[0].avg_price
    assert b._stops == {"BTC/USDT": pytest.approx(entry * 0.95)}
    assert b._targets == {"BTC/USDT": pytest.approx(entry * 1.10)}
    assert b._trails["BTC/USDT"][0] == pytest.approx(0.03)
    # The reference starts at the paid price and follows the bar's own high, exactly
    # as the research engine's does.
    assert b._trails["BTC/USDT"][1] == pytest.approx(100.5)
    assert b._trails["BTC/USDT"][1] >= entry


def test_take_profit_fills_at_the_level():
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100), (100, 115, 99, 112)])
    b.submit_order(
        OrderRequest(
            "tp", "BTC/USDT", Side.BUY, qty=1.0, stop_loss_pct=-0.05, take_profit_pct=0.10
        )
    )
    b.advance()  # entry at 100.07
    entry = b.get_positions()[0].avg_price
    b.advance()  # the high of 115 reaches the 10% target
    fill = b.get_trade_history()[-1]
    assert fill["reason"] == "target"
    assert fill["price"] == pytest.approx(entry * 1.10 * (1 - COST))
    assert b.get_positions() == []
    assert b.reconcile() == []


def test_the_stop_is_decided_before_the_target_in_the_same_bar():
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100), (100, 120, 90, 110)])
    b.submit_order(
        OrderRequest(
            "both", "BTC/USDT", Side.BUY, qty=1.0, stop_loss_pct=-0.05, take_profit_pct=0.10
        )
    )
    b.advance()
    entry = b.get_positions()[0].avg_price
    b.advance()  # one bar touches both levels
    fill = b.get_trade_history()[-1]
    assert fill["reason"] == "stop"  # conservative, and the research engine's rule
    assert fill["price"] == pytest.approx(entry * 0.95 * (1 - COST))


def test_the_trailing_stop_follows_the_best_price_and_keeps_the_profit():
    b = broker(
        [(100, 101, 99, 100), (100, 101, 99, 100), (100, 120, 119, 119), (119, 120, 110, 111)]
    )
    b.submit_order(
        OrderRequest(
            "trail", "BTC/USDT", Side.BUY, qty=1.0, stop_loss_pct=-0.20, trailing_pct=0.05
        )
    )
    b.advance()  # entry at the open
    b.advance()  # the high of 120 lifts the trailing reference
    b.advance()  # 120 * 0.95 = 114; the low of 110 goes through it
    fill = b.get_trade_history()[-1]
    assert fill["reason"] == "stop"
    assert fill["price"] == pytest.approx(120.0 * 0.95 * (1 - COST))
    assert b.get_account_balance().realized_pnl > 0


# --------------------------------------------------------------------------
# integrity of the books
# --------------------------------------------------------------------------

def test_ledger_reconciles_through_a_mixed_sequence():
    b = broker([(100, 101, 99, 100)] * 8)
    b.submit_order(buy(4, client_id="a"))
    b.advance()
    b.submit_order(OrderRequest("b", "BTC/USDT", Side.SELL, 1.5))  # partial close
    b.advance()
    b.submit_order(OrderRequest("c", "BTC/USDT", Side.SELL, 5.0))  # flips to short
    b.advance()
    b.submit_order(buy(2.0, client_id="d"))  # reduces the short
    b.advance()
    assert b.reconcile() == []
    assert b.get_positions()[0].qty == pytest.approx(-0.5)  # 4 - 1.5 - 5 + 2
    assert len(b.get_trade_history()) == 4


def test_reconcile_detects_a_tampered_position():
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100)])
    b.submit_order(buy(1.0))
    b.advance()
    b._positions["BTC/USDT"] = Position("BTC/USDT", 99.0, 100.0)  # something went wrong
    mismatches = b.reconcile()
    assert [m.what for m in mismatches] == ["position_qty"]
    assert isinstance(mismatches[0], Mismatch)
    assert mismatches[0].as_dict()["internal"] == 99.0


def test_reconcile_detects_tampered_cash():
    b = broker([(100, 101, 99, 100), (100, 101, 99, 100)])
    b.submit_order(buy(1.0))
    b.advance()
    b.cash += 1.0
    assert [m.what for m in b.reconcile()] == ["cash"]


def test_equity_series_tracks_the_bars_visited():
    b = broker([(100, 101, 99, 100)] * 5)
    b.advance(3)
    eq = b.equity_series()
    assert len(eq) == 4  # the starting point plus three bars
    assert eq.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert eq.iloc[0] == pytest.approx(10_000.0)


# --------------------------------------------------------------------------
# the risk gate
# --------------------------------------------------------------------------

def test_gate_takes_its_portfolio_limits_from_the_sealed_layer():
    assert digest_of() == SEALED_DIGEST
    assert RiskGate().limits is LIMITS
    assert LIMITS.MODEL_CAN_OVERRIDE is False
    assert set(RiskGate().limits.as_dict()) >= {
        "MAX_POSITION_PCT",
        "HARD_STOP_LOSS_PCT",
        "MAX_DAILY_LOSS_PCT",
        "MAX_DRAWDOWN_PCT",
        "MODEL_CAN_OVERRIDE",
    }


def test_gate_from_config_takes_its_thresholds_from_settings():
    class Cfg:
        enabled = True
        risk_pct = 0.002
        max_positions = 4
        max_leverage = 2.0
        min_seconds_between_orders = 30.0
        min_notional = 25.0
        require_stop = False
        allowed_symbols = ["BTC/USDT"]

    gate = RiskGate.from_config(Cfg())
    assert (gate.max_positions, gate.max_leverage) == (4, 2.0)
    assert gate.min_seconds_between_orders == 30.0
    assert gate.min_notional == 25.0
    assert gate.require_stop is False
    assert gate.allowed_symbols == ["BTC/USDT"]
    # the sealed limits are never taken from the config section
    assert gate.limits is LIMITS

    empty = RiskGate.from_config(type("C", (), {"allowed_symbols": []})())
    assert empty.allowed_symbols is None
    assert empty.max_positions == RiskGate.max_positions  # dataclass defaults


def test_gate_blocks_an_oversized_position_and_says_why():
    gate = RiskGate(max_positions=1)
    req = buy(60.0, stop=-0.01)  # 6,000 on 10,000 equity = 60%
    decision = gate.check(req, snapshot(), [], price=100.0)
    assert not decision.ok
    assert any("per-position cap" in r for r in decision.reasons)
    check = next(c for c in decision.checks if c["name"] == "position_pct")
    assert (check["value"], check["limit"]) == (pytest.approx(60.0), 5.0)
    assert "blocked" in describe_decision(decision)


def test_gate_requires_a_stop_and_bounds_its_distance():
    gate = RiskGate(max_positions=1)
    no_stop = gate.check(buy(1.0), snapshot(), [], price=100.0)
    assert any("no stop loss" in r for r in no_stop.reasons)

    too_wide = gate.check(buy(1.0, stop=-0.08), snapshot(), [], price=100.0)
    assert any("sealed hard stop of 5.0%" in r for r in too_wide.reasons)


def test_gate_bounds_the_risk_per_trade():
    gate = RiskGate(max_positions=1, risk_pct=0.005)
    # 4% of a 10,000 equity with a 2% stop risks 0.08%: fine
    ok = gate.check(buy(4.0, stop=-0.02), snapshot(), [], price=100.0)
    assert ok.ok, ok.reasons

    gate = RiskGate(max_positions=1, risk_pct=0.0005)  # a 0.05% budget
    tight = gate.check(buy(4.0, stop=-0.02), snapshot(), [], price=100.0)
    assert not tight.ok
    assert any("per-trade budget" in r for r in tight.reasons)


def test_gate_does_not_count_an_exit_as_new_exposure():
    """The same cap that lets a 5% entry in must not block the exit that closes it.

    An order that reduces the position adds no exposure and needs no stop of its own:
    the position it closes was gated, stop included, when it was opened.
    """
    gate = RiskGate()
    held = [Position("BTC/USDT", 0.5, 100.0)]  # 50 of a 10,000 equity
    closing = OrderRequest("close", "BTC/USDT", Side.SELL, 0.5)
    decision = gate.check(closing, snapshot(), held, price=100.0)
    assert decision.ok, decision.reasons
    pct = next(c for c in decision.checks if c["name"] == "position_pct")
    assert pct["value"] == pytest.approx(0.0)
    assert not any("no stop loss" in r for r in decision.reasons)

    # An order far bigger than the position flips the book: that new side is capped.
    flip = OrderRequest("flip", "BTC/USDT", Side.SELL, 60.0, stop_loss_pct=-0.01)
    blocked = gate.check(flip, snapshot(), held, price=100.0)
    assert not blocked.ok
    assert any("per-position cap" in r for r in blocked.reasons)
    assert any("per-trade budget" in r or "per-position cap" in r for r in blocked.reasons)


def test_gate_treats_a_quote_sized_order_by_its_amount():
    gate = RiskGate()
    small = OrderRequest(
        "q", "BTC/USDT", Side.BUY, quote_qty=400.0, stop_loss_pct=-0.02
    )
    decision = gate.check(small, snapshot(), [], price=100.0)
    assert decision.ok, decision.reasons
    pct = next(c for c in decision.checks if c["name"] == "position_pct")
    assert pct["value"] == pytest.approx(4.0)  # 400 on 10,000: no reference price needed

    too_big = OrderRequest(
        "q2", "BTC/USDT", Side.BUY, quote_qty=900.0, stop_loss_pct=-0.02
    )
    assert not gate.check(too_big, snapshot(), [], price=100.0).ok
    # ...and giving both sizes is refused as ambiguous
    both = OrderRequest("q3", "BTC/USDT", Side.BUY, qty=1.0, quote_qty=100.0)
    assert any(
        "either qty or quote_qty" in r
        for r in gate.check(both, snapshot(), [], price=100.0).reasons
    )


def test_gate_blocks_a_new_symbol_when_the_book_is_full():
    gate = RiskGate(max_positions=1)
    held = [Position("ETH/USDT", 1.0, 100.0)]
    decision = gate.check(buy(1.0, stop=-0.01), snapshot(), held, price=100.0)
    assert any("already holding 1 position" in r for r in decision.reasons)
    # the same symbol is not a new position
    same = gate.check(buy(1.0, stop=-0.01), snapshot(), [Position("BTC/USDT", 1.0, 100.0)], price=100.0)
    assert not any("already holding" in r for r in same.reasons)


def test_gate_blocks_on_drawdown_and_daily_loss_and_raises_alerts():
    gate = RiskGate(max_positions=1)
    dd = gate.check(buy(1.0, stop=-0.01), snapshot(drawdown=0.18), [], price=100.0)
    assert not dd.ok and dd.alerts
    assert any("MAX_DRAWDOWN" in c["name"].upper() or c["name"] == "max_drawdown" for c in dd.checks)
    assert any("15.0%" in r for r in dd.reasons)

    day = gate.check(buy(1.0, stop=-0.01), snapshot(day_pnl_pct=-0.03), [], price=100.0)
    assert not day.ok and day.alerts
    assert any("daily halt" in r for r in day.reasons)


def test_gate_bounds_leverage_and_allowed_symbols():
    gate = RiskGate(max_positions=2, max_leverage=1.5)
    positions = [Position("ETH/USDT", 160.0, 100.0)]  # 16,000 of gross exposure
    req = buy(4.0, stop=-0.01)  # another 400 -> 1.64x
    decision = gate.check(req, snapshot(), positions, price=100.0)
    assert any("exceeds the 1.50x limit" in r for r in decision.reasons)

    gate = RiskGate(max_positions=1, allowed_symbols=["BTC/USDT"])
    other = gate.check(buy(1.0, stop=-0.01, symbol="SOL/USDT"), snapshot(), [], price=100.0)
    assert any("not in the allowed_symbols" in r for r in other.reasons)


def test_gate_bounds_order_frequency():
    gate = RiskGate(max_positions=1, min_seconds_between_orders=60)
    check = gate.check(
        buy(1.0, stop=-0.01), snapshot(), [], price=100.0, last_order_at=1_000.0, now=1_030.0
    )
    assert not check.ok
    assert any("minimum spacing" in r for r in check.reasons)
    ok = gate.check(
        buy(1.0, stop=-0.01), snapshot(), [], price=100.0, last_order_at=1_000.0, now=1_100.0
    )
    assert ok.ok


def test_gate_blocks_on_the_kill_switch_first(tmp_path):
    switch = KillSwitch(tmp_path / "s.json")
    switch.trigger("manual test", by="test")
    gate = RiskGate(max_positions=1)
    decision = gate.check(
        buy(1.0, stop=-0.01), snapshot(), [], price=100.0, kill_switch=switch
    )
    assert not decision.ok
    assert decision.reasons[0].startswith("kill switch is engaged")
    assert decision.alerts
    assert decision.checks[0]["name"] == "kill_switch"


def test_gate_rejects_an_order_below_the_minimum_notional():
    gate = RiskGate(max_positions=1, min_notional=100.0)
    small = gate.check(buy(0.5, stop=-0.01), snapshot(), [], price=100.0)
    assert any("below the minimum" in r for r in small.reasons)


# --------------------------------------------------------------------------
# gate + broker + memory: the wiring
# --------------------------------------------------------------------------

def test_broker_refuses_an_order_the_gate_blocks_and_records_it(tmp_path):
    mem = Memory(tmp_path / "m.db")
    b = broker([(100, 101, 99, 100)] * 3, gate=RiskGate(max_positions=1), memory=mem)
    order = b.submit_order(buy(60.0, stop=-0.01))
    assert order.status is OrderStatus.REJECTED
    assert "per-position cap" in order.reject_reason
    assert b.open_orders() == []
    b.advance(2)
    assert b.get_positions() == []  # nothing leaked through
    events = mem.risk_events()
    assert len(events) == 1
    assert events[0]["kind"] == "order_blocked"
    assert "per-position cap" in events[0]["reason"]
    assert events[0]["detail"]["checks"]
    assert b.reconcile() == []


def test_alerts_engage_the_kill_switch_and_the_next_order_is_refused(tmp_path):
    """A breached limit does not just log: it stops new orders."""

    class DeepDrawdown(PaperBroker):
        def get_account_balance(self):
            snap = super().get_account_balance()
            snap.drawdown = 0.20
            return snap

    mem = Memory(tmp_path / "m.db")
    df = frame([(100, 101, 99, 100)] * 3)
    switch = KillSwitch(tmp_path / "kill.json")
    b = DeepDrawdown(df, symbol="BTC/USDT", cash=10_000.0, gate=RiskGate(), kill_switch=switch, memory=mem)
    order = b.submit_order(buy(4.0, stop=-0.01))
    assert order.status is OrderStatus.REJECTED
    assert "sealed 15.0% limit" in order.reject_reason
    assert switch.is_blocked()
    assert [e["kind"] for e in mem.risk_events()] == ["order_blocked"]

    other = b.submit_order(buy(1.0, stop=-0.01, client_id="second"))
    assert other.status is OrderStatus.REJECTED
    assert "kill switch is engaged" in other.reject_reason

    # The alert that follows this halt has to say *with how much room*, and that margin
    # only exists at this instant: the switch kept it, so `notify` can quote it later.
    detail = switch.describe()["detail"]
    assert detail["drawdown"] == 0.20
    assert detail["alerts"] and "limit breached" in detail["alerts"][0]
    checks = {c["name"]: c for c in detail["checks"]}
    assert set(checks) == {"max_drawdown", "max_daily_loss"}   # only the halting ones
    assert checks["max_drawdown"]["ok"] is False
    assert checks["max_drawdown"]["headroom"] == pytest.approx(-5.0)
    assert checks["max_daily_loss"]["ok"] is True
    assert checks["max_daily_loss"]["headroom"] > 0             # the margin that remained


def test_gate_is_optional_but_documented():
    b = broker([(100, 101, 99, 100)] * 2)
    assert b.gate is None
    assert b.submit_order(buy(1.0)).status is OrderStatus.NEW


# --------------------------------------------------------------------------
# the kill switch
# --------------------------------------------------------------------------

def test_kill_switch_starts_clear_and_blocks_when_armed(tmp_path):
    switch = KillSwitch(tmp_path / "k.json")
    assert switch.is_blocked() is False
    state = switch.trigger("daily loss limit breached", by="risk_gate")
    assert state["blocked"] is True
    assert switch.is_blocked() is True
    info = switch.describe()
    assert info["reason"] == "daily loss limit breached"
    assert info["triggered_by"] == "risk_gate"
    assert info["events"] == 1


def test_kill_switch_keeps_the_first_cause_when_triggered_again(tmp_path):
    """A second breach must not bury the reason that stopped trading first."""
    switch = KillSwitch(tmp_path / "k.json")
    switch.trigger("daily loss limit breached", by="risk_gate")
    state = switch.trigger("something else too", by="risk_gate")
    assert state["reason"] == "daily loss limit breached"
    assert [h["event"] for h in state["history"]] == ["trigger", "trigger_again"]
    assert state["history"][1]["reason"] == "something else too"


def test_kill_switch_survives_a_restart(tmp_path):
    path = tmp_path / "k.json"
    KillSwitch(path).trigger("operator stopped the bot", by="operator")
    assert KillSwitch(path).is_blocked() is True  # a new process, same file
    assert json.loads(path.read_text(encoding="utf-8"))["blocked"] is True


def test_kill_switch_fails_safe_on_a_corrupt_file(tmp_path):
    path = tmp_path / "k.json"
    path.write_text("{not json", encoding="utf-8")
    switch = KillSwitch(path)
    assert switch.is_blocked() is True
    assert "unreadable" in switch.describe()["reason"]
    assert switch.describe()["triggered_by"] == "fail-safe"


def test_kill_switch_reset_is_explicit_and_audited(tmp_path):
    switch = KillSwitch(tmp_path / "k.json")
    switch.trigger("test")
    state = switch.reset("reviewed by the operator")
    assert state["blocked"] is False
    assert switch.is_blocked() is False
    assert [h["event"] for h in state["history"]] == ["trigger", "reset"]
    with pytest.raises(Exception):
        switch.trigger("")


def test_arming_detail_keeps_the_halting_limits_and_nothing_else():
    """The numbers the gate saw: scalars and flat rows, not prose or nested blobs."""

    class Snap:
        equity = 8_456.0
        drawdown = 0.2387
        day_pnl_pct = -0.003
        ts = "2026-01-01T00:00:00+00:00"

    checks = [
        {"name": "max_drawdown", "value": 23.87, "limit": 15.0, "headroom": -8.87,
         "headroom_frac": -0.59, "unit": "pp", "ok": False, "arms": True, "reason": "prose"},
        {"name": "position_pct", "value": 0.0, "limit": 5.0, "headroom": 5.0,
         "headroom_frac": 1.0, "unit": "pp", "ok": True, "arms": False},
    ]
    detail = arming_detail(Snap(), checks, alerts=["drawdown limit breached: new orders halted"])

    assert [c["name"] for c in detail["checks"]] == ["max_drawdown"]
    assert "reason" not in detail["checks"][0]        # the numbers, not the wording
    assert detail["alerts"] == ["drawdown limit breached: new orders halted"]
    assert (detail["equity"], detail["drawdown"], detail["day_pnl_pct"]) == (
        8_456.0, 0.2387, -0.003
    )
    assert detail["as_of"] == "2026-01-01T00:00:00+00:00"
    assert arming_detail(None, None) == {
        "alerts": [], "equity": None, "drawdown": None, "day_pnl_pct": None,
        "as_of": "", "checks": [],
    }


def test_kill_switch_keeps_the_numbers_the_gate_saw(tmp_path):
    """Only scalars and one flat list survive, the first cause keeps its numbers, and a
    reset forgets them: what was left at the refusal cannot be reconstructed later."""
    switch = KillSwitch(tmp_path / "k.json")
    state = switch.trigger(
        "drawdown limit breached",
        by="risk_gate",
        detail={
            "drawdown": 0.2387,
            "equity": 8_456.0,
            "checks": [{"name": "max_drawdown", "headroom": -8.87, "ok": False}],
            "nested": {"not": "a scalar"},
            "long": "x" * 500,
        },
    )

    assert state["detail"]["drawdown"] == 0.2387
    assert state["detail"]["checks"][0]["headroom"] == -8.87
    assert "nested" not in state["detail"]
    assert len(state["detail"]["long"]) == 200
    # a second trigger keeps the *first* halt's numbers, like it keeps the first reason
    again = switch.trigger("something else too", by="risk_gate", detail={"drawdown": 0.5})
    assert again["detail"]["drawdown"] == 0.2387
    assert switch.reset("reviewed by the operator")["detail"] == {}


def test_a_switch_file_written_before_the_numbers_existed_has_none(tmp_path):
    """An older state file must read as "no numbers", not as an error."""
    path = tmp_path / "k.json"
    path.write_text(
        json.dumps({"blocked": True, "reason": "old halt", "triggered_by": "risk_gate",
                    "triggered_at": "2026-01-01T00:00:00+00:00", "history": []}),
        encoding="utf-8",
    )
    info = KillSwitch(path).describe()
    assert info["blocked"] is True and info["reason"] == "old halt"
    assert info["detail"] == {}


def test_kill_switch_path_follows_the_session_file(tmp_path):
    class S:
        session_file = str(tmp_path / "state" / "session.json")

    assert kill_switch_path(S()) == tmp_path / "state" / "kill_switch.json"
    assert kill_switch_path(None) == Path("state") / "kill_switch.json"


# --------------------------------------------------------------------------
# risk events in the database
# --------------------------------------------------------------------------

def test_risk_events_are_stored_and_filterable(tmp_path):
    mem = Memory(tmp_path / "m.db")
    mem.add_risk_event(7, "order_blocked", "too big", {"checks": [1, 2]})
    mem.add_risk_event(7, "kill_switch", "daily loss")
    events = mem.risk_events()
    assert [e["kind"] for e in events] == ["kill_switch", "order_blocked"]  # newest first
    assert events[1]["detail"] == {"checks": [1, 2]}
    assert events[1]["run_id"] == 7
    assert [e["kind"] for e in mem.risk_events(kind="order_blocked")] == ["order_blocked"]
    assert len(mem.risk_events(limit=1)) == 1
    assert mem.stats()  # the older tables still work


def test_missing_memory_does_not_break_a_rejection(tmp_path):
    """Auditing must never be the thing that breaks execution."""
    b = broker([(100, 101, 99, 100)] * 2, gate=RiskGate(), memory=None)
    assert b.submit_order(buy(60.0, stop=-0.01)).status is OrderStatus.REJECTED
