import numpy as np
import pandas as pd
import pytest

import requests

from selftrade.data import (
    DataError,
    TIMEFRAME_BARS_PER_YEAR,
    ensure_ohlcv,
    fetch_klines,
    load_csv,
    load_freqtrade_json,
    make_synthetic,
    normalize_symbol,
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


# ------------------------------------------------------- real public klines

STEP_MS = 3_600_000
BASE_MS = 1_600_000_000_000


def kline(open_ms, price, volume=1.0):
    return [
        open_ms,
        price,
        price + 1.0,
        price - 1.0,
        price + 0.5,
        volume,
        open_ms + STEP_MS - 1,
        volume * price,
        5,
        volume,
        volume * price,
        "0",
    ]


def kline_page(start_ms, n, price0=100.0):
    return [kline(start_ms + i * STEP_MS, price0 + i) for i in range(n)]


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _StubSession:
    """Records every request and replays canned kline pages in order."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        page = self.pages.pop(0) if self.pages else []
        if isinstance(page, _Resp):
            return page
        if isinstance(page, Exception):
            raise page
        return _Resp(page)


def test_normalize_symbol():
    assert normalize_symbol("BTC/USDT") == "BTCUSDT"
    assert normalize_symbol(" btc-usdt ") == "BTCUSDT"
    assert normalize_symbol("EURUSD") == "EURUSD"
    with pytest.raises(DataError):
        normalize_symbol("BTC USDT")
    with pytest.raises(DataError):
        normalize_symbol("")


def test_fetch_klines_contract_from_a_stub_server():
    page = kline_page(BASE_MS, 300)
    session = _StubSession(page)
    df = fetch_klines("BTC/USDT", "4h", 300, session=session)

    assert len(df) == 300
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing
    assert (df.dtypes == "float64").all()
    assert df["close"].iloc[0] == pytest.approx(100.5)
    assert df["close"].iloc[-1] == pytest.approx(399.5)
    assert df["volume"].sum() > 0

    assert len(session.calls) == 1
    assert session.calls[0]["url"].endswith("/api/v3/klines")
    assert session.calls[0]["params"] == {
        "symbol": "BTCUSDT",
        "interval": "4h",
        "limit": 300,
    }


def test_fetch_klines_paginates_backwards_and_trims():
    bars = kline_page(BASE_MS, 3000)
    session = _StubSession(bars[2000:3000], bars[1000:2000], bars[500:1000])
    df = fetch_klines("BTCUSDT", "1h", 2500, session=session)

    assert len(df) == 2500
    assert len(session.calls) == 3
    assert session.calls[0]["params"]["limit"] == 1000
    assert "endTime" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["endTime"] == bars[2000][0] - 1
    assert session.calls[2]["params"]["endTime"] == bars[1000][0] - 1
    assert session.calls[2]["params"]["limit"] == 500

    # The oldest bar of the oldest page first, the newest bar last.
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates
    assert df["open"].iloc[0] == pytest.approx(bars[500][1])
    assert df["open"].iloc[-1] == pytest.approx(bars[2999][1])


def test_fetch_klines_stops_when_history_is_exhausted():
    session = _StubSession(kline_page(BASE_MS, 10))
    df = fetch_klines("BTCUSDT", "1h", 5000, session=session)
    assert len(df) == 10
    assert len(session.calls) == 1


def test_fetch_klines_drops_malformed_rows():
    page = kline_page(BASE_MS, 3) + [[1, 2], "junk"]
    session = _StubSession(page)
    df = fetch_klines("BTCUSDT", "1h", 3, session=session)
    assert len(df) == 3


def test_fetch_klines_empty_payload_raises():
    with pytest.raises(DataError) as exc:
        fetch_klines("BTCUSDT", "1h", 10, session=_StubSession([]))
    assert "no candles" in str(exc.value)


def test_fetch_klines_upstream_error_object_raises():
    session = _StubSession({"code": -1121, "msg": "Invalid symbol."})
    with pytest.raises(DataError) as exc:
        fetch_klines("NOTACOIN", "1h", 10, session=session)
    assert "Invalid symbol" in str(exc.value)


def test_fetch_klines_http_error_raises():
    session = _StubSession([])
    session.get = lambda url, params=None, timeout=None: _Resp({}, status=400)
    with pytest.raises(DataError) as exc:
        fetch_klines("BTCUSDT", "1h", 10, session=session)
    assert "HTTP 400" in str(exc.value)


def test_fetch_klines_network_error_raises():
    session = _StubSession(requests.ConnectionError("no route to host"))
    with pytest.raises(DataError) as exc:
        fetch_klines("BTCUSDT", "1h", 10, session=session)
    assert "network error" in str(exc.value)


def test_fetch_klines_non_json_body_raises():
    session = _StubSession(_Resp(ValueError("not json")))
    with pytest.raises(DataError) as exc:
        fetch_klines("BTCUSDT", "1h", 10, session=session)
    assert "non-JSON" in str(exc.value)


def test_fetch_klines_rejects_bad_arguments():
    with pytest.raises(DataError):
        fetch_klines("BTCUSDT", "1h", 0, session=_StubSession([]))
    with pytest.raises(DataError) as exc:
        fetch_klines("BTCUSDT", "7m", 10, session=_StubSession([]))
    assert "cannot be fetched" in str(exc.value)
    with pytest.raises(DataError):
        fetch_klines("BTC USDT", "1h", 10, session=_StubSession([]))
