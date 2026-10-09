"""MetaTrader 5 / Trading.com bridge.

Trading.com exposes WebTrader + MetaTrader 5 and **no public REST/WebSocket
API**: the only automatable path is MT5, i.e. the ``MetaTrader5`` package (Windows
only) talking to a running, logged-in terminal.

    selftrade -> mt5.py -> MT5 terminal -> broker

Design notes:

* The real package is imported lazily and can be replaced via ``module=`` so
  **all** logic (data, symbols, gates, sending) is testable offline with a fake
  terminal.  ``MetaTrader5`` is never a test requirement.
* ``data.source: mt5`` yields the same OHLCV frame as every other source.
* Sending an order requires **two keys**: ``mt5.live: true`` in config *and*
  ``--live`` on the command line.  ``require_demo`` refuses real accounts even
  when armed.  The dry run is the default and never calls ``order_send``.
* The password is read only from the environment variable named by
  ``mt5.password_env``; it is never printed, logged or stored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .data import ensure_ohlcv

TF_ATTR = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1",
    "4h": "TIMEFRAME_H4",
    "1d": "TIMEFRAME_D1",
    "1w": "TIMEFRAME_W1",
}


class MT5Error(Exception):
    """Base error for the bridge."""


class OrderRejected(MT5Error):
    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("order rejected: " + "; ".join(self.reasons))


# Test seam: lets the whole bridge (including data.source=mt5) run offline
# against a fake terminal without the Windows-only package installed.
_MODULE_OVERRIDE: Any = None


def set_module_override(module: Any) -> None:
    global _MODULE_OVERRIDE
    _MODULE_OVERRIDE = module


def is_available(module: Any = None) -> tuple[bool, str]:
    """Return (available, message) without requiring the package on non-Windows."""
    if module is None:
        module = _MODULE_OVERRIDE
    if module is not None:
        return True, f"injected module ({type(module).__name__})"
    try:
        import MetaTrader5  # noqa: F401
    except Exception as exc:  # pragma: no cover - platform dependent
        return False, (
            "MetaTrader5 package not installed (Windows-only). "
            "Install with: pip install -r requirements-mt5.txt"
        )
    return True, "MetaTrader5 package present"


@dataclass
class Terminal:
    """Wraps the injected module and the connection lifecycle."""

    module: Any
    settings: Any
    connected: bool = False
    notes: list[str] = field(default_factory=list)

    # -- lifecycle ----------------------------------------------------
    def connect(self) -> "Terminal":
        cfg = self.settings.mt5
        m = self.module
        if not cfg.enabled:
            raise MT5Error("mt5.enabled is false in config; enable it to talk to the terminal")
        password = os.environ.get(cfg.password_env) if cfg.password_env else None
        kwargs: dict[str, Any] = {}
        if cfg.terminal_path:
            kwargs["path"] = cfg.terminal_path
        if cfg.login:
            kwargs["login"] = int(cfg.login)
        if cfg.server:
            kwargs["server"] = cfg.server
        if password:
            kwargs["password"] = password

        ok = bool(m.initialize(**kwargs)) if kwargs else bool(m.initialize())
        if not ok:
            err = m.last_error() if hasattr(m, "last_error") else None
            raise MT5Error(
                f"MT5 initialize() failed: {err}. Is the terminal installed, running and logged in?"
            )
        self.connected = True
        return self

    def close(self) -> None:
        if self.connected:
            try:
                self.module.shutdown()
            except Exception:  # pragma: no cover
                pass
            self.connected = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # -- helpers ------------------------------------------------------
    def timeframe(self) -> Any:
        attr = TF_ATTR.get(self.settings.mt5.timeframe)
        if attr is None or not hasattr(self.module, attr):
            raise MT5Error(
                f"unsupported mt5.timeframe {self.settings.mt5.timeframe!r}; "
                f"choose one of {', '.join(TF_ATTR)}"
            )
        return getattr(self.module, attr)

    def resolve_symbol(self, symbol: str | None = None) -> str:
        cfg = self.settings.mt5
        base = symbol or cfg.symbol
        candidates = []
        if cfg.suffix and not base.endswith(cfg.suffix):
            candidates.append(base + cfg.suffix)
        candidates.append(base)

        for cand in candidates:
            info = self.module.symbol_info(cand)
            if info is not None:
                try:
                    self.module.symbol_select(cand, True)
                except Exception:  # pragma: no cover
                    pass
                return cand

        suggestions: list[str] = []
        try:
            found = self.module.symbols_get(group=f"{base}*") or []
            suggestions = [getattr(s, "name", str(s)) for s in found][:20]
        except Exception:  # pragma: no cover
            suggestions = []
        hint = f" Candidates from the terminal: {', '.join(suggestions)}" if suggestions else ""
        raise MT5Error(
            f"symbol {base!r} not found (tried {', '.join(candidates)}). "
            f"Check mt5.symbol / mt5.suffix.{hint}"
        )


def open_terminal(settings: Any, module: Any = None, connect: bool = True) -> Terminal:
    if module is None:
        module = _MODULE_OVERRIDE
    if module is None:
        try:  # pragma: no cover - platform dependent
            import MetaTrader5 as module  # type: ignore
        except Exception as exc:
            raise MT5Error(
                "MetaTrader5 package not installed (Windows-only). "
                "Install with: pip install -r requirements-mt5.txt"
            ) from exc
    term = Terminal(module=module, settings=settings)
    if connect:
        term.connect()
    return term


# --------------------------------------------------------------------------
# Data in
# --------------------------------------------------------------------------


def bars_to_frame(rates: Any) -> pd.DataFrame:
    if rates is None or len(rates) == 0:
        raise MT5Error("no bars returned (market closed? wrong symbol/timeframe?)")
    names = rates.dtype.names if hasattr(rates, "dtype") and rates.dtype.names else None
    if names is None:
        raise MT5Error("unexpected rates structure from the terminal")

    def col(name: str) -> Any:
        return rates[name]

    idx = pd.to_datetime(np.asarray(col("time"), dtype="int64"), unit="s", utc=True)
    df = pd.DataFrame(
        {
            "open": np.asarray(col("open"), dtype="float64"),
            "high": np.asarray(col("high"), dtype="float64"),
            "low": np.asarray(col("low"), dtype="float64"),
            "close": np.asarray(col("close"), dtype="float64"),
            "volume": np.asarray(col("tick_volume"), dtype="float64")
            if "tick_volume" in names
            else np.zeros(len(rates)),
        },
        index=idx,
    )
    return df


def load_bars(
    settings: Any, *, module: Any = None, terminal: Terminal | None = None, raise_on_error: bool = False
) -> pd.DataFrame:
    """Fetch bars from the terminal and return a validated OHLCV frame."""
    cfg = settings.mt5
    owned = terminal is None
    term = terminal or open_terminal(settings, module=module, connect=True)
    try:
        sym = term.resolve_symbol()
        tf = term.timeframe()
        rates = term.module.copy_rates_from_pos(sym, tf, 0, int(cfg.bars))
        if rates is None or len(rates) == 0:
            raise MT5Error(
                f"terminal returned no bars for {sym} {cfg.timeframe}; the market may be closed "
                "(FX/CFD do not tick on weekends)."
            )
        df = bars_to_frame(rates)
        return ensure_ohlcv(
            df,
            min_rows=0,  # the terminal decides how much history it has
            timeframe=cfg.timeframe,
            warn_spacing=True,
        )
    finally:
        if owned:
            term.close()


def fetch_snapshot(settings: Any, module: Any = None) -> pd.DataFrame:
    """``mt5 bars --out``: save a reproducible offline snapshot."""
    return load_bars(settings, module=module)


def listed_symbols(settings: Any, module: Any = None, pattern: str | None = None) -> list[str]:
    term = open_terminal(settings, module=module, connect=True)
    try:
        group = pattern or (settings.mt5.symbol + "*")
        found = term.module.symbols_get(group=group) or []
        return sorted(getattr(s, "name", str(s)) for s in found)
    finally:
        term.close()


def account_info(settings: Any, module: Any = None) -> dict[str, Any]:
    term = open_terminal(settings, module=module, connect=True)
    try:
        acc = term.module.account_info()
        if acc is None:
            raise MT5Error("account_info() returned None; is the terminal logged in?")
        return {
            "login": getattr(acc, "login", None),
            "server": getattr(acc, "server", None),
            "name": getattr(acc, "name", None),
            "currency": getattr(acc, "currency", None),
            "balance": getattr(acc, "balance", None),
            "equity": getattr(acc, "equity", None),
            "leverage": getattr(acc, "leverage", None),
            "trade_mode": getattr(acc, "trade_mode", None),
            "is_demo": is_demo_account(term.module, acc),
        }
    finally:
        term.close()


def open_positions(settings: Any, module: Any = None) -> list[dict[str, Any]]:
    term = open_terminal(settings, module=module, connect=True)
    try:
        positions = term.module.positions_get() or []
        out = []
        for p in positions:
            out.append(
                {
                    "ticket": getattr(p, "ticket", None),
                    "symbol": getattr(p, "symbol", None),
                    "type": getattr(p, "type", None),
                    "volume": getattr(p, "volume", None),
                    "price_open": getattr(p, "price_open", None),
                    "sl": getattr(p, "sl", None),
                    "tp": getattr(p, "tp", None),
                    "profit": getattr(p, "profit", None),
                }
            )
        return out
    finally:
        term.close()


def is_demo_account(module: Any, acc: Any) -> bool:
    demo = getattr(module, "ACCOUNT_TRADE_MODE_DEMO", 0)
    return getattr(acc, "trade_mode", None) == demo


# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------


def spread_costs(settings: Any, symbol: str | None = None, module: Any = None) -> dict[str, Any]:
    """Read the real spread from the broker and convert it to bps.

    ``spread_points * point / mid * 10000`` = spread in bps.
    """
    term = open_terminal(settings, module=module, connect=True)
    try:
        sym = term.resolve_symbol(symbol)
        info = term.module.symbol_info(sym)
        tick = term.module.symbol_info_tick(sym)
        if info is None or tick is None:
            raise MT5Error(
                f"no market data for {sym}; the market may be closed and the terminal idle."
            )
        point = float(getattr(info, "point", 0.0))
        digits = int(getattr(info, "digits", 5))
        ask = float(getattr(tick, "ask", 0.0))
        bid = float(getattr(tick, "bid", 0.0))
        if ask <= 0 or bid <= 0 or point <= 0:
            raise MT5Error(f"invalid quote for {sym}: ask={ask} bid={bid} point={point}")
        mid = (ask + bid) / 2.0
        spread_price = ask - bid
        spread_points = spread_price / point
        reported_points = float(getattr(info, "spread", spread_points))
        spread_bps = spread_price / mid * 10000.0
        half = spread_bps / 2.0
        return {
            "symbol": sym,
            "digits": digits,
            "point": point,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_points": spread_points,
            "spread_points_reported": reported_points,
            "spread_price": spread_price,
            "spread_bps": spread_bps,
            "suggested_fee_bps": half,
            "suggested_slippage_bps": half,
            "round_trip_bps": spread_bps,
            "note": (
                "Set backtest.fee_bps and backtest.slippage_bps each to half the spread so the "
                "round trip costs exactly one spread. The backtester does NOT model overnight "
                "swap/financing."
            ),
        }
    finally:
        term.close()


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@dataclass
class OrderPlan:
    request: dict[str, Any]
    dry_run: bool
    notes: list[str]
    reasons: list[str]


def _round_volume(volume: float, info: Any) -> float:
    vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
    vmax = float(getattr(info, "volume_max", 100.0) or 100.0)
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    if step <= 0:
        step = 0.01
    steps = round(volume / step)
    v = steps * step
    v = max(vmin, min(vmax, v))
    return round(v, 8)


def compute_lots(
    settings: Any, term: Terminal, symbol: str, sl_points: int, balance: float | None = None
) -> tuple[float, list[str]]:
    """Risk-based sizing from stop distance; falls back to fixed_lots."""
    cfg = settings.mt5
    notes: list[str] = []
    info = term.module.symbol_info(symbol)
    if info is None:
        notes.append("symbol_info unavailable: using fixed lots")
        return float(cfg.fixed_lots), notes

    tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    point = float(getattr(info, "point", 0.0) or 0.0)
    if balance is None:
        acc = term.module.account_info()
        balance = float(getattr(acc, "balance", 0.0) or 0.0)

    if tick_value <= 0 or tick_size <= 0 or point <= 0 or balance <= 0 or sl_points <= 0:
        notes.append(
            "broker did not publish trade_tick_value/trade_tick_size (or balance/sl missing): "
            "using fixed_lots (declared, not invented)"
        )
        return _round_volume(float(cfg.fixed_lots), info), notes

    value_per_point_per_lot = tick_value * (point / tick_size)
    risk_amount = float(cfg.risk_pct) * balance
    loss_per_lot = value_per_point_per_lot * float(sl_points)
    if loss_per_lot <= 0:
        notes.append("degenerate stop distance: using fixed lots")
        return _round_volume(float(cfg.fixed_lots), info), notes
    raw = risk_amount / loss_per_lot
    lots = _round_volume(raw, info)
    notes.append(f"sized by risk_pct={cfg.risk_pct} over {sl_points} points -> {lots} lots")
    return lots, notes


def build_order(
    settings: Any,
    term: Terminal,
    *,
    symbol: str | None = None,
    side: str = "buy",
    volume: float | None = None,
    sl_points: int | None = None,
    tp_points: int | None = None,
    live_flag: bool = False,
    dry_run: bool | None = None,
) -> OrderPlan:
    """Assemble the full order request and evaluate every gate."""
    cfg = settings.mt5
    if not cfg.enabled:
        raise MT5Error("mt5.enabled is false in config; enable it to place orders")

    reasons: list[str] = []
    notes: list[str] = []

    armed = bool(cfg.live and live_flag)
    if dry_run is None:
        dry_run = not armed
    if not cfg.live:
        reasons.append("gate: mt5.live is false in config - the bridge is not armed")
    if not live_flag:
        reasons.append("gate: --live not passed on the command line - trigger not pressed")

    sym = term.resolve_symbol(symbol)
    info = term.module.symbol_info(sym)
    tick = term.module.symbol_info_tick(sym)
    if info is None or tick is None:
        raise MT5Error(f"no market data for {sym}; market may be closed")

    acct = term.module.account_info()
    if cfg.require_demo and not is_demo_account(term.module, acct):
        reasons.append(
            f"policy: account trade_mode={getattr(acct, 'trade_mode', None)} is not DEMO "
            f"(require_demo=true)"
        )

    if cfg.allowed_symbols and sym not in cfg.allowed_symbols:
        reasons.append(f"policy: {sym} is not in mt5.allowed_symbols {cfg.allowed_symbols}")

    # market state
    disabled = getattr(term.module, "SYMBOL_TRADE_MODE_DISABLED", 1)
    closeonly = getattr(term.module, "SYMBOL_TRADE_MODE_CLOSEONLY", 2)
    trade_mode = getattr(info, "trade_mode", None)
    if trade_mode in (disabled, closeonly):
        reasons.append(f"market: symbol trade_mode={trade_mode} (disabled or close-only)")

    ask = float(getattr(tick, "ask", 0.0))
    bid = float(getattr(tick, "bid", 0.0))
    mid = (ask + bid) / 2.0 if (ask > 0 and bid > 0) else 0.0
    point = float(getattr(info, "point", 0.0) or 0.0)
    spread_points = ((ask - bid) / point) if (point > 0 and mid > 0) else 0.0
    if mid <= 0:
        reasons.append("market: no valid quote (market closed?)")
    elif cfg.max_spread_points and spread_points > float(cfg.max_spread_points):
        reasons.append(
            f"market: spread {spread_points:.1f} points exceeds max_spread_points="
            f"{cfg.max_spread_points} (news/rollover)"
        )

    positions = term.module.positions_get(symbol=sym) or []
    if cfg.max_positions and len(positions) >= int(cfg.max_positions):
        reasons.append(
            f"policy: {len(positions)} open position(s) on {sym} >= max_positions={cfg.max_positions}"
        )

    if cfg.require_stop and (sl_points is None or int(sl_points) <= 0):
        reasons.append("policy: require_stop=true but no stop distance was given")

    eff_sl_points = int(sl_points) if sl_points else int(cfg.sl_fallback_points)

    if volume is None or volume <= 0:
        volume, sizing_notes = compute_lots(settings, term, sym, eff_sl_points)
        notes.extend(sizing_notes)
    else:
        volume = _round_volume(float(volume), info)
        notes.append(f"volume given explicitly: {volume}")

    vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
    vstep = float(getattr(info, "volume_step", 0.01) or 0.01)
    if volume < vmin:
        reasons.append(f"volume {volume} below broker volume_min={vmin}")
    if cfg.max_lots and volume > float(cfg.max_lots) + 1e-12:
        reasons.append(f"policy: volume {volume} exceeds max_lots={cfg.max_lots}")
    if vstep > 0:
        steps = round(volume / vstep)
        if abs(steps * vstep - volume) > 1e-9:
            volume = _round_volume(volume, info)
            notes.append(f"volume snapped to the broker volume_step ({vstep}) -> {volume}")

    buy = side == "buy"
    price = ask if buy else bid
    sl_price = 0.0
    tp_price = 0.0
    if point > 0 and price > 0:
        if eff_sl_points:
            sl_price = price - eff_sl_points * point if buy else price + eff_sl_points * point
        if tp_points:
            tp_price = price + int(tp_points) * point if buy else price - int(tp_points) * point

    order_type = (
        getattr(term.module, "ORDER_TYPE_BUY", 0)
        if buy
        else getattr(term.module, "ORDER_TYPE_SELL", 1)
    )
    filling = getattr(term.module, "ORDER_FILLING_IOC", getattr(term.module, "ORDER_FILLING_FOK", 0))

    request = {
        "action": getattr(term.module, "TRADE_ACTION_DEAL", 1),
        "symbol": sym,
        "volume": float(volume),
        "type": order_type,
        "price": float(price),
        "sl": float(round(sl_price, int(getattr(info, "digits", 5)))),
        "tp": float(round(tp_price, int(getattr(info, "digits", 5)))) if tp_price else 0.0,
        "deviation": int(cfg.deviation),
        "magic": int(cfg.magic),
        "comment": "selftrade",
        "type_time": getattr(term.module, "ORDER_TIME_GTC", 0),
        "type_filling": filling,
    }
    return OrderPlan(request=request, dry_run=bool(dry_run), notes=notes, reasons=reasons)


def place_order(
    settings: Any,
    *,
    symbol: str | None = None,
    side: str = "buy",
    volume: float | None = None,
    sl_points: int | None = None,
    tp_points: int | None = None,
    live_flag: bool = False,
    module: Any = None,
    terminal: Terminal | None = None,
) -> dict[str, Any]:
    """Dry run by default; sends **only** when both keys are present and gates pass."""
    owned = terminal is None
    term = terminal or open_terminal(settings, module=module, connect=True)
    try:
        plan = build_order(
            settings,
            term,
            symbol=symbol,
            side=side,
            volume=volume,
            sl_points=sl_points,
            tp_points=tp_points,
            live_flag=live_flag,
        )
        if plan.dry_run:
            return {
                "sent": False,
                "dry_run": True,
                "request": plan.request,
                "notes": plan.notes,
                "reasons": plan.reasons,
                "armed": bool(settings.mt5.live),
                "live_flag": live_flag,
            }
        # Armed AND triggered: a policy violation is a hard rejection.
        if plan.reasons:
            raise OrderRejected(plan.reasons)
        result = term.module.order_send(plan.request)
        retcode = getattr(result, "retcode", None)
        done = getattr(term.module, "TRADE_RETCODE_DONE", 10009)
        if retcode != done:
            raise MT5Error(
                f"order_send failed: retcode={retcode} "
                f"comment={getattr(result, 'comment', '')!r}"
            )
        return {
            "sent": True,
            "dry_run": False,
            "request": plan.request,
            "notes": plan.notes,
            "reasons": [],
            "result": {
                "retcode": retcode,
                "order": getattr(result, "order", None),
                "deal": getattr(result, "deal", None),
                "volume": getattr(result, "volume", None),
                "price": getattr(result, "price", None),
                "comment": getattr(result, "comment", ""),
            },
        }
    finally:
        if owned:
            term.close()


def close_position(
    settings: Any,
    *,
    ticket: int,
    live_flag: bool = False,
    module: Any = None,
    terminal: Terminal | None = None,
) -> dict[str, Any]:
    """Close a position by ticket.  Same two-key gate."""
    cfg = settings.mt5
    owned = terminal is None
    term = terminal or open_terminal(settings, module=module, connect=True)
    try:
        positions = term.module.positions_get(ticket=int(ticket)) or []
        if not positions:
            raise MT5Error(f"position ticket {ticket} not found")
        pos = positions[0]
        sym = getattr(pos, "symbol", cfg.symbol)
        volume = float(getattr(pos, "volume", 0.0))
        is_buy = getattr(pos, "type", None) == getattr(term.module, "POSITION_TYPE_BUY", 0)
        tick = term.module.symbol_info_tick(sym)
        price = float(getattr(tick, "bid" if is_buy else "ask", 0.0))
        request = {
            "action": getattr(term.module, "TRADE_ACTION_DEAL", 1),
            "symbol": sym,
            "volume": volume,
            "type": getattr(term.module, "ORDER_TYPE_SELL", 1)
            if is_buy
            else getattr(term.module, "ORDER_TYPE_BUY", 0),
            "position": int(ticket),
            "price": price,
            "deviation": int(cfg.deviation),
            "magic": int(cfg.magic),
            "comment": "selftrade close",
            "type_time": getattr(term.module, "ORDER_TIME_GTC", 0),
            "type_filling": getattr(term.module, "ORDER_FILLING_IOC", 0),
        }
        if not (cfg.live and live_flag):
            return {"sent": False, "dry_run": True, "request": request, "notes": [], "reasons": []}
        result = term.module.order_send(request)
        return {
            "sent": True,
            "dry_run": False,
            "request": request,
            "result": {"retcode": getattr(result, "retcode", None)},
            "notes": [],
            "reasons": [],
        }
    finally:
        if owned:
            term.close()


# --------------------------------------------------------------------------
# doctor + CLI
# --------------------------------------------------------------------------


def doctor_lines(settings: Any, module: Any = None) -> list[str]:
    cfg = settings.mt5
    lines: list[str] = []
    avail, msg = is_available(module)
    if not cfg.enabled:
        lines.append(f"disabled in config (mt5.enabled=false); package: {msg}")
        return lines
    if not avail:
        lines.append(msg)
        return lines
    try:
        term = open_terminal(settings, module=module, connect=True)
    except Exception as exc:
        lines.append(f"cannot connect: {exc}")
        return lines
    try:
        acc = term.module.account_info()
        demo = "DEMO" if is_demo_account(term.module, acc) else "NOT-DEMO"
        lines.append(
            f"connected ({msg}); account login={getattr(acc, 'login', None)} "
            f"server={getattr(acc, 'server', None)} balance={getattr(acc, 'balance', None)} {demo}"
        )
        lines.append(f"password source: env {cfg.password_env} (value never printed)")
        try:
            sym = term.resolve_symbol()
            tick = term.module.symbol_info_tick(sym)
            info = term.module.symbol_info(sym)
            point = float(getattr(info, "point", 0.0) or 0.0)
            spread_pts = ((getattr(tick, "ask", 0) - getattr(tick, "bid", 0)) / point) if point else 0
            lines.append(
                f"symbol {sym}: bid={getattr(tick, 'bid', None)} ask={getattr(tick, 'ask', None)} "
                f"spread={spread_pts:.1f} points"
            )
        except Exception as exc:
            lines.append(f"symbol: {exc}")
        lines.append(
            f"orders: live(armed)={cfg.live} require_demo={cfg.require_demo} "
            f"require_stop={cfg.require_stop} max_lots={cfg.max_lots}"
        )
    finally:
        term.close()
    return lines


def cli(settings: Any, args: Any) -> int:
    import json
    import sys

    from .data import save_csv

    action = args.action
    module = getattr(args, "_module", None)
    try:
        if action == "doctor":
            for line in doctor_lines(settings, module=module):
                print("mt5: " + line)
            return 0
        if action == "symbols":
            syms = listed_symbols(settings, module=module, pattern=(args.symbol or None))
            if args.json:
                print(json.dumps(syms, indent=2))
            else:
                print("\n".join(syms) if syms else "(no symbols matched)")
            return 0
        if action == "bars":
            df = fetch_snapshot(settings, module=module)
            out = args.out or f"data/{settings.mt5.symbol}_{settings.mt5.timeframe}.csv"
            path = save_csv(df, out)
            print(f"wrote {len(df)} bars to {path} ({df.index[0]} .. {df.index[-1]})")
            return 0
        if action == "account":
            info = account_info(settings, module=module)
            print(json.dumps(info, indent=2, default=str))
            return 0
        if action == "positions":
            pos = open_positions(settings, module=module)
            print(json.dumps(pos, indent=2, default=str))
            return 0
        if action == "costs":
            costs = spread_costs(settings, symbol=args.symbol, module=module)
            if args.json:
                print(json.dumps(costs, indent=2, default=str))
            else:
                print(f"{costs['symbol']}: spread {costs['spread_points']:.1f} points "
                      f"= {costs['spread_bps']:.3f} bps")
                print(f"  suggested: --set backtest.fee_bps={costs['suggested_fee_bps']:.4f} "
                      f"--set backtest.slippage_bps={costs['suggested_slippage_bps']:.4f}")
                print(f"  {costs['note']}")
            return 0
        if action == "order":
            try:
                out = place_order(
                    settings,
                    symbol=args.symbol,
                    side=args.side,
                    volume=args.volume,
                    sl_points=args.sl_points,
                    tp_points=args.tp_points,
                    live_flag=bool(args.live),
                    module=module,
                )
            except OrderRejected as exc:
                print("order REJECTED:", file=sys.stderr)
                for r in exc.reasons:
                    print(f"  - {r}", file=sys.stderr)
                return 1
            _print_order(out, args.json)
            return 0
        if action == "close":
            if not args.ticket:
                print("--ticket is required for `mt5 close`", file=sys.stderr)
                return 2
            out = close_position(
                settings, ticket=args.ticket, live_flag=bool(args.live), module=module
            )
            _print_order(out, args.json)
            return 0
    except MT5Error as exc:
        print(f"mt5 error: {exc}", file=sys.stderr)
        return 1
    print(f"unknown mt5 action: {action}", file=sys.stderr)
    return 1


def _print_order(out: dict[str, Any], as_json: bool) -> None:
    import json

    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return
    if out["sent"]:
        print("order SENT (live)")
    elif out.get("dry_run"):
        if out.get("armed"):
            print("DRY RUN (armed but no policy check passed yet): request built, order_send NOT called")
        else:
            print("DRY RUN (bridge not armed): request built, order_send NOT called")
    print(json.dumps(out["request"], indent=2, default=str))
    for n in out.get("notes", []):
        print(f"  note: {n}")
    for r in out.get("reasons", []):
        print(f"  would-block: {r}")
