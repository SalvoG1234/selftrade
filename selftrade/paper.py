"""Forward paper trading - at order level, through the risk gate.

Runs the champion strategy over an **out-of-sample** window, advancing a
**persistent cursor** in blocks of ``paper.step_bars``.  The signals are the
strategy's own, and the mechanics are the backtester's (:func:`selftrade.backtest.simulate`:
signal at the close, fill at the next open, costs on both sides, the exit plan
checked intrabar) - but the session does not book its own fills.  Every entry and
exit is an :class:`~selftrade.execution.base.OrderRequest` that goes through
:func:`selftrade.execution.runner.run_orders`, so the order passes the pre-trade
:class:`~selftrade.execution.risk_gate.RiskGate` and the kill switch first.

Two consequences worth stating plainly:

* a refused order is refused **here too**: it is recorded as a risk event, counted
  in the status and not traded.  ``risk_gate.enabled: false`` is the only way off,
  and it is a config change, not a flag on the command line;
* the session is sized at ``risk_rules.effective_position_size`` - the sealed cap -
  because an order above the sealed per-position cap can only ever be refused.
  The research backtester keeps the configured size on purpose (exploration stays
  free and comparable); ``docs/PAPER-TRADING.md`` records the difference.

Because each step re-runs the window prefix and rewrites the derived rows, finishing
the window in one shot, in many steps, or after a restart all produce identical
trades and equity.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .backtest import compute_metrics
from .data import load_dataframe, split_holdout
from .execution import ALERT_KIND, KillSwitch, RiskGate, kill_switch_path
from .execution.runner import run_orders
from .genome import describe, validate_genome
from .memory import Memory
from .risk_rules import effective_position_size

#: How many recent refusals the status inspects when it attributes them to a session.
RISK_EVENT_SCAN = 500


def session_settings(settings: Any) -> dict[str, Any]:
    """The execution settings a session is pinned to at creation.

    ``position_size`` is the **effective** fraction: the configured size clamped by
    the sealed per-position cap.  ``create_session`` and the tests both use this, so
    the number in the session's manifest is the number the rail will use.
    """
    return {
        "fee_bps": settings.backtest.fee_bps,
        "slippage_bps": settings.backtest.slippage_bps,
        "position_size": effective_position_size(settings),
        "configured_position_size": float(settings.backtest.position_size),
        "allow_short": settings.backtest.allow_short,
        "timeframe": settings.data.timeframe,
    }


def _risk_gate(settings: Any) -> Any:
    """The pre-trade gate, or ``None`` when the config explicitly disables it."""
    if not bool(getattr(settings.risk_gate, "enabled", True)):
        return None
    return RiskGate.from_config(settings.risk_gate)


def blocked_orders(memory: Memory, session_id: int, limit: int = RISK_EVENT_SCAN) -> int:
    """Refusals this session has on record, among the most recent ``limit`` ones.

    The fallback for a status read in a new process: ``run_session`` reports the
    *current* run's count exactly, which is the number that matters, while this scan
    is bounded (``RISK_EVENT_SCAN``) and cannot tell a superseded tick from the last
    one - each tick re-runs the window prefix, so old refusals stay in the audit.
    """
    try:
        events = memory.risk_events(limit=limit, kind=ALERT_KIND)
    except Exception:  # pragma: no cover - the status must never fail on an audit read
        return 0
    return len([e for e in events if (e.get("detail") or {}).get("paper_session") == session_id])


def select_window(df: pd.DataFrame, settings: Any) -> tuple[pd.DataFrame, str]:
    """Return the paper-trading window according to ``paper.window``."""
    mode = settings.paper.window
    if mode == "holdout":
        _, hold = split_holdout(df, settings.evaluation.holdout_frac)
        if len(hold) >= 10:
            return hold, "holdout"
        mode = "recent"  # fall back rather than run on nothing
    if mode == "recent":
        n = max(10, int(settings.paper.window_bars))
        return df.iloc[-n:], "recent"
    return df, "full"


def create_session(
    settings: Any,
    memory: Memory,
    genome: dict[str, Any] | None = None,
    name: str | None = None,
    *,
    deploy_gate: bool = True,
) -> int:
    """Create a new paper session.  Uses the hall-of-fame champion by default.

    ``deploy_gate`` runs the critic against the sealed risk limits first: a genome
    that breaks a hard limit is **refused** here, with the reasons recorded as a
    lesson in the memory, instead of being quietly paper-traded.  Tests that only
    exercise the paper mechanics pass ``deploy_gate=False``; the CLI never does.
    """
    if genome is None:
        champ = memory.champion()
        if champ is None:
            raise RuntimeError(
                "no champion in the hall of fame; run `evolve` first (or pass a genome)"
            )
        genome = champ["genome"]
    genome = validate_genome(genome)

    df = load_dataframe(settings)

    if deploy_gate:
        from . import critic as critic_mod

        verdict, _res, _cut = critic_mod.verdict_for_genome(
            settings, genome, df, window=str(getattr(settings.paper, "window", "holdout"))
        )
        if not verdict.approved:
            detail = verdict.text
            if verdict.warnings:
                detail += " Warnings: " + "; ".join(verdict.warnings)
            memory.add_lesson(
                None,
                f"Deploy refused by the critic: {detail}",
                tags=["critic", "refused", f"family:{genome['family']}"],
            )
            raise RuntimeError(f"the critic refused this deployment - {detail}")

    window, label = select_window(df, settings)
    sid = memory.create_paper_session(
        name=name or f"{settings.data.symbol}-{label}",
        genome=genome,
        window=label,
        total_bars=len(window),
        start_time=str(window.index[0]),
        end_time=str(window.index[-1]),
        initial_cash=settings.backtest.initial_cash,
        settings=session_settings(settings),
    )
    return sid


def _window_for_session(df: pd.DataFrame, session: dict[str, Any], settings: Any) -> pd.DataFrame:
    start = session.get("start_time")
    end = session.get("end_time")
    if start and end:
        w = df.loc[pd.Timestamp(start) : pd.Timestamp(end)]
        if len(w) >= 2:
            return w
    window, _ = select_window(df, settings)
    return window


def _closed_trades(res_trades: pd.DataFrame) -> pd.DataFrame:
    if len(res_trades) == 0:
        return res_trades
    return res_trades[res_trades["exit_reason"] != "end_of_data"]


def run_session(
    settings: Any,
    memory: Memory,
    session_id: int,
    *,
    mode: str = "all",
    max_ticks: int | None = None,
    df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Advance the session cursor by one block (``step``) or to the end (``all``)."""
    session = memory.get_paper_session(session_id)
    if session is None:
        raise RuntimeError(f"paper session {session_id} not found")
    genome = validate_genome(session["genome"])

    df = df if df is not None else load_dataframe(settings)
    window = _window_for_session(df, session, settings)
    total = len(window)
    cursor = int(session["cursor"])
    step = max(1, int(settings.paper.step_bars))
    tick_limit = settings.paper.max_ticks if max_ticks is None else max_ticks

    cfg = session.get("settings") or {}
    fee_bps = float(cfg.get("fee_bps", settings.backtest.fee_bps))
    slippage_bps = float(cfg.get("slippage_bps", settings.backtest.slippage_bps))
    allow_short = bool(cfg.get("allow_short", settings.backtest.allow_short))
    timeframe = str(cfg.get("timeframe", settings.data.timeframe))
    # Whatever the session says, the rail is sized to pass the sealed cap: a bigger
    # order would be refused by the gate, and a session that never trades is worse
    # than one that trades in the size the live rail is allowed to use.
    position_size = min(
        float(cfg.get("position_size", settings.backtest.position_size)),
        effective_position_size(settings),
    )
    gate = _risk_gate(settings)
    switch = KillSwitch(kill_switch_path(settings))
    symbol = str(getattr(settings.data, "symbol", "BTC/USDT")) or "BTC/USDT"
    audit = {"paper_session": session_id, "symbol": symbol}

    ticks = 0
    last_blocked: int | None = None  # the last tick owns the completed prefix
    notices: list[dict[str, Any]] = []
    while cursor < total:
        cursor = min(cursor + step, total)
        prefix = window.iloc[:cursor]
        res = run_orders(
            prefix,
            genome,
            initial_cash=float(session["initial_cash"]),
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            position_size=position_size,
            allow_short=allow_short,
            timeframe=timeframe,
            symbol=symbol,
            gate=gate,
            kill_switch=switch,
            memory=memory,
            context=audit,
            client_prefix=f"ps{session_id}",
        )
        trades = _closed_trades(res.trades)
        equity_points = [(str(ts), float(v)) for ts, v in res.equity.items()]
        status = "done" if cursor >= total else "active"
        memory.replace_paper_results(
            session_id,
            trades.to_dict("records"),
            equity_points,
            cursor,
            float(res.cash),
            status,
        )
        last_blocked = len(res.blocked)
        ticks += 1
        # A tick that stopped trading - or came close to it - is worth telling someone
        # about.  `RiskWatch` announces each halt once and each warning once per
        # approach, so this costs one file read (and one database read only when there
        # is something to explain).
        notices.extend(risk_notices(settings))
        if mode == "step":
            break
        if tick_limit and ticks >= int(tick_limit):
            break

    status = paper_status(settings, memory, session_id, blocked=last_blocked)
    if notices:
        status["notices"] = notices
    return status


def risk_notices(settings: Any) -> list[dict[str, Any]]:
    """Ask the alert channel what is new, without ever letting it break the session.

    A notification is worth nothing if it can stop trading: the failure is returned as a
    notice the caller prints, so "the alert did not go out" is visible instead of silent.
    """
    try:
        from .notify import RiskWatch

        return RiskWatch(settings).notify()
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return [
            {
                "kind": "notify_error",
                "sent": False,
                "reason": type(exc).__name__,
                "message": f"the alert channel could not be read: {exc}",
            }
        ]


def paper_status(
    settings: Any, memory: Memory, session_id: int, *, blocked: int | None = None
) -> dict[str, Any]:
    session = memory.get_paper_session(session_id)
    if session is None:
        raise RuntimeError(f"paper session {session_id} not found")
    trades = memory.paper_trades(session_id)
    equity = memory.paper_equity(session_id)
    cfg = session.get("settings") or {}
    timeframe = str(cfg.get("timeframe", settings.data.timeframe))

    trades_df = pd.DataFrame(trades)
    eq = pd.Series(
        [row["equity"] for row in equity],
        index=pd.to_datetime([row["ts"] for row in equity], utc=True) if equity else None,
        dtype="float64",
    )
    metrics = compute_metrics(eq, trades_df, float(session["initial_cash"]), timeframe)
    # Where the capital came from, so a reader never has to guess: the ledger, or the
    # config's `backtest.initial_cash` when nobody has credited the account.
    funded = float(memory.session_allocations().get(session_id, 0.0))
    return {
        "session_id": session_id,
        "funded_capital": funded,
        "capital_source": "ledger" if funded > 0 else "config",
        "name": session["name"],
        "window": session["window"],
        "status": session["status"],
        "cursor": session["cursor"],
        "total_bars": session["total_bars"],
        "progress": (session["cursor"] / session["total_bars"]) if session["total_bars"] else 0.0,
        "cash": session["cash"],
        "equity": float(eq.iloc[-1]) if len(eq) else float(session["initial_cash"]),
        "initial_cash": session["initial_cash"],
        "n_trades": int(metrics["n_trades"]),
        "total_return": float(metrics["total_return"]),
        "equity_points": len(equity),
        "blocked_orders": (
            int(blocked) if blocked is not None else blocked_orders(memory, session_id)
        ),
        "genome": session["genome"],
        "genome_description": describe(session["genome"]),
        "metrics": metrics,
        "start_time": session["start_time"],
        "end_time": session["end_time"],
    }


def cli(settings: Any, args: Any) -> int:
    import json

    # Imported here, not at module scope: the session runner must not depend on the
    # account, and only the CLI path is allowed to move capital at all.
    from . import capital as capital_mod

    memory = Memory(settings.memory.db)

    if args.reset:
        sid = args.session_id
        if sid is None:
            active = memory.active_paper_session()
            sid = active["id"] if active else None
        if sid is None:
            print("no paper session to reset", file=__import__("sys").stderr)
            return 1
        memory.reset_paper_session(sid)
        print(f"paper session {sid} reset (explicit --reset)")
        return 0

    if args.create:
        chunk = None
        if args.champion:
            chunk = memory.champion()
        fund = getattr(args, "fund", None)
        if fund is not None:
            # The refusal has to cost nothing: the ledger is checked *before* a session
            # exists, so a credit that could never be made leaves no empty session behind.
            try:
                capital_mod.preflight_allocate(settings, memory, fund, None)
            except capital_mod.CapitalError as exc:
                print(f"paper: {exc}", file=__import__("sys").stderr)
                return 1
        try:
            sid = create_session(settings, memory, genome=chunk["genome"] if chunk else None)
        except RuntimeError as exc:
            print(f"paper: {exc}", file=__import__("sys").stderr)
            return 1
        st = paper_status(settings, memory, sid)
        print(f"created paper session {sid}: {st['name']} window={st['window']} bars={st['total_bars']}")
        print(f"  genome: {st['genome_description']}")
        if fund is not None:
            try:
                out = capital_mod.fund_session(
                    settings,
                    memory,
                    sid,
                    fund,
                    reason=getattr(args, "reason", "") or f"funded paper session {sid}",
                )
            except capital_mod.CapitalError as exc:
                print(
                    f"paper: session {sid} was created but could not be funded: {exc}",
                    file=__import__("sys").stderr,
                )
                return 1
            entry = out["entry"]
            print(
                f"  funded with {abs(entry['amount']):.2f} {entry['currency'] or ''} - "
                f"capital now {out['capital']:.2f} (ledger #{entry['id']})"
            )
        return 0

    session_id = args.session_id
    if session_id is None:
        active = memory.active_paper_session()
        if active is None:
            print("no paper session; create one with `paper --create`", file=__import__("sys").stderr)
            return 1
        session_id = active["id"]

    if getattr(args, "fund", None) is not None:
        try:
            out = capital_mod.fund_session(
                settings,
                memory,
                session_id,
                args.fund,
                reason=getattr(args, "reason", "") or f"funded paper session {session_id}",
            )
        except capital_mod.CapitalError as exc:
            print(f"paper: {exc}", file=__import__("sys").stderr)
            return 1
        entry = out["entry"]
        print(
            f"session {session_id} funded with {abs(entry['amount']):.2f} "
            f"{entry['currency'] or ''} - capital now {out['capital']:.2f} (ledger #{entry['id']})"
        )
        return 0

    if getattr(args, "release", False):
        try:
            out = capital_mod.release(
                settings,
                reason=getattr(args, "reason", "") or f"released paper session {session_id}",
                memory=memory,
                session_id=session_id,
            )
        except capital_mod.CapitalError as exc:
            print(f"paper: {exc}", file=__import__("sys").stderr)
            return 1
        entry = out["entry"]
        print(
            f"session {session_id} released {abs(entry['amount']):.2f} "
            f"{entry['currency'] or ''} - capital now {out['capital']:.2f} (ledger #{entry['id']})"
        )
        return 0

    if args.status:
        st = paper_status(settings, memory, session_id)
        if args.json:
            print(json.dumps(st, indent=2, default=str))
        else:
            _print_status(st)
        return 0

    if args.all:
        st = run_session(settings, memory, session_id, mode="all")
        if args.json:
            print(json.dumps(st, indent=2, default=str))
        else:
            _print_status(st)
        return 0

    if args.run:
        st = run_session(settings, memory, session_id, mode="step")
        if args.json:
            print(json.dumps(st, indent=2, default=str))
        else:
            _print_status(st)
        return 0

    # default: show status
    st = paper_status(settings, memory, session_id)
    _print_status(st)
    return 0


def _print_status(st: dict[str, Any]) -> None:
    print(
        f"paper session {st['session_id']} '{st['name']}' window={st['window']} "
        f"status={st['status']}"
    )
    print(
        f"  cursor {st['cursor']}/{st['total_bars']} ({st['progress']*100:.1f}%) "
        f"cash={st['cash']:.2f} equity={st['equity']:.2f} "
        f"return={st['total_return']*100:.2f}% trades={st['n_trades']}"
    )
    if st.get("funded_capital"):
        print(
            f"  capital {st['initial_cash']:.2f} funded from the ledger "
            f"({st['funded_capital']:.2f} allocated)"
        )
    if st.get("blocked_orders"):
        print(f"  risk gate refusals on record: {st['blocked_orders']}")
    for notice in st.get("notices") or []:
        where = "sent" if notice.get("sent") else "NOT sent"
        print(f"  alert [{notice.get('kind')}] {where}: {notice.get('message')}")
    print(f"  genome: {st['genome_description']}")
