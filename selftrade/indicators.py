"""Causal technical indicators.

Every indicator at bar ``i`` uses only data from bars ``<= i``.  A signal taken
at the close of bar ``i`` is therefore valid to fill at the open of bar ``i+1``.

NaN is returned during the warm-up window rather than a made-up value; the
strategies treat NaN as "no signal".
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (causal, ``adjust=False``)."""
    period = max(1, int(period))
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    period = max(1, int(period))
    return series.rolling(period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    period = max(1, int(period))
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing == EMA with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # All-gain window (avg_loss == 0) -> RSI 100.
    out = out.where(~((avg_loss == 0.0) & (avg_gain > 0.0)), 100.0)
    out = out.where(~((avg_loss == 0.0) & (avg_gain == 0.0)), 50.0)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    a = df["high"] - df["low"]
    b = (df["high"] - prev_close).abs()
    c = (df["low"] - prev_close).abs()
    tr = pd.concat([a, b, c], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    period = max(1, int(period))
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def bollinger(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (lower, middle, upper) Bollinger bands."""
    period = max(1, int(period))
    mid = series.rolling(period, min_periods=period).mean()
    sd = series.rolling(period, min_periods=period).std(ddof=0)
    return mid - num_std * sd, mid, mid + num_std * sd


def donchian(
    df: pd.DataFrame, period: int = 20
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (lower, middle, upper) Donchian channel over the last ``period`` bars.

    The current bar is included: at the close of bar ``i`` bars ``<= i`` are all
    complete, which is causal.
    """
    period = max(1, int(period))
    upper = df["high"].rolling(period, min_periods=period).max()
    lower = df["low"].rolling(period, min_periods=period).min()
    return lower, (upper + lower) / 2.0, upper


def realized_vol(series: pd.Series, period: int = 20) -> pd.Series:
    """Rolling standard deviation of log returns."""
    period = max(1, int(period))
    ret = np.log(series / series.shift(1))
    return ret.rolling(period, min_periods=period).std(ddof=0)
