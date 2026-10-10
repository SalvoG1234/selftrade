"""The pre-trade gate: every order passes through here before it exists.

The brief requires the risk manager to be an independent, deterministic module
with priority over the AI agent, and to check *before* every order: risk per
trade, daily loss, portfolio drawdown, exposure per instrument and overall,
leverage, number of positions and order frequency.  When something breaks it must
block the order, record why and raise an alert - which is exactly what
:class:`Decision` and the caller (``PaperBroker``/live adapters) do.

The portfolio-wide limits come from :mod:`selftrade.risk_rules`, the sealed layer:
they are not configurable here, and there is no flag to bypass them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..risk_rules import LIMITS, RiskLimits
from .base import AccountSnapshot, OrderRequest, Position

ALERT_KIND = "order_blocked"


@dataclass
class Decision:
    """The gate's verdict, with the numbers that produced it."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "alerts": list(self.alerts),
            "checks": [dict(c) for c in self.checks],
        }


def _check(
    name: str, value: float, limit: float, ok: bool, detail: str, *, blocking: bool = True
) -> dict[str, Any]:
    return {
        "name": name,
        "value": float(value),
        "limit": float(limit),
        "ok": bool(ok),
        "detail": detail,
        "blocking": bool(blocking),
    }


@dataclass
class RiskGate:
    """Configurable pre-trade checks; the hard portfolio limits are sealed."""

    limits: RiskLimits = LIMITS
    risk_pct: float = 0.005
    """Share of equity the stop distance may risk on one trade."""

    max_positions: int = 1
    allowed_symbols: Sequence[str] | None = None
    max_leverage: float = 1.0
    min_seconds_between_orders: float = 0.0
    min_notional: float = 1.0
    require_stop: bool = True

    # ------------------------------------------------------------------ wiring
    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "RiskGate":
        """Build a gate from a settings section (``settings.risk_gate``).

        The section carries only this layer's own thresholds; the portfolio-wide
        limits are always taken from the sealed :mod:`selftrade.risk_rules` layer,
        whatever the config says.  ``overrides`` wins over the file.
        """
        kwargs: dict[str, Any] = {
            "risk_pct": float(getattr(config, "risk_pct", cls.risk_pct)),
            "max_positions": int(getattr(config, "max_positions", cls.max_positions)),
            "max_leverage": float(getattr(config, "max_leverage", cls.max_leverage)),
            "min_seconds_between_orders": float(
                getattr(config, "min_seconds_between_orders", cls.min_seconds_between_orders)
            ),
            "min_notional": float(getattr(config, "min_notional", cls.min_notional)),
            "require_stop": bool(getattr(config, "require_stop", cls.require_stop)),
        }
        allowed = list(getattr(config, "allowed_symbols", None) or [])
        if allowed:
            kwargs["allowed_symbols"] = allowed
        kwargs.update(overrides)
        return cls(**kwargs)

    # ------------------------------------------------------------------ check
    def check(
        self,
        request: OrderRequest,
        account: AccountSnapshot,
        positions: Iterable[Position],
        *,
        price: float | None = None,
        last_order_at: float | None = None,
        now: float | None = None,
        kill_switch: Any = None,
    ) -> Decision:
        positions = list(positions)
        checks: list[dict[str, Any]] = []
        reasons: list[str] = []
        alerts: list[str] = []

        # 1. the kill switch wins over everything
        if kill_switch is not None:
            state = kill_switch.describe() if hasattr(kill_switch, "describe") else dict(kill_switch)
            blocked = bool(state.get("blocked"))
            checks.append(
                _check(
                    "kill_switch",
                    1.0 if blocked else 0.0,
                    0.0,
                    not blocked,
                    f"kill switch blocked: {state.get('reason') or 'n/a'}"
                    if blocked
                    else "kill switch clear",
                )
            )
            if blocked:
                reasons.append(f"kill switch is engaged ({state.get('reason') or 'no reason'})")
                alerts.append("kill switch engaged: no new orders")

        # 2. the instrument must be allowed
        allowed = list(self.allowed_symbols) if self.allowed_symbols else []
        if allowed:
            ok = request.symbol in allowed
            checks.append(
                _check(
                    "allowed_symbol",
                    1.0 if ok else 0.0,
                    1.0,
                    ok,
                    f"{request.symbol} {'is' if ok else 'is not'} in allowed_symbols {allowed}",
                )
            )
            if not ok:
                reasons.append(f"{request.symbol} is not in the allowed_symbols list")

        # 3. the order has to be meaningful
        qty_ok = float(request.qty) > 0
        checks.append(
            _check("qty", float(request.qty), 0.0, qty_ok, f"qty={request.qty}")
        )
        if not qty_ok:
            reasons.append(f"qty must be positive, got {request.qty}")

        reference = float(price) if price is not None else float(request.limit_price or 0.0)
        if reference <= 0:
            reasons.append("no reference price available for the risk checks")
            checks.append(_check("reference_price", reference, 0.0, False, "price unknown"))
        notional = request.notional(reference) if reference > 0 else 0.0

        equity = max(1e-9, float(account.equity))
        if reference > 0:
            min_ok = notional >= self.min_notional
            checks.append(
                _check(
                    "min_notional",
                    notional,
                    self.min_notional,
                    min_ok,
                    f"order notional {notional:,.2f} vs minimum {self.min_notional:,.2f}",
                )
            )
            if not min_ok:
                reasons.append(
                    f"order notional {notional:,.2f} is below the minimum {self.min_notional:,.2f}"
                )

        # 4. position count
        held = {p.symbol for p in positions if abs(p.qty) > 0}
        is_new_symbol = request.symbol not in held
        if is_new_symbol:
            ok = len(held) < int(self.max_positions)
            checks.append(
                _check(
                    "max_positions",
                    float(len(held) + 1),
                    float(self.max_positions),
                    ok,
                    f"opening {request.symbol} would make {len(held) + 1} position(s), "
                    f"limit {self.max_positions}",
                )
            )
            if not ok:
                reasons.append(
                    f"already holding {len(held)} position(s), the limit is {self.max_positions}"
                )

        # 5. exposure on the instrument (sealed) and overall leverage
        if reference > 0:
            current = sum(p.notional(reference) for p in positions if p.symbol == request.symbol)
            symbol_exposure_pct = (current + notional) / equity * 100.0
            ok = symbol_exposure_pct <= self.limits.MAX_POSITION_PCT + 1e-9
            checks.append(
                _check(
                    "position_pct",
                    symbol_exposure_pct,
                    self.limits.MAX_POSITION_PCT,
                    ok,
                    f"{request.symbol} exposure would be {symbol_exposure_pct:.2f}% of equity "
                    f"against the sealed {self.limits.MAX_POSITION_PCT:.1f}%",
                )
            )
            if not ok:
                reasons.append(
                    f"{symbol_exposure_pct:.2f}% of equity on {request.symbol} breaks the sealed "
                    f"{self.limits.MAX_POSITION_PCT:.1f}% per-position cap"
                )

            gross = sum(p.notional(reference) for p in positions if p.symbol != request.symbol)
            leverage = (gross + current + notional) / equity
            lev_ok = leverage <= self.max_leverage + 1e-9
            checks.append(
                _check(
                    "leverage",
                    leverage,
                    self.max_leverage,
                    lev_ok,
                    f"gross exposure would be {leverage:.3f}x equity, limit {self.max_leverage:.2f}x",
                )
            )
            if not lev_ok:
                reasons.append(
                    f"gross exposure {leverage:.3f}x exceeds the {self.max_leverage:.2f}x limit"
                )

        # 6. risk per trade, which needs a stop
        stop_pct = request.stop_loss_pct
        if self.require_stop and stop_pct is None:
            reasons.append("no stop loss on the order; the risk layer requires one")
            checks.append(_check("trade_risk", 0.0, self.risk_pct * 100.0, False, "no stop given"))
        elif stop_pct is not None and reference > 0:
            stop_distance_pct = abs(float(stop_pct)) * 100.0
            hard_ok = 0.0 < stop_distance_pct <= self.limits.HARD_STOP_LOSS_PCT + 1e-9
            checks.append(
                _check(
                    "hard_stop",
                    stop_distance_pct,
                    self.limits.HARD_STOP_LOSS_PCT,
                    hard_ok,
                    f"stop at {stop_distance_pct:.2f}% against the sealed "
                    f"{self.limits.HARD_STOP_LOSS_PCT:.1f}% hard stop",
                )
            )
            if not hard_ok:
                reasons.append(
                    f"a {stop_distance_pct:.2f}% stop is outside the sealed hard stop of "
                    f"{self.limits.HARD_STOP_LOSS_PCT:.1f}%"
                )
            risk_pct = notional * abs(float(stop_pct)) / equity * 100.0
            risk_ok = risk_pct <= self.risk_pct * 100.0 + 1e-9
            checks.append(
                _check(
                    "trade_risk",
                    risk_pct,
                    self.risk_pct * 100.0,
                    risk_ok,
                    f"order risks {risk_pct:.3f}% of equity if the stop is hit, "
                    f"budget {self.risk_pct * 100.0:.3f}%",
                )
            )
            if not risk_ok:
                reasons.append(
                    f"the order risks {risk_pct:.3f}% of equity, above the "
                    f"{self.risk_pct * 100.0:.3f}% per-trade budget"
                )

        # 7. account-level sealed limits
        dd_pct = float(account.drawdown) * 100.0
        dd_ok = dd_pct <= self.limits.MAX_DRAWDOWN_PCT + 1e-9
        checks.append(
            _check(
                "max_drawdown",
                dd_pct,
                self.limits.MAX_DRAWDOWN_PCT,
                dd_ok,
                f"account drawdown {dd_pct:.2f}% against the sealed "
                f"{self.limits.MAX_DRAWDOWN_PCT:.1f}%",
            )
        )
        if not dd_ok:
            reasons.append(
                f"account drawdown {dd_pct:.2f}% breaks the sealed "
                f"{self.limits.MAX_DRAWDOWN_PCT:.1f}% limit: trading halts"
            )
            alerts.append("drawdown limit breached: new orders halted")

        day_pct = abs(float(account.day_pnl_pct)) * 100.0
        day_ok = float(account.day_pnl_pct) >= -self.limits.MAX_DAILY_LOSS_PCT / 100.0 - 1e-12
        checks.append(
            _check(
                "max_daily_loss",
                day_pct,
                self.limits.MAX_DAILY_LOSS_PCT,
                day_ok,
                f"today's P&L is {account.day_pnl_pct * 100:+.2f}% against the sealed "
                f"-{self.limits.MAX_DAILY_LOSS_PCT:.1f}% daily halt",
            )
        )
        if not day_ok:
            reasons.append(
                f"today's loss {account.day_pnl_pct * 100:.2f}% is past the sealed "
                f"-{self.limits.MAX_DAILY_LOSS_PCT:.1f}% daily halt"
            )
            alerts.append("daily loss limit breached: new orders halted")

        # 8. order frequency
        if self.min_seconds_between_orders > 0 and last_order_at is not None and now is not None:
            gap = float(now) - float(last_order_at)
            freq_ok = gap >= self.min_seconds_between_orders
            checks.append(
                _check(
                    "order_frequency",
                    gap,
                    self.min_seconds_between_orders,
                    freq_ok,
                    f"{gap:.1f}s since the last order, minimum spacing "
                    f"{self.min_seconds_between_orders:.1f}s",
                )
            )
            if not freq_ok:
                reasons.append(
                    f"only {gap:.1f}s since the previous order; the minimum spacing is "
                    f"{self.min_seconds_between_orders:.1f}s"
                )

        return Decision(ok=not reasons, reasons=reasons, alerts=alerts, checks=checks)


def describe_decision(decision: Decision) -> str:
    """One readable line for logs and the CLI."""
    if decision.ok:
        return "allowed by the risk gate"
    return "blocked: " + "; ".join(decision.reasons)
