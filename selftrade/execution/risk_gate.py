"""The pre-trade gate: every order passes through here before it exists.

The brief requires the risk manager to be an independent, deterministic module
with priority over the AI agent, and to check *before* every order: risk per
trade, daily loss, portfolio drawdown, exposure per instrument and overall,
leverage, number of positions and order frequency.  When something breaks it must
block the order, record why and raise an alert - which is exactly what
:class:`Decision` and the caller (``PaperBroker``/live adapters) do.

The portfolio-wide limits come from :mod:`selftrade.risk_rules`, the sealed layer:
they are not configurable here, and there is no flag to bypass them.

One distinction runs through the checks: an order that **opens or increases**
exposure is new risk, while an order that closes or trims a position is not.  The
exposure cap, the leverage check and the stop requirement therefore look at the
exposure the order *adds* and at the exposure the book is left with - otherwise the
same per-position cap that lets a 5% entry through would block the very exit that
flattens it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..risk_rules import LIMITS, RiskLimits
from .base import AccountSnapshot, OrderRequest, Position, Side

ALERT_KIND = "order_blocked"

#: What a failed check does.  The account limits are the ones that *stop* the system:
#: the broker arms the kill switch when a :class:`Decision` carries an alert.
ARMS_SWITCH = "arms the kill switch"
REFUSES_ORDER = "refuses the order"

#: Where a limit comes from.  The exposure and stop caps are frozen in
#: :mod:`selftrade.risk_rules`; the leverage cap is this layer's own setting.
SEALED = "sealed"
CONFIGURED = "config"


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


def _limit(
    name: str,
    value: float,
    limit: float,
    ok: bool,
    detail: str,
    *,
    source: str,
    effect: str,
    reason: str,
    alert: str | None = None,
    headroom: float | None = None,
    unit: str = "pp",
) -> dict[str, Any]:
    """One limit check, carrying what the *caller* needs as well as the numbers.

    ``reason``/``alert`` are the exact strings :meth:`RiskGate.check` appends to a
    refused :class:`Decision`; ``headroom``/``source``/``effect`` are what the
    dashboard's risk panel renders.  They travel in the same dict on purpose: a panel
    that re-derived the numbers could disagree with the gate, and an operator reading
    a limit that the gate does not enforce is being told fiction.

    ``headroom`` is the room left before the check fails, in the check's own unit
    (``unit``: percentage points of equity for the percentage limits, x-equity for
    leverage).  It is derived from the cap unless a check measures its own room
    (today's P&L can only *add* room, so it cannot be read as ``limit - |p&l|``).
    """
    entry = _check(name, value, limit, ok, detail)
    room = float(limit) - float(value) if headroom is None else float(headroom)
    entry.update(
        {
            "headroom": room,
            # Comparable across limits with different units and different sizes: the
            # share of the cap that is still unused.  Above 1.0 is possible (a
            # profitable day pushes the daily room past its own cap) and means the
            # same thing: nothing close to the limit is in the way.
            "headroom_frac": (room / float(limit)) if limit else 0.0,
            "unit": unit,
            "source": source,
            "effect": effect,
            # An alert is precisely what arms the stop, so the panel can say which
            # limits can halt trading without re-deciding it.
            "arms": alert is not None,
            "reason": reason,
            "alert": alert,
        }
    )
    return entry


def account_limits(limits: RiskLimits, account: AccountSnapshot) -> list[dict[str, Any]]:
    """The two account-level limits, measured: drawdown and today's P&L.

    These are the checks that can *halt* trading rather than refuse one order: a
    breach is what puts an alert on the decision, and the broker arms the kill switch
    when it sees one.  Both come from the sealed layer, so neither the config nor the
    model can move them.

    The equities and the peak are the caller's business: the account snapshot carries
    them already measured, and the gate and the dashboard panel hand it in the same
    shape, so the two cannot drift apart.
    """
    dd_pct = float(account.drawdown) * 100.0
    dd_ok = dd_pct <= limits.MAX_DRAWDOWN_PCT + 1e-9
    day_pnl_pct = float(account.day_pnl_pct)
    day_pct = abs(day_pnl_pct) * 100.0
    day_ok = day_pnl_pct >= -limits.MAX_DAILY_LOSS_PCT / 100.0 - 1e-12
    return [
        _limit(
            "max_drawdown",
            dd_pct,
            limits.MAX_DRAWDOWN_PCT,
            dd_ok,
            f"account drawdown {dd_pct:.2f}% against the sealed "
            f"{limits.MAX_DRAWDOWN_PCT:.1f}%",
            source=SEALED,
            effect=ARMS_SWITCH,
            reason=(
                f"account drawdown {dd_pct:.2f}% breaks the sealed "
                f"{limits.MAX_DRAWDOWN_PCT:.1f}% limit: trading halts"
            ),
            alert="drawdown limit breached: new orders halted",
        ),
        _limit(
            "max_daily_loss",
            day_pct,
            limits.MAX_DAILY_LOSS_PCT,
            day_ok,
            f"today's P&L is {day_pnl_pct * 100:+.2f}% against the sealed "
            f"-{limits.MAX_DAILY_LOSS_PCT:.1f}% daily halt",
            source=SEALED,
            effect=ARMS_SWITCH,
            reason=(
                f"today's loss {day_pnl_pct * 100:.2f}% is past the sealed "
                f"-{limits.MAX_DAILY_LOSS_PCT:.1f}% daily halt"
            ),
            alert="daily loss limit breached: new orders halted",
            # Room measured from the signed P&L: a profitable day adds room instead
            # of eating the cap from the wrong side.
            headroom=limits.MAX_DAILY_LOSS_PCT + day_pnl_pct * 100.0,
        ),
    ]


def exposure_limits(
    limits: RiskLimits,
    equity: float,
    *,
    symbol: str,
    symbol_notional: float,
    other_notional: float = 0.0,
    max_leverage: float = 1.0,
    hypothetical: bool = False,
) -> list[dict[str, Any]]:
    """The per-position and gross-exposure caps for a book.

    ``hypothetical=True`` describes the book **after** the order the gate is looking
    at ("would be"), ``False`` the book as it stands ("is"), which is what the panel
    asks for.  Neither cap halts trading: they refuse the order that would break them.
    """
    equity = max(1e-9, float(equity))
    verb = "would be" if hypothetical else "is"
    symbol_pct = abs(float(symbol_notional)) / equity * 100.0
    position_ok = symbol_pct <= limits.MAX_POSITION_PCT + 1e-9
    leverage = (abs(float(other_notional)) + abs(float(symbol_notional))) / equity
    leverage_ok = leverage <= float(max_leverage) + 1e-9
    return [
        _limit(
            "position_pct",
            symbol_pct,
            limits.MAX_POSITION_PCT,
            position_ok,
            f"{symbol} exposure {verb} {symbol_pct:.2f}% of equity against the sealed "
            f"{limits.MAX_POSITION_PCT:.1f}%",
            source=SEALED,
            effect=REFUSES_ORDER,
            reason=(
                f"{symbol_pct:.2f}% of equity on {symbol} breaks the sealed "
                f"{limits.MAX_POSITION_PCT:.1f}% per-position cap"
            ),
        ),
        _limit(
            "leverage",
            leverage,
            float(max_leverage),
            leverage_ok,
            f"gross exposure {verb} {leverage:.3f}x equity, limit {max_leverage:.2f}x",
            source=CONFIGURED,
            effect=REFUSES_ORDER,
            reason=f"gross exposure {leverage:.3f}x exceeds the {max_leverage:.2f}x limit",
            unit="x",
        ),
    ]


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

        # 3. the order has to be meaningful - sized, and sized one way only
        qty_ok = float(request.qty) > 0 or request.sized_at_fill
        checks.append(
            _check(
                "qty",
                float(request.quote_qty) if request.sized_at_fill else float(request.qty),
                0.0,
                qty_ok,
                f"quote_qty={request.quote_qty}"
                if request.sized_at_fill
                else f"qty={request.qty}",
            )
        )
        if not qty_ok:
            reasons.append(f"qty must be positive, got {request.qty}")
        if float(request.qty) > 0 and request.sized_at_fill:
            reasons.append("give either qty or quote_qty, not both")

        reference = float(price) if price is not None else float(request.limit_price or 0.0)
        if reference <= 0:
            reasons.append("no reference price available for the risk checks")
            checks.append(_check("reference_price", reference, 0.0, False, "price unknown"))
        notional = request.notional(reference) if reference > 0 else 0.0

        equity = max(1e-9, float(account.equity))

        # What the order does to the book.  An order that closes or trims a position
        # *reduces* exposure: counting it as new exposure would make the cap and the
        # stop requirement block every exit, which is the opposite of risk control.
        current_signed = sum(float(p.qty) for p in positions if p.symbol == request.symbol)
        current_notional = abs(current_signed) * reference if reference > 0 else 0.0
        other_notional = (
            sum(p.notional(reference) for p in positions if p.symbol != request.symbol)
            if reference > 0
            else 0.0
        )
        signed_notional = request.side.sign * notional
        same_way = current_signed == 0 or (current_signed > 0) == (request.side is Side.BUY)
        opening_notional = notional if same_way else max(0.0, notional - current_notional)
        resulting_notional = (
            abs(current_signed * reference + signed_notional) if reference > 0 else 0.0
        )

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

        # 5. exposure on the instrument (sealed) and overall leverage, after the fill.
        #    The limits themselves live in `exposure_limits`, which the dashboard panel
        #    calls with the book as it stands: one implementation, two readers.
        if reference > 0:
            for entry in exposure_limits(
                self.limits,
                equity,
                symbol=request.symbol,
                symbol_notional=resulting_notional,
                other_notional=other_notional,
                max_leverage=self.max_leverage,
                hypothetical=True,
            ):
                checks.append(entry)
                if not entry["ok"]:
                    reasons.append(entry["reason"])

        # 6. risk per trade, which needs a stop.  A close adds no risk and needs none:
        #    the position it closes was already gated, stop included.
        stop_pct = request.stop_loss_pct
        if self.require_stop and stop_pct is None and opening_notional > 0:
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
            risk_pct = opening_notional * abs(float(stop_pct)) / equity * 100.0
            risk_ok = risk_pct <= self.risk_pct * 100.0 + 1e-9
            checks.append(
                _check(
                    "trade_risk",
                    risk_pct,
                    self.risk_pct * 100.0,
                    risk_ok,
                    f"order adds {opening_notional:,.2f} of exposure risking {risk_pct:.3f}% "
                    f"of equity if the stop is hit, budget {self.risk_pct * 100.0:.3f}%",
                )
            )
            if not risk_ok:
                reasons.append(
                    f"the order risks {risk_pct:.3f}% of equity, above the "
                    f"{self.risk_pct * 100.0:.3f}% per-trade budget"
                )

        # 7. account-level sealed limits - the ones that can halt trading
        for entry in account_limits(self.limits, account):
            checks.append(entry)
            if not entry["ok"]:
                reasons.append(entry["reason"])
                if entry["alert"]:
                    alerts.append(entry["alert"])

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
