import numpy as np
import pandas as pd
import pytest

from selftrade.data import (
    DataError,
    TIMEFRAME_BARS_PER_YEAR,
    ensure_ohlcv,
    load_csv,
    load_freqtrade_json,
    make_synthetic,
    split_holdout,
)


def frame(n=50, start="2020-01-01", freq="1h"):
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    close = np.linspace(100, 110, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.ones(n),
        },
        index=idx,
    )


def test_make_synthetic_contract():
    df = make_synthetic(2000, seed=1)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert (df.dtypes == "float64").all()
    assert (df["high"] >= df["low"]).all()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()


def test_synthetic_deterministic():
    a = make_synthetic(500, seed=42)
    b = make_synthetic(500, seed=42)
    pd.testing.assert_frame_equal(a, b)


def test_ensure_rejects_empty():
    with pytest.raises(DataError):
        ensure_ohlcv(pd.DataFrame())


def test_ensure_rejects_missing_columns():
    df = frame().drop(columns=["volume"])
    with pytest.raises(DataError) as exc:
        ensure_ohlcv(df)
    assert "volume" in str(exc.value)


def test_ensure_rejects_non_datetime_index():
    df = frame().reset_index(drop=True)
    with pytest.raises(DataError):
        ensure_ohlcv(df)


def test_ensure_rejects_high_below_low():
    df = frame()
    df.iloc[3, df.columns.get_loc("high")] = df.iloc[3]["low"] - 5
    with pytest.raises(DataError):
        ensure_ohlcv(df)


def test_ensure_rejects_high_below_close():
    df = frame()
    df.iloc[3, df.columns.get_loc("high")] = df.iloc[3]["close"] - 0.5
    with pytest.raises(DataError):
        ensure_ohlcv(df)


def test_ensure_rejects_low_above_open():
    df = frame()
    df.iloc[3, df.columns.get_loc("low")] = df.iloc[3]["open"] + 5
    with pytest.raises(DataError):
        ensure_ohlcv(df)


def test_ensure_min_rows():
    with pytest.raises(DataError):
        ensure_ohlcv(frame(10), min_rows=100)


def test_ensure_dedup_keeps_last():
    df = frame(10)
    dup = pd.concat(
        [
            df,
            df.iloc[[4]].assign(
                close=lambda d: d["close"] + 99,
                high=lambda d: d["high"] + 200,
            ),
        ]
    )
    out = ensure_ohlcv(dup, min_rows=1, warn_spacing=False)
    assert len(out) == 10
    idx = df.index[4]
    assert out.loc[idx, "close"] == df.iloc[4]["close"] + 99


def test_ensure_volume_nan_becomes_zero():
    df = frame(10)
    df.iloc[2, df.columns.get_loc("volume")] = np.nan
    out = ensure_ohlcv(df, warn_spacing=False)
    assert out.iloc[2]["volume"] == 0.0


def test_ensure_naive_index_localized_utc():
    df = frame(10)
    df.index = df.index.tz_localize(None)
    out = ensure_ohlcv(df, warn_spacing=False)
    assert str(out.index.tz) == "UTC"


def test_ensure_warns_on_wide_spacing():
    df = frame(20, freq="1D")
    with pytest.warns(UserWarning):
        ensure_ohlcv(df, timeframe="1h")


def test_csv_roundtrip(tmp_path):
    df = make_synthetic(300, seed=3)
    p = tmp_path / "d.csv"
    out = df.copy()
    out.index.name = "timestamp"
    out.to_csv(p)
    loaded = load_csv(p, min_rows=100, timeframe="1h")
    pd.testing.assert_frame_equal(loaded, df, check_freq=False)


def test_csv_missing_datetime_col(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(DataError):
        load_csv(p, datetime_col="timestamp")


def test_freqtrade_json_list_form(tmp_path):
    n = 40
    base = 1609459200000  # 2021-01-01 in ms
    rows = [[base + i * 3600000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10] for i in range(n)]
    p = tmp_path / "ft.json"
    p.write_text(__import__("json").dumps(rows), encoding="utf-8")
    df = load_freqtrade_json(p, min_rows=10, timeframe="1h")
    assert len(df) == n
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"


def test_freqtrade_json_dict_form(tmp_path):
    rows = [
        {"time": 1609459200000 + i * 3600000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 5}
        for i in range(15)
    ]
    p = tmp_path / "ft2.json"
    p.write_text(__import__("json").dumps(rows), encoding="utf-8")
    df = load_freqtrade_json(p, min_rows=10)
    assert len(df) == 15


def test_split_holdout():
    df = make_synthetic(1000, seed=5)
    dev, hold = split_holdout(df, 0.2)
    assert len(hold) == 200
    assert len(dev) == 800
    assert hold.index[0] > dev.index[-1]


def test_split_holdout_zero():
    df = make_synthetic(100, seed=5)
    dev, hold = split_holdout(df, 0.0)
    assert len(hold) == 0
    assert len(dev) == 100


def test_bars_per_year_table():
    assert TIMEFRAME_BARS_PER_YEAR["1h"] == pytest.approx(8760)
    assert TIMEFRAME_BARS_PER_YEAR["1d"] == pytest.approx(365)
