import numpy as np
import pandas as pd
import pytest

from selftrade.backtest import BacktestResult, max_drawdown, simulate
from selftrade.data import make_synthetic
from selftrade.genome import validate_genome
from selftrade.strategies import generate_signals


def bars(rows):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range("2021-01-01", periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(
        volume=1.0
    )


def sig(n, long_entry=None, long_exit=None, short_entry=None, short_exit=None):
    idx = pd.date_range("2021-01-01", periods=n, freq="1h", tz="UTC")
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


GEN = validate_genome(
    {
        "family": "rsi_reversion",
        "params": {},
        "risk": {"stop_loss_pct": -0.10, "take_profit_pct": None, "trailing_pct": None, "trade_shorts": False},
    }
)


def test_fill_at_next_open_with_entry_cost():
    df = bars([(100, 101, 99, 100), (110, 112, 108, 111), (111, 113, 110, 112), (112, 114, 111, 113)])
    signals = sig(4, long_entry=[True, False, False, False], long_exit=[False, False, True, False])
    res = simulate(df, GEN, signals=signals, fee_bps=10.0, slippage_bps=0.0, initial_cash=10000, position_size=1.0)
    t = res.trades.iloc[0]
    # Entry at open of bar 1 (110), with 10bps cost against us.
    assert t["entry_price"] == pytest.approx(110 * 1.001)
    # Exit at open of bar 3 (112) with cost against us.
    assert t["exit_price"] == pytest.approx(112 * 0.999)
    assert t["exit_reason"] == "signal"


def test_costs_applied_both_sides():
    df = bars([(100, 101, 99, 100)] * 2 + [(100, 101, 99, 100)] * 2)
    signals = sig(4, long_entry=[True, False, False, False], long_exit=[False, True, False, False])
    res = simulate(df, GEN, signals=signals, fee_bps=20.0, slippage_bps=0.0, position_size=1.0, initial_cash=10000)
    # Flat prices: the only PnL is the two-sided cost.
    assert res.trades.iloc[0]["pnl"] < 0
    assert res.metrics["total_return"] < 0


def test_stop_fires_before_target_same_bar():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {},
            "risk": {"stop_loss_pct": -0.05, "take_profit_pct": 0.05, "trailing_pct": None, "trade_shorts": False},
        }
    )
    df = bars(
        [
            (100, 101, 99, 100),   # signal bar
            (100, 101, 99, 100),   # entry at 100
            (100, 120, 80, 100),   # both stop (~95) and target (~105) touched
        ]
    )
    signals = sig(3, long_entry=[True, False, False])
    res = simulate(df, g, signals=signals, fee_bps=0.0, slippage_bps=0.0, position_size=1.0)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(95.0)


def test_gap_through_stop_fills_at_open():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {},
            "risk": {"stop_loss_pct": -0.05, "take_profit_pct": None, "trailing_pct": None, "trade_shorts": False},
        }
    )
    df = bars(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),   # entry at 100, stop at 95
            (90, 92, 88, 90),      # gaps below the stop -> fill at open 90
        ]
    )
    signals = sig(3, long_entry=[True, False, False])
    res = simulate(df, g, signals=signals, fee_bps=0.0, slippage_bps=0.0, position_size=1.0)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(90.0)


def test_take_profit_fills_at_level():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {},
            "risk": {"stop_loss_pct": -0.05, "take_profit_pct": 0.05, "trailing_pct": None, "trade_shorts": False},
        }
    )
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 110, 99, 108)])
    signals = sig(3, long_entry=[True, False, False])
    res = simulate(df, g, signals=signals, fee_bps=0.0, slippage_bps=0.0, position_size=1.0)
    assert res.trades.iloc[0]["exit_reason"] == "target"
    assert res.trades.iloc[0]["exit_price"] == pytest.approx(105.0)


def test_trailing_stop_locks_profit():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {},
            "risk": {"stop_loss_pct": -0.20, "take_profit_pct": None, "trailing_pct": 0.05, "trade_shorts": False},
        }
    )
    df = bars(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),   # entry 100
            (100, 120, 99, 119),   # trail ref -> 120
            (119, 120, 110, 111),  # trailing stop at 120*0.95 = 114, low 110 hits
        ]
    )
    signals = sig(4, long_entry=[True, False, False, False])
    res = simulate(df, g, signals=signals, fee_bps=0.0, slippage_bps=0.0, position_size=1.0)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(114.0)


def test_end_of_data_not_a_closed_trade():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    signals = sig(3, long_entry=[True, False, False])
    res = simulate(df, GEN, signals=signals, fee_bps=0.0, slippage_bps=0.0)
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["exit_reason"] == "end_of_data"
    assert res.metrics["n_trades"] == 0


def test_shorts_need_both_switches():
    g = validate_genome(
        {"family": "rsi_reversion", "params": {}, "risk": {"trade_shorts": True, "stop_loss_pct": -0.1}}
    )
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    signals = sig(3, short_entry=[True, False, False])
    # allow_short False -> no short taken
    res = simulate(df, g, signals=signals, allow_short=False)
    assert len(res.trades) == 0
    # allow_short True -> short taken
    res2 = simulate(df, g, signals=signals, allow_short=True)
    assert len(res2.trades) == 1
    assert res2.trades.iloc[0]["direction"] == "short"


def test_short_stop_is_above_entry():
    g = validate_genome(
        {"family": "rsi_reversion", "params": {}, "risk": {"trade_shorts": True, "stop_loss_pct": -0.05}}
    )
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 110, 99, 105)])
    signals = sig(3, short_entry=[True, False, False])
    res = simulate(df, g, signals=signals, allow_short=True, fee_bps=0.0, slippage_bps=0.0, position_size=1.0)
    t = res.trades.iloc[0]
    assert t["direction"] == "short"
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(105.0)


def test_metrics_on_synthetic():
    df = make_synthetic(3000, seed=9)
    g = validate_genome({"family": "trend_momentum", "params": {}})
    res = simulate(df, g, initial_cash=10000, position_size=0.95)
    m = res.metrics
    assert m["n_trades"] >= 1
    assert 0 <= m["max_drawdown"] <= 1
    assert 0 <= m["win_rate"] <= 1
    assert np.isfinite(m["sharpe"])
    assert m["final_equity"] > 0


def test_no_lookahead_trades_unchanged():
    """Tampering with future prices must not change past trades."""
    df = make_synthetic(2000, seed=13)
    g = validate_genome({"family": "donchian_breakout", "params": {}})
    cut = 1200
    full = simulate(df, g, fee_bps=0, slippage_bps=0)
    tampered = df.copy()
    tampered.iloc[cut:] = tampered.iloc[cut:] * 1.7 + 30.0
    after = simulate(tampered, g, fee_bps=0, slippage_bps=0)
    full_past = full.trades[full.trades["exit_time"] < df.index[cut]]
    after_past = after.trades[after.trades["exit_time"] < df.index[cut]]
    assert len(full_past) == len(after_past)
    for a, b in zip(full_past.itertuples(), after_past.itertuples()):
        assert a.entry_price == pytest.approx(b.entry_price)
        assert a.exit_price == pytest.approx(b.exit_price)
        assert a.exit_reason == b.exit_reason


def test_position_size_controls_exposure():
    df = make_synthetic(2000, seed=3)
    g = validate_genome({"family": "trend_momentum", "params": {}})
    small = simulate(df, g, position_size=0.25)
    big = simulate(df, g, position_size=1.0)
    assert abs(big.metrics["total_return"]) > abs(small.metrics["total_return"])


def test_max_drawdown_basic():
    eq = pd.Series([100, 120, 90, 95], dtype="float64")
    assert max_drawdown(eq) == pytest.approx(0.25)
