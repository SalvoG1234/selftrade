"""Data loading and the strict OHLCV contract.

Every loader funnels through :func:`ensure_ohlcv`, so the rest of the pipeline
never has to care where prices came from (CSV, freqtrade JSON, synthetic,
MetaTrader 5).
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

# Timeframe -> nominal bar spacing and number of bars per year.
TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "12h": 720,
    "1d": 1440,
    "1w": 10080,
}

TIMEFRAME_BARS_PER_YEAR: dict[str, float] = {
    tf: (365.0 * 24.0 * 60.0) / mins for tf, mins in TIMEFRAME_MINUTES.items()
}

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataError(Exception):
    """Raised when a dataset does not satisfy the OHLCV contract."""


def _to_utc_index(obj: Any) -> pd.DatetimeIndex:
    """Best-effort conversion of an arbitrary datetime column to a UTC index."""
    if isinstance(obj, pd.DatetimeIndex):
        idx = obj
    elif pd.api.types.is_numeric_dtype(np.asarray(obj)):
        series = pd.Series(np.asarray(obj))
        # Heuristic: ms vs s epochs.
        max_abs = float(series.abs().max()) if len(series) else 0.0
        unit = "ms" if max_abs > 1e11 else "s"
        idx = pd.DatetimeIndex(pd.to_datetime(series, unit=unit, utc=True))
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(obj, utc=True))

    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    return idx


def ensure_ohlcv(
    df: pd.DataFrame,
    *,
    min_rows: int = 0,
    timeframe: str = "1h",
    warn_spacing: bool = True,
) -> pd.DataFrame:
    """Validate and normalise a OHLCV dataframe to the project contract.

    Enforced rules (raise :class:`DataError`):
      * non-empty, with all of ``open, high, low, close, volume``
      * a real datetime index (converted to tz-aware UTC)
      * ``high >= low``; ``high >= max(open, close)``; ``low <= min(open, close)``
      * at least ``min_rows`` rows

    Non-fatal normalisations:
      * duplicate timestamps -> keep the last
      * missing/NaN ``volume`` -> 0
      * bars sorted ascending by time
      * a warning when median bar spacing is far greater than ``timeframe``
    """
    if not isinstance(df, pd.DataFrame):
        raise DataError(f"expected a pandas DataFrame, got {type(df).__name__}")
    if df.empty or len(df) == 0:
        raise DataError("dataset is empty")

    df = df.copy()

    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise DataError(
            f"missing OHLCV column(s): {', '.join(missing)}. "
            f"Required: {', '.join(OHLCV_COLUMNS)}"
        )
    df = df[OHLCV_COLUMNS]

    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataError(
            "index is not a DatetimeIndex. Load with a datetime column "
            "(data.datetime_col) or pass datetime values."
        )
    df.index = _to_utc_index(df.index)
    df.index.name = None
    if df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()

    for col in OHLCV_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

    df["volume"] = df["volume"].fillna(0.0)

    price = df[["open", "high", "low", "close"]]
    if price.isna().any().any():
        bad = price.isna().any(axis=1).sum()
        raise DataError(f"{bad} row(s) have NaN prices")
    if df["volume"].isna().any():
        df["volume"] = df["volume"].fillna(0.0)

    if (df["high"] < df["low"]).any():
        raise DataError("row(s) where high < low")
    bad_high = df["high"] < df[["open", "close"]].max(axis=1)
    if bad_high.any():
        raise DataError("row(s) where high < max(open, close)")
    bad_low = df["low"] > df[["open", "close"]].min(axis=1)
    if bad_low.any():
        raise DataError("row(s) where low > min(open, close)")

    if min_rows and len(df) < min_rows:
        raise DataError(
            f"dataset has {len(df)} rows, below data.min_rows={min_rows}. "
            f"Generate more (data --rows N) or lower data.min_rows."
        )

    df = df[OHLCV_COLUMNS].astype("float64")

    if warn_spacing and len(df) >= 3 and timeframe in TIMEFRAME_MINUTES:
        deltas = df.index.to_series().diff().dropna()
        if len(deltas):
            median_min = float(deltas.median().total_seconds()) / 60.0
            nominal = TIMEFRAME_MINUTES[timeframe]
            if median_min > nominal * 1.5 + 1e-9:
                warnings.warn(
                    f"median bar spacing (~{median_min:.1f} min) is much larger than "
                    f"the configured timeframe '{timeframe}' ({nominal} min). "
                    f"Check data.timeframe or the dataset.",
                    stacklevel=2,
                )
    return df


# --------------------------------------------------------------------------
# Synthetic generator
# --------------------------------------------------------------------------


def make_synthetic(
    rows: int = 12000,
    *,
    seed: int = 7,
    timeframe: str = "1h",
    start: str = "2019-01-01",
    start_price: float = 100.0,
    min_rows: int = 0,
) -> pd.DataFrame:
    """Deterministic regime-switching GBM walk. Synthetic - not market data."""
    rng = np.random.default_rng(seed)
    minutes = TIMEFRAME_MINUTES.get(timeframe, 60)
    idx = pd.date_range(start=start, periods=rows, freq=f"{minutes}min", tz="UTC")

    # Blend drift/vol regimes so the series has trends and chop, giving
    # strategies something to find (and to overfit to - that is the point).
    n_regimes = max(3, rows // 1500)
    bounds = np.linspace(0, rows, n_regimes + 1).astype(int)
    drift = np.zeros(rows)
    vol = np.zeros(rows)
    for i in range(n_regimes):
        a, b = bounds[i], bounds[i + 1]
        d = rng.uniform(-0.06, 0.10) / TIMEFRAME_BARS_PER_YEAR.get(timeframe, 8760.0)
        v = rng.uniform(0.15, 0.55) / np.sqrt(TIMEFRAME_BARS_PER_YEAR.get(timeframe, 8760.0))
        drift[a:b] = d
        vol[a:b] = v

    shocks = rng.standard_normal(rows)
    log_ret = drift + vol * shocks
    close = start_price * np.exp(np.cumsum(log_ret))

    open_ = np.empty(rows)
    open_[0] = start_price
    open_[1:] = close[:-1]
    intrabar = np.abs(rng.standard_normal(rows)) * vol * close
    high = np.maximum(open_, close) + intrabar * rng.uniform(0.2, 1.0, rows)
    low = np.minimum(open_, close) - intrabar * rng.uniform(0.2, 1.0, rows)
    low = np.maximum(low, 1e-6)
    volume = np.abs(rng.standard_normal(rows)) * 1000.0 + 100.0

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    return ensure_ohlcv(df, min_rows=min_rows, timeframe=timeframe, warn_spacing=False)


# --------------------------------------------------------------------------
# File loaders
# --------------------------------------------------------------------------


def load_csv(
    path: str | Path,
    *,
    datetime_col: str = "timestamp",
    min_rows: int = 0,
    timeframe: str = "1h",
    max_rows: int = 0,
) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise DataError(f"CSV not found: {p}")
    df = pd.read_csv(p)
    if datetime_col not in df.columns:
        # Try the index column or the first column.
        candidates = [c for c in df.columns if c.lower() in ("time", "date", "datetime", "timestamp")]
        if candidates:
            datetime_col = candidates[0]
        else:
            raise DataError(
                f"datetime column '{datetime_col}' not found in {p}. "
                f"Columns: {', '.join(map(str, df.columns))}"
            )
    df.index = _to_utc_index(df[datetime_col])
    if max_rows and len(df) > max_rows:
        df = df.iloc[-max_rows:]
    return ensure_ohlcv(df, min_rows=min_rows, timeframe=timeframe)


def load_freqtrade_json(
    path: str | Path,
    *,
    min_rows: int = 0,
    timeframe: str = "1h",
    max_rows: int = 0,
) -> pd.DataFrame:
    """Load a freqtrade ``*.json`` OHLCV export.

    Supports both the list-of-lists form ``[[ts, o, h, l, c, v], ...]`` and the
    list-of-dicts form.
    """
    p = Path(path)
    if not p.exists():
        raise DataError(f"freqtrade JSON not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataError(f"invalid JSON in {p}: {exc}") from exc

    if isinstance(raw, dict):
        for key in ("data", "ohlcv", "candles", "result"):
            if key in raw and isinstance(raw[key], list):
                raw = raw[key]
                break
    if not isinstance(raw, list) or not raw:
        raise DataError(f"{p}: expected a non-empty list of candles")

    if isinstance(raw[0], dict):
        idx = _to_utc_index([r.get("timestamp", r.get("time", r.get("date"))) for r in raw])
        recs = {
            "open": [r["open"] for r in raw],
            "high": [r["high"] for r in raw],
            "low": [r["low"] for r in raw],
            "close": [r["close"] for r in raw],
            "volume": [r.get("volume", 0.0) for r in raw],
        }
    else:
        arr = np.asarray(raw, dtype="float64")
        if arr.ndim != 2 or arr.shape[1] < 6:
            raise DataError(f"{p}: expected rows of [ts, o, h, l, c, v]")
        idx = _to_utc_index(arr[:, 0])
        recs = {
            "open": arr[:, 1],
            "high": arr[:, 2],
            "low": arr[:, 3],
            "close": arr[:, 4],
            "volume": arr[:, 5],
        }

    df = pd.DataFrame(recs, index=idx)
    if max_rows and len(df) > max_rows:
        df = df.iloc[-max_rows:]
    return ensure_ohlcv(df, min_rows=min_rows, timeframe=timeframe)


def load_dataframe(settings: Any) -> pd.DataFrame:
    """Dispatch on ``data.source`` and return a validated OHLCV frame.

    ``mt5`` routes through :mod:`selftrade.mt5` so the terminal is only touched
    when it is actually configured.
    """
    d = settings.data
    src = d.source
    if src == "synthetic":
        return make_synthetic(
            d.synthetic_rows,
            seed=settings.evolution.seed,
            timeframe=d.timeframe,
            min_rows=d.min_rows,
        )
    if src == "csv":
        return load_csv(
            d.csv,
            datetime_col=d.datetime_col,
            min_rows=d.min_rows,
            timeframe=d.timeframe,
            max_rows=d.max_rows,
        )
    if src == "freqtrade_json":
        return load_freqtrade_json(
            d.csv, min_rows=d.min_rows, timeframe=d.timeframe, max_rows=d.max_rows
        )
    if src == "mt5":
        from . import mt5

        return mt5.load_bars(settings, raise_on_error=True)
    raise DataError(f"unknown data.source: {src!r}")


# --------------------------------------------------------------------------
# Public klines (real market data, no API key)
# --------------------------------------------------------------------------

# The public market-data mirror serves the same payload as api.binance.com but
# is not geo-restricted.  Both are keyless and read-only.
DEFAULT_FETCH_BASE_URL = "https://data-api.binance.vision"

# Project timeframe -> exchange interval.  Identity here, but the mapping is
# explicit so an unsupported timeframe fails loudly instead of silently.
EXCHANGE_INTERVALS: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "12h": "12h",
    "1d": "1d",
    "1w": "1w",
}

MAX_KLINE_PAGE = 1000


def normalize_symbol(symbol: str) -> str:
    """``BTC/USDT`` -> ``BTCUSDT`` (the exchange wants no separator)."""
    sym = str(symbol).strip().upper().replace("/", "").replace("-", "").replace("_", "")
    if not sym or not sym.isalnum():
        raise DataError(
            f"invalid symbol {symbol!r}; use e.g. BTC/USDT or BTCUSDT (no spaces)."
        )
    return sym


def _exchange_interval(timeframe: str) -> str:
    tf = str(timeframe).strip()
    if tf not in EXCHANGE_INTERVALS:
        raise DataError(
            f"timeframe {timeframe!r} cannot be fetched. Supported: "
            f"{', '.join(EXCHANGE_INTERVALS)}"
        )
    return EXCHANGE_INTERVALS[tf]


def _get_json(get: Any, base_url: str, params: dict[str, Any], timeout: float) -> Any:
    return _request_json(get, base_url, "/api/v3/klines", params, timeout)


def _request_json(
    get: Any, base_url: str, path: str, params: dict[str, Any], timeout: float
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise DataError(
            f"network error talking to {base_url}: {exc}. Check the internet "
            f"connection, or use a local CSV (data.source: csv)."
        ) from exc
    status = getattr(resp, "status_code", 200)
    if status and int(status) != 200:
        raise DataError(
            f"{base_url} returned HTTP {status} for symbol={params.get('symbol')} "
            f"interval={params.get('interval')}. Check the symbol/timeframe."
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise DataError(f"{base_url} returned a non-JSON body: {exc}") from exc
    # An error is a dict carrying both "code" and "msg"; some endpoints (e.g.
    # /api/v3/ticker/24hr) legitimately answer with a dict, so the two must not
    # be confused.
    if isinstance(payload, dict) and "code" in payload and "msg" in payload:
        raise DataError(
            f"{base_url}{path} error {payload.get('code')}: {payload.get('msg')} "
            f"(symbol={params.get('symbol')}). Use a listed symbol, e.g. BTC/USDT."
        )
    return payload


def fetch_klines(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    rows: int = 1000,
    *,
    base_url: str = DEFAULT_FETCH_BASE_URL,
    session: Any = None,
    timeout: float = 20.0,
    end_ms: int | None = None,
) -> pd.DataFrame:
    """Download **real** OHLCV candles from the public klines endpoint.

    Keyless and read-only; pages backwards with ``endTime`` until ``rows`` bars
    (or the start of the instrument's history) are collected, then trims to the
    most recent ``rows``.  The result goes through :func:`ensure_ohlcv`, so it is
    indistinguishable from any other source by the time the pipeline sees it.

    ``session`` accepts anything with a ``.get(url, params=, timeout=)`` method
    returning an object with ``.json()``/``.status_code`` - tests inject a stub,
    so this function is exercised offline.
    """
    if rows <= 0:
        raise DataError(f"rows must be positive, got {rows}")
    sym = normalize_symbol(symbol)
    interval = _exchange_interval(timeframe)
    get = getattr(session, "get", None) or requests.get

    collected: list[list[Any]] = []
    end_time = end_ms
    while len(collected) < rows:
        want = int(min(MAX_KLINE_PAGE, rows - len(collected)))
        params: dict[str, Any] = {"symbol": sym, "interval": interval, "limit": want}
        if end_time is not None:
            params["endTime"] = int(end_time)
        batch = _get_json(get, base_url, params, timeout)
        if not isinstance(batch, list) or not batch:
            break
        batch = [row for row in batch if isinstance(row, (list, tuple)) and len(row) >= 6]
        if not batch:
            break
        collected = list(batch) + collected
        if len(batch) < want:
            break  # history exhausted
        first_open = int(batch[0][0])
        end_time = first_open - 1
        if len(batch) >= rows:
            break

    if not collected:
        raise DataError(
            f"no candles returned for {sym} {interval}. Check the symbol, or use "
            f"a local CSV (data.source: csv)."
        )
    recs = collected[-rows:]
    idx = pd.DatetimeIndex(pd.to_datetime([int(r[0]) for r in recs], unit="ms", utc=True))
    df = pd.DataFrame(
        {
            "open": [float(r[1]) for r in recs],
            "high": [float(r[2]) for r in recs],
            "low": [float(r[3]) for r in recs],
            "close": [float(r[4]) for r in recs],
            "volume": [float(r[5]) for r in recs],
        },
        index=idx,
    )
    return ensure_ohlcv(df, min_rows=0, timeframe=timeframe)


def fetch_ticker(
    symbol: str = "BTC/USDT",
    *,
    base_url: str = DEFAULT_FETCH_BASE_URL,
    session: Any = None,
    timeout: float = 20.0,
) -> dict[str, float]:
    """The 24h rolling stats of one symbol, keyless and read-only.

    ``GET /api/v3/ticker/24hr`` on the public mirror from the same configuration
    as :func:`fetch_klines`.  ``session`` is injectable so this is testable
    offline, like every other loader here.
    """
    sym = normalize_symbol(symbol)
    get = getattr(session, "get", None) or requests.get
    body = _request_json(get, base_url, "/api/v3/ticker/24hr", {"symbol": sym}, timeout)
    if not isinstance(body, dict):
        raise DataError(f"the ticker endpoint returned {type(body).__name__}, not an object")

    def num(key: str) -> float:
        try:
            return float(body.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    return {
        "last": num("lastPrice"),
        "open": num("openPrice"),
        "high": num("highPrice"),
        "low": num("lowPrice"),
        "volume": num("volume"),
        "quote_volume": num("quoteVolume"),
        "change": num("priceChange"),
        "change_pct": num("priceChangePercent") / 100.0,
        "trades": num("count"),
    }


def save_csv(df: pd.DataFrame, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index.name = "timestamp"
    out.to_csv(p)
    return p


def split_holdout(
    df: pd.DataFrame, holdout_frac: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split into (development, holdout). Holdout is the tail of the series."""
    if holdout_frac <= 0:
        return df, df.iloc[0:0]
    n = len(df)
    cut = int(round(n * (1.0 - holdout_frac)))
    cut = max(1, min(cut, n))
    return df.iloc[:cut], df.iloc[cut:]
