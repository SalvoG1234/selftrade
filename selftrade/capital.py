"""The capital account: crediting simulated money the agents can actually work.

The brief's next step after a paper broker is the account the broker trades: *where
does the money come from, and who is allowed to put it there?*  This module is that
account, and it is deliberately built as a **ledger** rather than an editable number:

* every movement is one append-only row in ``cash_ledger`` - kind, amount, currency,
  reason, source, the session it belongs to and the balance it left behind.  Nothing is
  ever updated or deleted, so the balance is always the sum of what was recorded and a
  mistake is corrected by a new entry with a reason, never by rewriting history;
* the sign of ``amount`` is always the effect on the account's **free** balance: a
  ``deposit`` and a ``release`` add to it, a ``withdrawal`` and an ``allocation`` take
  from it.  One number, one meaning;
* **nothing here is real money and nothing here touches a network.**  There is no
  exchange call, no key, no order: a credit is a locally recorded, simulated allocation
  of trading capital, and the module imports no HTTP client for a test to catch.  The
  real rails (:mod:`selftrade.binance`, :mod:`selftrade.mt5`) are armed by their own
  two-key gates and do not read this ledger;
* **no agent can credit itself.**  ``deposit``/``withdraw``/``allocate`` are reached
  from three places only - the ``capital`` CLI command, ``paper --fund``/``--release``,
  and the dashboard's confirm-gated POST - and the research side (the Evolver, the
  proposer, the evaluators, the critic) has no code path that imports this module.
  ``tests/test_capital.py`` pins that by reading the sources.

What still binds on funded capital: the sealed caps.  Credited money is traded by a
paper session through the pre-trade gate, so 5% per position and the 15% drawdown
ceiling apply to it exactly as they apply to a session created with the config's
``initial_cash``.  Funding changes how much capital there is, not how much may be
risked on a single position - and ``docs/CAPITAL.md`` records that boundary.

The policy in ``settings.funding`` (per-credit cap, total-capital ceiling, whether
withdrawals are allowed) is *operator* policy, not the sealed layer: it is there to
stop a typo - an extra zero - from funding a fantasy account, and it is reachable by
whoever edits ``config.yaml``, exactly as ``backtest.initial_cash`` always was.
"""

from __future__ import annotations

import sys
from typing import Any

from .memory import Memory

#: Where a movement came from.  An unknown source is refused: the audit line has to say
#: who credited the money, and "the model" is not one of the answers.
SOURCES = ("cli", "dashboard", "test")

#: Rejecting a non-finite or absurd number early keeps the ledger free of rows that
#: would make every later balance meaningless.
MAX_ABS_AMOUNT = 1e12


class CapitalError(Exception):
    """Raised when a movement is refused.  The message says why, never just 'no'."""


def _currency(settings: Any, override: str | None = None) -> str:
    cfg = getattr(settings, "funding", None)
    code = (override or getattr(cfg, "currency", "") or "USDT").strip()
    return code.upper() or "USDT"


def _policy(settings: Any) -> dict[str, Any]:
    cfg = getattr(settings, "funding", None)
    return {
        "enabled": bool(getattr(cfg, "enabled", True)),
        "currency": _currency(settings),
        "max_deposit": float(getattr(cfg, "max_deposit", 100_000.0) or 0.0),
        "max_total_capital": float(getattr(cfg, "max_total_capital", 1_000_000.0) or 0.0),
        "allow_withdrawal": bool(getattr(cfg, "allow_withdrawal", True)),
    }


def policy(settings: Any) -> dict[str, Any]:
    """The funding policy in force, with the note that it is not the sealed layer."""
    out = _policy(settings)
    out["note"] = (
        "operator policy for the simulated account; the sealed caps "
        "(5% per position, 15% drawdown) still apply to whatever capital a session holds"
    )
    return out


def _check_amount(amount: Any, *, what: str = "amount") -> float:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise CapitalError(f"{what} must be a number, got {amount!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise CapitalError(f"{what} must be a finite number, got {amount!r}")
    if value <= 0:
        raise CapitalError(f"{what} must be positive, got {value}")
    if value > MAX_ABS_AMOUNT:
        raise CapitalError(f"{what} is beyond what this ledger will record: {value}")
    return value


def _check_reason(reason: Any) -> str:
    text = str(reason or "").strip()
    if not text:
        raise CapitalError(
            "a capital movement must carry a reason: it is the only record of why the "
            "money exists (a credit with no reason is an unauditable credit)"
        )
    return text


def _check_source(source: Any) -> str:
    text = str(source or "cli").strip().lower()
    if text not in SOURCES:
        raise CapitalError(f"unknown source {source!r}; use one of {', '.join(SOURCES)}")
    return text


def _account(settings: Any, memory: Memory, *, limit: int = 50) -> dict[str, Any]:
    """The account as the ledger describes it, plus the active session's funding."""
    totals = memory.ledger_totals()
    pol = _policy(settings)
    session = memory.active_paper_session()
    allocations = memory.session_allocations()
    active = None
    if session is not None:
        active = {
            "session_id": session["id"],
            "name": session["name"],
            "status": session["status"],
            "cursor": session["cursor"],
            "total_bars": session["total_bars"],
            "capital": float(session["initial_cash"]),
            "funded": float(allocations.get(session["id"], 0.0)),
            "started": memory.paper_session_started(session["id"]),
        }
    return {
        "ok": True,
        "simulated": True,
        "currency": pol["currency"],
        "enabled": pol["enabled"],
        "balance": totals["balance"],
        "credited": totals["credited"],
        "withdrawn": totals["withdrawn"],
        "allocated": totals["allocated"],
        "net_credited": totals["credited"] - totals["withdrawn"],
        "entries": totals["entries"],
        "counts": totals["counts"],
        "session": active,
        "policy": {
            "max_deposit": pol["max_deposit"],
            "max_total_capital": pol["max_total_capital"],
            "allow_withdrawal": pol["allow_withdrawal"],
        },
        "ledger": memory.ledger_entries(limit),
        "note": (
            "a simulated account: credits are recorded locally and traded on paper. "
            "No exchange, no key and no order is involved anywhere in this ledger."
        ),
    }


def summary(settings: Any, memory: Memory | None = None, *, limit: int = 50) -> dict[str, Any]:
    """The whole account: free balance, what was credited, what is allocated, the rows."""
    return _account(settings, memory if memory is not None else Memory(settings.memory.db),
                    limit=limit)


def deposit(
    settings: Any,
    amount: Any,
    *,
    reason: Any,
    memory: Memory | None = None,
    currency: str | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Credit simulated capital into the account's free balance.

    Refused - with nothing written - when funding is disabled, when the amount is not a
    positive finite number, when it exceeds ``funding.max_deposit``, when it would take
    the total credited past ``funding.max_total_capital``, or when no reason is given.
    """
    value = _check_amount(amount, what="the deposit")
    note = _check_reason(reason)
    src = _check_source(source)
    store = memory if memory is not None else Memory(settings.memory.db)
    pol = _policy(settings)
    if not pol["enabled"]:
        raise CapitalError(
            "funding is disabled: set `funding.enabled: true` to credit capital"
        )
    if pol["max_deposit"] <= 0 or value > pol["max_deposit"]:
        raise CapitalError(
            f"the deposit {value:g} exceeds `funding.max_deposit` ({pol['max_deposit']:g}); "
            "raise the cap on purpose or credit less"
        )
    totals = store.ledger_totals()
    projected = (totals["credited"] - totals["withdrawn"]) + value
    if pol["max_total_capital"] > 0 and projected > pol["max_total_capital"]:
        raise CapitalError(
            f"this credit would take the total capital to {projected:g}, past "
            f"`funding.max_total_capital` ({pol['max_total_capital']:g})"
        )
    entry = store.add_ledger_entry(
        kind="deposit",
        amount=value,
        currency=_currency(settings, currency),
        reason=note,
        source=src,
    )
    return {"ok": True, "action": "deposit", "entry": entry,
            "account": _account(settings, store)}


def withdraw(
    settings: Any,
    amount: Any,
    *,
    reason: Any,
    memory: Memory | None = None,
    currency: str | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Take simulated capital back out of the free balance.

    Refused when withdrawals are off, when the amount is not positive, when it is larger
    than the free balance, or when no reason is given.  It is a ledger row too: taking
    money out is as auditable as putting it in.
    """
    value = _check_amount(amount, what="the withdrawal")
    note = _check_reason(reason)
    src = _check_source(source)
    store = memory if memory is not None else Memory(settings.memory.db)
    pol = _policy(settings)
    if not pol["allow_withdrawal"]:
        raise CapitalError("withdrawals are disabled (`funding.allow_withdrawal: false`)")
    totals = store.ledger_totals()
    if value > totals["balance"]:
        raise CapitalError(
            f"the withdrawal {value:g} is more than the free balance "
            f"({totals['balance']:g}); allocated capital is not yours to withdraw until "
            "the session holding it is released"
        )
    entry = store.add_ledger_entry(
        kind="withdrawal",
        amount=-value,
        currency=_currency(settings, currency),
        reason=note,
        source=src,
    )
    return {"ok": True, "action": "withdraw", "entry": entry,
            "account": _account(settings, store)}


def _resolve_session(memory: Memory, session_id: int | None) -> dict[str, Any]:
    if session_id is None:
        session = memory.active_paper_session()
        if session is None:
            raise CapitalError(
                "no paper session to fund: create one first "
                "(`python -m selftrade paper --create`)"
            )
        return session
    session = memory.get_paper_session(int(session_id))
    if session is None:
        raise CapitalError(f"no paper session with id {session_id}")
    return session


def preflight_allocate(settings: Any, memory: Memory, amount: Any, session_id: int | None) -> float:
    """Everything ``allocate`` refuses on, checked **before** a session is created.

    ``paper --create --fund X`` uses this so a refusal costs nothing: no empty session is
    left behind by a credit that could never have been made.
    """
    value = _check_amount(amount, what="the allocation")
    pol = _policy(settings)
    if not pol["enabled"]:
        raise CapitalError("funding is disabled: set `funding.enabled: true` to allocate")
    totals = memory.ledger_totals()
    if value > totals["balance"]:
        raise CapitalError(
            f"the allocation {value:g} is more than the free balance "
            f"({totals['balance']:g}); credit capital first "
            "(`python -m selftrade capital deposit ...`)"
        )
    if session_id is not None:
        _resolve_session(memory, session_id)
    return value


def allocate(
    settings: Any,
    amount: Any,
    *,
    reason: Any,
    memory: Memory | None = None,
    session_id: int | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Move capital from the free balance into a paper session the agents can trade.

    The session's trading capital becomes its total allocated amount, so a top-up while
    it is still unfunded is allowed and simply raises the capital.  It is refused once
    the session has **traded** - a larger capital re-sizes every position, so adding it
    late would rewrite a history that was already recorded.  The way to work more money
    is a new session, or an explicit ``paper --reset`` first.

    The guard, the ledger row and the capital update happen in one database transaction
    (:meth:`selftrade.memory.Memory.allocate_capital`), so a session cannot start trading
    between the check and the write.
    """
    value = _check_amount(amount, what="the allocation")
    note = _check_reason(reason)
    src = _check_source(source)
    store = memory if memory is not None else Memory(settings.memory.db)
    session = _resolve_session(store, session_id)
    s_id = int(session["id"])
    pol = _policy(settings)
    if not pol["enabled"]:
        raise CapitalError("funding is disabled: set `funding.enabled: true` to allocate")
    totals = store.ledger_totals()
    if value > totals["balance"]:
        raise CapitalError(
            f"the allocation {value:g} is more than the free balance "
            f"({totals['balance']:g}); credit capital first "
            "(`python -m selftrade capital deposit ...`)"
        )
    if store.paper_session_started(s_id):
        raise CapitalError(
            f"paper session {s_id} has already traded: a bigger capital would re-size "
            "every position it recorded. Fund a new session, or "
            f"`paper --reset --session {s_id}` first if you mean to start it over"
        )
    new_capital = float(store.session_allocations().get(s_id, 0.0)) + value
    entry = store.allocate_capital(
        session_id=s_id,
        amount=-value,
        new_capital=new_capital,
        currency=_currency(settings),
        reason=note,
        source=src,
    )
    if entry is None:  # pragma: no cover - the guard is inside the transaction
        raise CapitalError(
            f"paper session {s_id} started trading while the allocation was being written; "
            "nothing was credited"
        )
    return {
        "ok": True,
        "action": "allocate",
        "entry": entry,
        "session_id": s_id,
        "capital": new_capital,
        "account": _account(settings, store),
    }


def release(
    settings: Any,
    *,
    reason: Any,
    memory: Memory | None = None,
    session_id: int | None = None,
    source: str = "cli",
    amount: Any = None,
) -> dict[str, Any]:
    """Return a session's allocated capital to the free balance.

    Refused while the session has traded, for the same reason an allocation is: the
    capital and the recorded results have to move together, and only an explicit
    ``paper --reset`` may take a session back to its start.  ``amount`` defaults to the
    whole allocation.
    """
    note = _check_reason(reason)
    src = _check_source(source)
    store = memory if memory is not None else Memory(settings.memory.db)
    session = _resolve_session(store, session_id)
    s_id = int(session["id"])
    allocated = float(store.session_allocations().get(s_id, 0.0))
    if allocated <= 0:
        raise CapitalError(
            f"paper session {s_id} holds no allocated capital: there is nothing to release"
        )
    value = allocated if amount is None else _check_amount(amount, what="the release")
    if value > allocated:
        raise CapitalError(
            f"the release {value:g} is more than the {allocated:g} allocated to session {s_id}"
        )
    if store.paper_session_started(s_id):
        raise CapitalError(
            f"paper session {s_id} has already traded: release would return its capital "
            "while its recorded results stayed behind. Reset it explicitly first "
            f"(`paper --reset --session {s_id}`)"
        )
    entry = store.allocate_capital(
        session_id=s_id,
        amount=value,
        new_capital=allocated - value,
        currency=_currency(settings),
        reason=note,
        source=src,
        kind="release",
    )
    if entry is None:  # pragma: no cover - the guard is inside the transaction
        raise CapitalError(
            f"paper session {s_id} started trading while the release was being written; "
            "nothing was moved"
        )
    return {
        "ok": True,
        "action": "release",
        "entry": entry,
        "session_id": s_id,
        "capital": allocated - value,
        "account": _account(settings, store),
    }


def fund_session(
    settings: Any,
    memory: Memory,
    session_id: int,
    amount: Any,
    *,
    reason: str,
    source: str = "cli",
) -> dict[str, Any]:
    """Allocate ``amount`` to an existing session (the ``paper --fund`` path)."""
    return allocate(
        settings, amount, reason=reason, memory=memory, session_id=session_id, source=source
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cli(settings: Any, args: Any) -> int:
    """``python -m selftrade capital …``: status, credit, allocate, release, ledger."""
    import json

    action = (getattr(args, "action", None) or "status").lower()
    store = Memory(settings.memory.db)
    try:
        if action in ("status", "show"):
            account = summary(settings, store, limit=getattr(args, "limit", 50) or 50)
            if getattr(args, "json", False):
                print(json.dumps(account, indent=2, default=str))
            else:
                _print_account(account)
            return 0
        if action == "ledger":
            rows = store.ledger_entries(getattr(args, "limit", 50) or 50)
            if getattr(args, "json", False):
                print(json.dumps(rows, indent=2, default=str))
            else:
                _print_ledger(rows, _currency(settings))
            return 0
        if action == "policy":
            pol = policy(settings)
            if getattr(args, "json", False):
                print(json.dumps(pol, indent=2, default=str))
            else:
                for key, value in pol.items():
                    print(f"{key}: {value}")
            return 0
        if action == "deposit":
            out = deposit(
                settings, getattr(args, "amount", None), reason=getattr(args, "reason", ""),
                memory=store, currency=getattr(args, "currency", None),
            )
        elif action == "withdraw":
            out = withdraw(
                settings, getattr(args, "amount", None), reason=getattr(args, "reason", ""),
                memory=store, currency=getattr(args, "currency", None),
            )
        elif action == "allocate":
            out = allocate(
                settings, getattr(args, "amount", None), reason=getattr(args, "reason", ""),
                memory=store, session_id=getattr(args, "session_id", None),
            )
        elif action == "release":
            out = release(
                settings, reason=getattr(args, "reason", ""), memory=store,
                session_id=getattr(args, "session_id", None),
                amount=getattr(args, "amount", None),
            )
        else:
            print(f"unknown capital action: {action}", file=sys.stderr)
            return 1
    except CapitalError as exc:
        print(f"capital: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # the ledger's own kind/amount validation
        print(f"capital: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2, default=str))
    else:
        entry = out["entry"]
        verb = {"deposit": "credited", "withdrawal": "withdrawn",
                "allocation": "allocated", "release": "released"}.get(entry["kind"], entry["kind"])
        print(
            f"{verb} {abs(entry['amount']):.2f} {entry['currency'] or ''} - "
            f"balance {entry['balance_after']:.2f}"
        )
        if out.get("session_id"):
            print(f"  paper session {out['session_id']} capital: {out['capital']:.2f}")
        print(f"  reason: {entry['reason']} ({entry['source']})")
    return 0


def _print_account(account: dict[str, Any]) -> None:
    cur = account["currency"]
    print(f"capital account ({'simulated' if account['simulated'] else 'real'} {cur})")
    print(
        f"  free balance {account['balance']:.2f} {cur}   "
        f"credited {account['credited']:.2f}   withdrawn {account['withdrawn']:.2f}   "
        f"allocated {account['allocated']:.2f}"
    )
    session = account.get("session")
    if session is None:
        print("  no paper session: nothing is holding capital right now")
    else:
        print(
            f"  paper session {session['session_id']} '{session['name']}' "
            f"capital={session['capital']:.2f} (funded {session['funded']:.2f}) "
            f"status={session['status']} cursor={session['cursor']}/{session['total_bars']}"
        )
    print(
        "  note: this is a simulated account - no exchange, no key, no order. "
        "The sealed 5%/15% caps still bind."
    )
    print(f"  funding policy: {account['policy']}")


def _print_ledger(rows: list[dict[str, Any]], currency: str) -> None:
    if not rows:
        print("the ledger is empty: no capital has ever been credited")
        return
    for row in rows:
        # ASCII on purpose: this prints on a Windows console whose code page may not
        # carry the typographic characters the rest of the project uses.
        print(
            f"#{row['id']:<4} {row['kind']:<11} {row['amount']:>12.2f} "
            f"{row['currency'] or currency:<5} balance {row['balance_after']:>12.2f} "
            f"session={row['session_id'] or '-'} | {row['reason'] or '-'} "
            f"({row['source'] or '?'})"
        )
