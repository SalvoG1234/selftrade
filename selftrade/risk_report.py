"""One measurement of the account against the sealed limits, for every reader.

Three things ask the same question - *how far is the account from the caps the model
cannot move?* - and they must answer it identically, or the one that shouts would be
describing a limit the others do not enforce:

* the dashboard's *Risk limits* panel (``GET /api/risk``);
* ``python -m selftrade risk limits`` from a terminal;
* the alert channel in :mod:`selftrade.notify`, which has to be able to say where the
  account stands at the moment it stops trading.

So this module is the single answer.  It reconstructs the account from the state
database, hands it to the gate's own limit functions (:func:`~selftrade.execution.account_limits`
and :func:`~selftrade.execution.exposure_limits`) and reports the **room left** under
each cap.  What it measures, and what it cannot:

* **drawdown** - the active paper session's equity curve against its own running peak,
  the measure :class:`~selftrade.execution.PaperBroker` takes;
* **today's P&L** - that curve's last point against the first point of the same UTC
  date, which is the broker's own day boundary;
* **exposure** - ``equity - cash`` on the session, where the rail marks its open
  position (the session stores no positions table, and ``tests/test_paper.py`` pins the
  identity against the fill ledger);
* with **no session** there is nothing to measure, and the report says so instead of
  four zeros that would read as a clean bill of health;
* the **Binance rail is not measured here**: reading a real account means signed
  requests against the exchange's rate budget, and that is the key panel's job.

Two of the four limits can *halt* trading (drawdown and daily loss raise the alert the
broker arms the kill switch on); the other two only refuse the order that breaks them.
Every entry says which it is, because "at the cap" means something different in each
case.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .execution import AccountSnapshot, RiskGate, account_limits, exposure_limits
from .memory import Memory
from .risk_rules import LIMITS, SEALED_DIGEST, digest_of, is_sealed

#: How much of a cap may be left before the report calls it *tight*.  A quarter is a
#: judgement, not a limit: the threshold picks the colour and the warning, nothing else.
TIGHT_FRAC = 0.25

#: Bumped whenever the payload gains or changes a field, so a client can tell.
REPORT_VERSION = 1


def utc_day(ts: Any) -> Any:
    """The UTC date of a stored timestamp, or ``None`` when it cannot be read."""
    try:
        return datetime.fromisoformat(str(ts).replace(" ", "T", 1)).date()
    except ValueError:
        return None


def _session_account(session: dict[str, Any], points: list[dict[str, Any]]) -> dict[str, Any]:
    """The account the session's own rows describe, in the gate's shape."""
    equities = [float(p["equity"]) for p in points]
    cash = float(session["cash"])
    equity = equities[-1] if equities else float(session["initial_cash"])
    peak = max(equities) if equities else equity
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0
    day_pnl_pct = 0.0
    if points:
        last_day = utc_day(points[-1]["ts"])
        same_day = [float(p["equity"]) for p in points if utc_day(p["ts"]) == last_day]
        start = same_day[0] if same_day else equity
        day_pnl_pct = equity / start - 1.0 if start else 0.0
    positions_value = equity - cash
    snapshot = AccountSnapshot(
        ts=str(points[-1]["ts"]) if points else "",
        cash=cash,
        positions_value=positions_value,
        equity=equity,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        day_pnl_pct=day_pnl_pct,
        drawdown=drawdown,
        open_positions=1 if abs(positions_value) > 1e-9 else 0,
    )
    return {
        "session": {
            "session_id": session["id"],
            "name": session["name"],
            "status": session["status"],
            "cursor": session["cursor"],
            "total_bars": session["total_bars"],
            "points": len(points),
            "as_of": str(points[-1]["ts"]) if points else None,
        },
        "account": {
            "equity": equity,
            "cash": cash,
            "positions_value": positions_value,
            "peak_equity": peak,
            "drawdown_pct": drawdown * 100.0,
            "day_pnl_pct": day_pnl_pct * 100.0,
            "open_positions": int(snapshot.open_positions),
        },
        "snapshot": snapshot,
    }


def measure_account(settings: Any, memory: Any = None) -> dict[str, Any]:
    """The active paper session's account, or the honest reason there is none.

    ``memory`` is injectable so a caller that already holds one (and the tests) do not
    open the database twice.
    """
    store = memory if memory is not None else Memory(settings.memory.db)
    try:
        session = store.active_paper_session()
        points = store.paper_equity(session["id"]) if session else []
    except Exception as exc:  # noqa: BLE001 - a reader must report, never raise
        return {"ok": False, "session": None, "account": None, "snapshot": None,
                "error": f"the account could not be read: {exc}"}
    if session is None:
        return {
            "ok": False,
            "session": None,
            "account": None,
            "snapshot": None,
            "error": "no paper session to measure",
            "hint": "create one with `python -m selftrade paper --create`, then this is "
            "measured on every read",
        }
    out = _session_account(session, points)
    out["ok"] = True
    out["symbol"] = str(getattr(settings.data, "symbol", "BTC/USDT") or "BTC/USDT")
    out["session"]["symbol"] = out["symbol"]
    return out


def limit_checks(
    settings: Any, account: dict[str, Any], snapshot: AccountSnapshot, *, symbol: str = ""
) -> list[dict[str, Any]]:
    """The four limits for a book as it stands, tightest first, halting ones ahead.

    The functions are the gate's own; only the ordering and the ``as it stands`` wording
    are this module's.
    """
    gate = RiskGate.from_config(settings.risk_gate)
    checks = account_limits(gate.limits, snapshot) + exposure_limits(
        gate.limits,
        snapshot.equity,
        symbol=symbol or str(getattr(settings.data, "symbol", "BTC/USDT") or "BTC/USDT"),
        symbol_notional=snapshot.positions_value,
        max_leverage=gate.max_leverage,
    )
    checks.sort(key=lambda c: (not c["arms"], c["headroom_frac"]))
    return checks


def limits_report(settings: Any, memory: Any = None, *, alerts: Any = None) -> dict[str, Any]:
    """The whole answer: the sealed caps, the account, the room left under each.

    ``alerts`` overrides the "who would be told" block (the notifier passes its own
    readiness so the report and the channel never disagree); by default it is read from
    :mod:`selftrade.notify`, which reports the *names* of unset environment variables
    and never their values.
    """
    from . import notify as notify_mod

    limits = LIMITS
    digest = digest_of(limits)
    gate = RiskGate.from_config(settings.risk_gate)
    out: dict[str, Any] = {
        "version": REPORT_VERSION,
        "limits": limits.as_dict(),
        "digest": digest,
        "pinned": digest == SEALED_DIGEST,
        "sealed": is_sealed(limits),
        "leverage_cap": float(gate.max_leverage),
        "tight_frac": TIGHT_FRAC,
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": False,
        "session": None,
        "account": None,
        "checks": [],
        "breached": [],
        "would_arm": False,
        "nearest": None,
        "alerts": alerts if alerts is not None else notify_mod.readiness(settings),
    }
    account = measure_account(settings, memory)
    out["ok"] = bool(account["ok"])
    out["session"] = account["session"]
    out["account"] = account["account"]
    if not account["ok"]:
        out["error"] = account.get("error")
        if account.get("hint"):
            out["hint"] = account["hint"]
        return out

    checks = limit_checks(
        settings, account["account"], account["snapshot"], symbol=account["symbol"]
    )
    arming = [c for c in checks if c["arms"]]
    nearest = arming[0] if arming else None
    out.update(
        {
            "checks": checks,
            "breached": [c["name"] for c in checks if not c["ok"]],
            "would_arm": any(c["arms"] and not c["ok"] for c in checks),
            "nearest": (
                {
                    "name": nearest["name"],
                    "headroom": nearest["headroom"],
                    "headroom_frac": nearest["headroom_frac"],
                    "unit": nearest["unit"],
                }
                if nearest
                else None
            ),
        }
    )
    return out


#: A halting limit is in one of these bands, or in none of them.
CLOSE = "close"
"""Inside ``TIGHT_FRAC`` (or ``notify.warn_below_frac``) of its cap, still allowed."""

PAST = "past"
"""Breached, and nothing has stopped yet: the next order through the gate arms the stop.

The loudest state this program has, and the easiest to miss: the account is past a cap,
but the kill switch is armed by the *gate* the next time it checks an order - which may
be minutes away, or never, if nothing tries to trade.  A historical session (a finished
paper window, a weekend, a stopped loop) sits here indefinitely."""


def halting_bands(
    report: dict[str, Any], below: float = TIGHT_FRAC
) -> dict[str, str]:
    """Which band each limit that can halt trading is in: ``{name: close|past}``.

    Only the limits that stop the system are bands at all - an exposure cap can be
    breached for a while without anything halting, and shouting about that would train
    the reader to ignore the channel.  ``below`` is the share of a cap that may be left
    before *close* counts; 0 turns *close* off, while *past* is always reported: it is
    not a threshold, it is a breach.
    """
    bands: dict[str, str] = {}
    for check in report.get("checks", []):
        if not check.get("arms"):
            continue
        if not check["ok"]:
            bands[check["name"]] = PAST
        elif below > 0 and check["headroom_frac"] <= float(below):
            bands[check["name"]] = CLOSE
    return bands


def _line(check: dict[str, Any]) -> str:
    """One limit, in the unit it is measured in: ``max_drawdown 12.20% of 15.0% (2.80pp)``."""
    unit = "x" if check["unit"] == "x" else "%"
    room_unit = "x" if check["unit"] == "x" else "pp"
    return (
        f"{check['name']} {check['value']:.2f}{unit} of the {check['limit']:.2f}{unit} cap "
        f"(room {check['headroom']:+.2f}{room_unit})"
    )


def describe(report: dict[str, Any], *, limit: int = 4) -> list[str]:
    """Short human lines: the headline, then the limits.  Used by the CLI and the alerts."""
    if not report.get("ok"):
        return [f"not measurable: {report.get('error')}"]
    lines: list[str] = []
    if report["would_arm"]:
        lines.append(
            "BREACHED: " + ", ".join(report["breached"]) + " - the next order is refused "
            "and arms the kill switch"
        )
    elif report["nearest"]:
        n = report["nearest"]
        unit = "x" if n["unit"] == "x" else "pp"
        lines.append(f"within the limits; nearest is {n['name']} with {n['headroom']:+.2f}{unit} left")
    else:
        lines.append("within the limits")
    for check in report["checks"][:limit]:
        mark = "!" if not check["ok"] else ("~" if check["headroom_frac"] <= report["tight_frac"] else " ")
        lines.append(f"  {mark} {_line(check)} - {check['effect']} [{check['source']}]")
    session = report.get("session") or {}
    if session:
        lines.append(
            f"  measured on paper session #{session['session_id']} "
            f"({session['points']} point(s), as of {session['as_of']})"
        )
    return lines
