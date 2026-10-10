"""The broker interface: one contract, several implementations.

The brief asks for an abstract ``BrokerAdapter`` so that the system is not welded
to one provider's API.  Every adapter - the simulated one here, the MetaTrader 5
rail in :mod:`selftrade.mt5` and the Binance Spot Testnet adapter - speaks the same
seven calls:

    get_account_balance, get_positions, get_market_data,
    submit_order, cancel_order, get_order_status, get_trade_history

Two rules are baked into this module, not into the documentation:

* ``mode`` is ``"paper"`` unless an adapter explicitly says otherwise, and
  nothing in :mod:`selftrade.execution` imports ``requests`` or ``socket`` - a
  test asserts both, so the simulated rail cannot quietly acquire a network
  dependency;
* an order that breaks a risk limit is returned **rejected with its reasons** and
  recorded as a risk event, never silently dropped.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pandas as pd


class BrokerError(Exception):
    """Raised for illegal broker operations (cancel a filled order, bad qty...)."""


class RiskBlockedError(BrokerError):
    """Raised when a risk limit blocks an order and the caller needs the reasons."""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    NEW = "new"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    """Built, signed and recorded, but deliberately not sent.  Not a fill, not a
    refusal: the caller can tell the difference without reading a log line."""


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


@dataclass
class OrderRequest:
    """What the caller wants.  ``client_id`` makes retries idempotent."""

    client_id: str
    symbol: str
    side: Side
    qty: float
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_loss_pct: float | None = None
    note: str = ""

    def notional(self, price: float) -> float:
        return abs(float(self.qty)) * abs(float(price))


@dataclass
class Order:
    id: str
    client_id: str
    symbol: str
    side: Side
    qty: float
    type: OrderType
    status: OrderStatus
    created_at: str
    limit_price: float | None = None
    filled_qty: float = 0.0
    avg_price: float | None = None
    updated_at: str | None = None
    reject_reason: str = ""
    note: str = ""

    @property
    def is_open(self) -> bool:
        return self.status is OrderStatus.NEW

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": float(self.qty),
            "type": self.type.value,
            "status": self.status.value,
            "limit_price": self.limit_price,
            "filled_qty": float(self.filled_qty),
            "avg_price": self.avg_price,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reject_reason": self.reject_reason,
            "note": self.note,
        }


@dataclass
class Fill:
    ts: str
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    reason: str = "signal"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": float(self.qty),
            "price": float(self.price),
            "fee": float(self.fee),
            "reason": self.reason,
        }


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    opened_at: str = ""

    @property
    def side(self) -> Side:
        return Side.BUY if self.qty >= 0 else Side.SELL

    def notional(self, price: float) -> float:
        return abs(self.qty) * float(price)

    def unrealized(self, price: float) -> float:
        return self.qty * (float(price) - self.avg_price)

    def as_dict(self, price: float | None = None) -> dict[str, Any]:
        out = {
            "symbol": self.symbol,
            "qty": float(self.qty),
            "avg_price": float(self.avg_price),
            "side": self.side.value,
            "opened_at": self.opened_at,
        }
        if price is not None:
            out["price"] = float(price)
            out["notional"] = self.notional(price)
            out["unrealized_pnl"] = self.unrealized(price)
        return out


@dataclass
class AccountSnapshot:
    ts: str
    cash: float
    positions_value: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    day_pnl_pct: float
    drawdown: float
    open_positions: int
    fees_paid: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "cash": round(self.cash, 6),
            "positions_value": round(self.positions_value, 6),
            "equity": round(self.equity, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "day_pnl_pct": round(self.day_pnl_pct, 8),
            "drawdown": round(self.drawdown, 8),
            "open_positions": int(self.open_positions),
            "fees_paid": round(self.fees_paid, 6),
        }


@dataclass
class Mismatch:
    what: str
    internal: Any
    ledger: Any
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "what": self.what,
            "internal": self.internal,
            "ledger": self.ledger,
            "detail": self.detail,
        }


@dataclass
class BrokerAdapter(ABC):
    """The contract every execution rail implements."""

    name: str = "adapter"
    mode: str = "paper"  # paper | live - only a live adapter may say "live"
    orders_submitted: int = field(default=0, init=False)

    @abstractmethod
    def get_account_balance(self) -> AccountSnapshot:
        """Cash, equity, drawdown and open-position count."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Currently open positions."""

    @abstractmethod
    def get_market_data(self, symbol: str | None = None, limit: int = 100) -> pd.DataFrame:
        """The most recent OHLCV bars this adapter can see."""

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> Order:
        """Send an order.  Rejected orders come back with ``reject_reason``."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order:
        """Cancel an open order.  Raises :class:`BrokerError` if it cannot be done."""

    @abstractmethod
    def get_order_status(self, order_id: str) -> Order | None:
        """Look up one order."""

    @abstractmethod
    def get_trade_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fills, oldest first, as plain dicts."""

    # ---------------------------------------------------------------- extras
    def open_orders(self) -> list[Order]:
        """Orders that are still working.  Default: none."""
        return []

    def reconcile(self) -> list[Mismatch]:
        """Compare the adapter's own view with its ledger.  Default: nothing to check."""
        return []

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "orders_submitted": int(self.orders_submitted),
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
