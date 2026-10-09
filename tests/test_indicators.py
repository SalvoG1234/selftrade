import numpy as np
import pandas as pd
import pytest

from selftrade.data import make_synthetic
from selftrade.indicators import (
    atr,
    bollinger,
    donchian,
    ema,
    realized_vol,
    rsi,
    sma,
    true_range,
)


@pytest.fixture(scope="module")
def df():
    return make_synthetic(600, seed=11)


def _assert_causal(func, df, cut=400):
    """Shifting the future must not change the past."""
    full = func(df)
    tampered = df.copy()
    future = tampered.iloc[cut:]
    tampered.iloc[cut:] = future * 1.5 + 10.0
    changed = func(tampered)
    pd.testing.assert_series_equal(full.iloc[:cut], changed.iloc[:cut])


def test_ema_causal(df):
    _assert_causal(lambda d: ema(d["close"], 20), df)


def test_ema_warmup_is_nan(df):
    out = ema(df["close"], 20)
    assert out.iloc[:19].isna().all()
    assert not np.isnan(out.iloc[19])


def test_sma_matches_manual(df):
    out = sma(df["close"], 10)
    manual = df["close"].iloc[10:20].mean()
    assert out.iloc[19] == pytest.approx(manual)


def test_rsi_causal(df):
    _assert_causal(lambda d: rsi(d["close"], 14), df)


def test_rsi_bounds(df):
    out = rsi(df["close"], 14).dropna()
    assert out.between(0, 100).all()


def test_rsi_all_gain_is_100():
    s = pd.Series(np.arange(1, 60, dtype="float64"))
    out = rsi(s, 14)
    assert out.iloc[-1] == pytest.approx(100.0)


def test_rsi_all_loss_is_zero():
    s = pd.Series(np.arange(60, 1, -1, dtype="float64"))
    out = rsi(s, 14)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_atr_causal(df):
    _assert_causal(lambda d: atr(d, 14), df)


def test_true_range_first_bar(df):
    tr = true_range(df)
    # First bar has no previous close: TR is the bar range by convention.
    assert tr.iloc[0] == pytest.approx(df["high"].iloc[0] - df["low"].iloc[0])
    assert tr.iloc[1] >= df["high"].iloc[1] - df["low"].iloc[1] - 1e-9


def test_bollinger_causal(df):
    def f(d):
        lo, mid, up = bollinger(d["close"], 20, 2.0)
        return mid

    _assert_causal(f, df)


def test_bollinger_ordering(df):
    lo, mid, up = bollinger(df["close"], 20, 2.0)
    valid = mid.dropna().index
    assert (up.loc[valid] >= mid.loc[valid]).all()
    assert (mid.loc[valid] >= lo.loc[valid]).all()


def test_donchian_causal(df):
    _assert_causal(lambda d: donchian(d, 20)[2], df)


def test_donchian_bounds(df):
    lo, mid, up = donchian(df, 20)
    valid = up.dropna().index
    assert (up.loc[valid] >= mid.loc[valid]).all()
    assert (mid.loc[valid] >= lo.loc[valid]).all()
    # Upper touches the max high within the window.
    i = valid[0]
    pos = df.index.get_loc(i)
    window = df["high"].iloc[pos - 19 : pos + 1]
    assert up.loc[i] == pytest.approx(window.max())


def test_realized_vol_causal(df):
    _assert_causal(lambda d: realized_vol(d["close"], 20), df)


def test_realized_vol_nonneg(df):
    out = realized_vol(df["close"], 20).dropna()
    assert (out >= 0).all()
