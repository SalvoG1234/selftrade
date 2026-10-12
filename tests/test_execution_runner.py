"""The forward session is the same engine, driven by orders.

``backtest.simulate`` books its own fills; ``execution.runner.run_orders`` submits
orders to the simulated broker instead.  If the two disagree, the paper session is
measuring something other than the strategy, so this file pins the agreement:

* a signal at the close of bar ``i`` becomes an order that fills at the **open of bar
  ``i + 1``**, sized as the configured fraction of equity at the signal bar;
* the exit plan (stop, take profit, trailing) is evaluated intrabar with the same
  ladder: a gap through the stop fills at the worse open, the stop is checked before
  the target, the trailing level follows the best price seen;
* a position still open at the last bar is reported as ``end_of_data`` and is not a
  closed trade;
* and all of it still passes through the risk gate: a genome the gate refuses cannot
  trade at all, which is asserted here rather than assumed.

Prices, times, directions, ``bars_held`` and exit reasons are compared **exactly** -
they depend only on the bars.  Sizes, PnL, returns and equity are compared to
``rel=1e-9``: the engine adds ``pnl`` to a running equity while the broker moves cash
in and out, and the two sums differ by a few ULPs.  That is the whole difference, and
it is why the session's numbers are the engine's numbers to 15 significant digits.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selftrade.backtest import simulate
from selftrade.data import make_synthetic
from selftrade.execution import KillSwitch, RiskGate, run_orders
from selftrade.genome import sample_genome, validate_genome
from selftrade.memory import Memory

COST = (5.0 + 2.0) / 10_000.0

#: The number every comparison here is run at: the session's effective size, the
#: fee/slippage the config ships, and one symbol that the gate allows.
KW: dict = {
    "initial_cash": 10_000.0,
    "fee_bps": 5.0,
    "slippage_bps": 2.0,
    "position_size": 0.05,
    "allow_short": False,
    "timeframe": "4h",
}

BASE = sample_genome("trend_momentum", np.random.default_rng(3))


def gen(stop: float = -0.03, tp=None, trail=None, shorts: bool = False) -> dict:
    """A genome of the sampled family with an exit plan the sealed layer allows."""
    return validate_genome(
        {
            **BASE,
            "risk": {
                "stop_loss_pct": stop,
                "take_profit_pct": tp,
                "trailing_pct": trail,
                "trade_shorts": shorts,
            },
        }
    )


def bars(rows, start: str = "2021-01-01", freq: str = "4h") -> pd.DataFrame:
    """OHLCV from explicit ``(open, high, low, close)`` tuples."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(
        volume=1.0
    )


def sig(n, long_entry=None, long_exit=None, short_entry=None, short_exit=None) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=n, freq="4h", tz="UTC")
    z = [False] * n
    return pd.DataFrame(
        {
            "long_entry": long_entry or z,
            "long_exit": long_exit or z,
            "short_entry": short_entry or z,
            "short_exit": short_exit or z,
        },
        index=idx,
    )


def run_both(df, genome, **overrides):
    """The bar engine and the order path over the same frame, same everything."""
    kw = {**KW, **overrides}
    ref = simulate(df, genome, **kw)
    got = run_orders(df, genome, symbol="BTC/USDT", **kw)
    return ref, got


def assert_same(ref, got) -> None:
    """The engine's trades and equity, exactly where they can be."""
    rt = ref.trades.reset_index(drop=True)
    gt = got.trades.reset_index(drop=True)
    assert len(rt) == len(gt), f"{len(rt)} bar-engine trades vs {len(gt)} order trades"
    for col in ("direction", "exit_reason"):
        assert list(rt[col]) == list(gt[col]), col
    for col in ("entry_time", "exit_time"):
        assert [str(x) for x in rt[col]] == [str(x) for x in gt[col]], col
    assert list(rt["bars_held"]) == list(gt["bars_held"])
    # Prices come from the bars and the cost model only: bit for bit identical.
    for col in ("entry_price", "exit_price"):
        assert list(rt[col]) == list(gt[col]), col
    for col in ("size", "pnl", "return_pct"):
        assert list(rt[col]) == pytest.approx(list(gt[col]), rel=1e-9), col
    assert ref.metrics["n_trades"] == got.metrics["n_trades"]
    assert got.metrics["total_return"] == pytest.approx(ref.metrics["total_return"], rel=1e-9)
    assert list(ref.equity) == pytest.approx(list(got.equity), rel=1e-9)
    assert len(ref.equity) == len(got.equity)


# --------------------------------------------------------------------------
# equivalence with the bar engine
# --------------------------------------------------------------------------

def test_orders_reproduce_the_bar_engine():
    """Same genome, same bars: same trades - and nothing refused on the way."""
    win = make_synthetic(3000, seed=55).iloc[-1200:]
    gate = RiskGate()
    ref = simulate(win, gen(), **KW)
    got = run_orders(win, gen(), symbol="BTC/USDT", gate=gate, **KW)

    assert len(ref.trades) > 5  # otherwise "identical" would mean "identically flat"
    assert got.blocked == []  # a compliant genome never trips the gate
    assert_same(ref, got)


def test_orders_reproduce_the_engine_with_target_and_trailing():
    win = make_synthetic(3000, seed=55).iloc[-1500:]
    for genome in (gen(tp=0.03), gen(trail=0.04), gen(tp=0.08, trail=0.03, shorts=True)):
        ref, got = run_both(win, genome, allow_short=True)
        assert len(ref.trades) > 0
        assert_same(ref, got)

    # ...and the exits really were exercised, not inert parameters.  (The trailing
    # level is pinned on hand-built bars below; here it is the agreement that matters.)
    ref, _ = run_both(win, gen(tp=0.03), allow_short=True)
    assert "target" in set(ref.trades["exit_reason"])
    ref, _ = run_both(win, gen(stop=-0.01), allow_short=True)
    assert "stop" in set(ref.trades["exit_reason"])


def test_the_entry_fills_at_the_next_open_and_sizes_from_equity():
    win = make_synthetic(3000, seed=55).iloc[-600:]
    got = run_orders(win, gen(), symbol="BTC/USDT", **KW)

    first = got.fills[0]
    bar = win.index.get_loc(pd.Timestamp(first["ts"]))
    assert first["side"] == "buy"
    assert first["price"] == pytest.approx(float(win["open"].iloc[bar]) * (1.0 + COST))
    # The quote amount is what the account spends, whatever the price was.
    assert first["qty"] * first["price"] == pytest.approx(KW["position_size"] * KW["initial_cash"])
    # ...and it was decided at the bar before, exactly as the engine does it.
    assert got.orders[0]["bar"] == bar - 1
    assert got.orders[0]["quote_qty"] == pytest.approx(KW["position_size"] * KW["initial_cash"])


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    df = bars(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),  # entry at 100.07, stop 5% under it
            (90, 92, 88, 89),  # gapped straight through the stop
            (89, 90, 88, 89),
        ]
    )
    signals = sig(4, long_entry=[True, False, False, False])
    ref, got = run_both(df, gen(stop=-0.05), signals=signals)

    assert_same(ref, got)
    trade = got.trades.iloc[0]
    assert trade["exit_reason"] == "stop"
    assert trade["exit_time"] == df.index[2]  # a real timestamp, like the engine's frame
    assert trade["exit_price"] == pytest.approx(90.0 * (1.0 - COST))  # the open, not 95.07


def test_the_stop_is_checked_before_the_target_in_the_same_bar():
    df = bars(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 120, 90, 110),  # one bar touches both levels
            (110, 111, 109, 110),
        ]
    )
    signals = sig(4, long_entry=[True, False, False, False])
    ref, got = run_both(df, gen(stop=-0.05, tp=0.10), signals=signals)

    assert_same(ref, got)
    assert got.trades.iloc[0]["exit_reason"] == "stop"


def test_the_trailing_stop_follows_the_best_price_seen():
    df = bars(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),  # entry at 100.07
            (100, 120, 119, 119),  # trail reference becomes 120
            (119, 120, 110, 111),  # trailing stop at 120 * 0.95 = 114, low 110 hits it
            (111, 112, 110, 111),
        ]
    )
    signals = sig(5, long_entry=[True, False, False, False, False])
    ref, got = run_both(df, gen(stop=-0.20, trail=0.05), signals=signals)

    assert_same(ref, got)
    trade = got.trades.iloc[0]
    assert trade["exit_reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(120.0 * 0.95 * (1.0 - COST))
    assert trade["pnl"] > 0  # the trailing stop locked the run-up in


def test_a_position_open_at_the_end_is_reported_but_not_counted():
    df = bars([(100, 101, 99, 100), (100, 102, 99, 101), (101, 103, 100, 102)])
    signals = sig(3, long_entry=[True, False, False])
    ref, got = run_both(df, gen(stop=-0.05), signals=signals)

    assert_same(ref, got)
    assert list(got.trades["exit_reason"]) == ["end_of_data"]
    assert got.open_at_end is True
    assert got.metrics["n_trades"] == ref.metrics["n_trades"] == 0
    # It is marked at the last close, not liquidated: the position is still worth
    # something in the equity, and the cash is the money the entry spent.
    assert got.equity.iloc[-1] > KW["initial_cash"]
    assert got.cash < KW["initial_cash"]
    # ...while the reported (uncounted) end_of_data exit does pay the exit cost.
    assert got.trades.iloc[0]["exit_price"] == pytest.approx(102.0 * (1.0 - COST))


def test_a_run_that_cannot_reach_two_bars_returns_a_flat_result():
    one = bars([(100, 101, 99, 100)])
    result = run_orders(one, gen(), symbol="BTC/USDT", **KW)
    assert list(result.equity) == [KW["initial_cash"]]
    assert result.trades.empty
    assert result.metrics["n_trades"] == 0
    assert result.orders == [] and result.fills == []


# --------------------------------------------------------------------------
# the gate is on the path, not in the documentation
# --------------------------------------------------------------------------

def test_orders_the_gate_refuses_never_fill(tmp_path):
    win = make_synthetic(3000, seed=55).iloc[-600:]
    mem = Memory(tmp_path / "m.db")
    gate = RiskGate(risk_pct=1e-9)  # a per-trade budget nothing can fit into

    got = run_orders(
        win,
        gen(),
        symbol="BTC/USDT",
        gate=gate,
        memory=mem,
        context={"paper_session": 7},
        **KW,
    )

    assert got.fills == []
    assert len(got.trades) == 0
    assert got.blocked and len(got.blocked) == len(got.orders)
    assert all(o["status"] == "rejected" for o in got.blocked)
    assert all("per-trade budget" in o["reason"] for o in got.blocked)
    # ...and each refusal was recorded, attributed to the session that sent it
    events = mem.risk_events(limit=10, kind="order_blocked")
    assert events
    assert events[0]["detail"]["paper_session"] == 7
    assert events[0]["detail"]["request"]["client_id"].startswith("pt-")


def test_the_kill_switch_keeps_the_session_flat(tmp_path):
    win = make_synthetic(3000, seed=55).iloc[-600:]
    switch = KillSwitch(tmp_path / "k.json")
    switch.trigger("operator stopped trading", by="test")

    got = run_orders(win, gen(), symbol="BTC/USDT", gate=RiskGate(), kill_switch=switch, **KW)

    assert got.fills == [] and len(got.trades) == 0
    assert all("kill switch is engaged" in o["reason"] for o in got.blocked)
    assert got.equity.iloc[-1] == pytest.approx(KW["initial_cash"])


def test_the_session_result_reports_what_it_did():
    win = make_synthetic(3000, seed=55).iloc[-600:]
    got = run_orders(win, gen(), symbol="BTC/USDT", **KW)
    summary = got.to_dict()
    assert summary["orders"] == len(got.orders)
    assert summary["fills"] == len(got.fills)
    assert summary["blocked"] == 0
    assert summary["n_trades"] == int(got.metrics["n_trades"])
    assert summary["equity_points"] == len(win)
    assert got.cash > 0
