"""A fake MetaTrader 5 terminal implementing the API surface mt5.py uses."""

from __future__ import annotations

import numpy as np
from types import SimpleNamespace


def make_rates(n=400, start=1609459200, step=3600, base=1.10):
    t = start + np.arange(n, dtype="int64") * step
    wave = base + 0.002 * np.sin(np.arange(n) / 20.0)
    o = wave
    c = wave + 0.0004 * np.cos(np.arange(n) / 10.0)
    h = np.maximum(o, c) + 0.0002
    l = np.minimum(o, c) - 0.0002
    dtype = [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<i8"),
        ("spread", "<i4"),
        ("real_volume", "<i8"),
    ]
    arr = np.zeros(n, dtype=dtype)
    arr["time"] = t
    arr["open"] = o
    arr["high"] = h
    arr["low"] = l
    arr["close"] = c
    arr["tick_volume"] = np.arange(n) + 1
    arr["spread"] = 20
    return arr


class FakeMT5:
    # constants
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    TIMEFRAME_W1 = 32769
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    SYMBOL_TRADE_MODE_DISABLED = 1
    SYMBOL_TRADE_MODE_CLOSEONLY = 2
    SYMBOL_TRADE_MODE_FULL = 4
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(
        self,
        *,
        symbol="EURUSD",
        trade_mode=0,  # DEMO
        point=0.00001,
        digits=5,
        bid=1.10000,
        ask=1.10020,
        bars=400,
        trade_mode_symbol=4,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=100.0,
        tick_value=1.0,
        tick_size=0.00001,
        positions=None,
        init_ok=True,
        password_seen=None,
    ):
        self._symbol = symbol
        self._trade_mode = trade_mode
        self._point = point
        self._digits = digits
        self._bid = bid
        self._ask = ask
        self._bars = bars
        self._symbol_trade_mode = trade_mode_symbol
        self._volume_min = volume_min
        self._volume_step = volume_step
        self._volume_max = volume_max
        self._tick_value = tick_value
        self._tick_size = tick_size
        self._positions = positions or []
        self._init_ok = init_ok
        self.sent = []
        self.init_kwargs = None
        self.initialized = False
        self.shutdown_calls = 0
        self.password_seen = password_seen

    # -- lifecycle
    def initialize(self, **kwargs):
        self.init_kwargs = kwargs
        if getattr(self, "password_seen", None) is not None:
            self.password_seen.append(kwargs.get("password"))
        if not self._init_ok:
            return False
        self.initialized = True
        return True

    def shutdown(self):
        self.shutdown_calls += 1
        self.initialized = False
        return True

    def last_error(self):
        return (1, "fake terminal error")

    # -- account / symbols
    def account_info(self):
        return SimpleNamespace(
            login=1234567,
            server="Fake-Demo",
            name="Fake Trader",
            currency="USD",
            balance=10000.0,
            equity=10000.0,
            leverage=100,
            trade_mode=self._trade_mode,
        )

    def symbol_info(self, name):
        if name != self._symbol:
            return None
        return SimpleNamespace(
            name=self._symbol,
            point=self._point,
            digits=self._digits,
            spread=int((self._ask - self._bid) / self._point),
            trade_mode=self._symbol_trade_mode,
            volume_min=self._volume_min,
            volume_step=self._volume_step,
            volume_max=self._volume_max,
            trade_tick_value=self._tick_value,
            trade_tick_size=self._tick_size,
        )

    def symbol_info_tick(self, name):
        if name != self._symbol:
            return None
        return SimpleNamespace(bid=self._bid, ask=self._ask, time=1)

    def symbols_get(self, group=None):
        if group and not self._symbol.startswith(group.rstrip("*")):
            return ()
        return (SimpleNamespace(name=self._symbol),)

    def symbol_select(self, name, enable):
        return name == self._symbol

    # -- market data
    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if symbol != self._symbol:
            return None
        rates = make_rates(min(count, self._bars), base=1.10)
        return rates

    # -- positions / orders
    def positions_get(self, symbol=None, ticket=None):
        out = list(self._positions)
        if ticket is not None:
            out = [p for p in out if getattr(p, "ticket", None) == ticket]
        if symbol is not None:
            out = [p for p in out if getattr(p, "symbol", None) == symbol]
        return tuple(out)

    def order_send(self, request):
        self.sent.append(dict(request))
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=111,
            deal=222,
            volume=request.get("volume"),
            price=request.get("price"),
            comment="fake fill",
        )
