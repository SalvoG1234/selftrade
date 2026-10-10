"""Forward paper trading.

Simulates the champion strategy over an **out-of-sample** window, advancing a
**persistent cursor** in blocks of ``paper.step_bars``.  It reuses the *same*
engine as the backtester (:func:`selftrade.backtest.simulate`), so paper and
backtest agree by construction.

Because each step simply re-simulates the window prefix and rewrites the derived
rows, finishing the window in one shot, in many steps, or after a restart all
produce identical trades and equity.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .backtest import compute_metrics, simulate
from .data import load_dataframe, split_holdout
from .genome import describe, validate_genome
from .memory import Memory


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
        settings={
            "fee_bps": settings.backtest.fee_bps,
            "slippage_bps": settings.backtest.slippage_bps,
            "position_size": settings.backtest.position_size,
            "allow_short": settings.backtest.allow_short,
            "timeframe": settings.data.timeframe,
        },
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
    position_size = float(cfg.get("position_size", settings.backtest.position_size))
    allow_short = bool(cfg.get("allow_short", settings.backtest.allow_short))
    timeframe = str(cfg.get("timeframe", settings.data.timeframe))

    ticks = 0
    while cursor < total:
        cursor = min(cursor + step, total)
        prefix = window.iloc[:cursor]
        res = simulate(
            prefix,
            genome,
            initial_cash=float(session["initial_cash"]),
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            position_size=position_size,
            allow_short=allow_short,
            timeframe=timeframe,
        )
        trades = _closed_trades(res.trades)
        equity_points = [(str(ts), float(v)) for ts, v in res.equity.items()]
        status = "done" if cursor >= total else "active"
        memory.replace_paper_results(
            session_id,
            trades.to_dict("records"),
            equity_points,
            cursor,
            float(res.equity.iloc[-1]),
            status,
        )
        ticks += 1
        if mode == "step":
            break
        if tick_limit and ticks >= int(tick_limit):
            break

    return paper_status(settings, memory, session_id)


def paper_status(settings: Any, memory: Memory, session_id: int) -> dict[str, Any]:
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
    return {
        "session_id": session_id,
        "name": session["name"],
        "window": session["window"],
        "status": session["status"],
        "cursor": session["cursor"],
        "total_bars": session["total_bars"],
        "progress": (session["cursor"] / session["total_bars"]) if session["total_bars"] else 0.0,
        "cash": session["cash"],
        "initial_cash": session["initial_cash"],
        "n_trades": int(metrics["n_trades"]),
        "total_return": float(metrics["total_return"]),
        "equity_points": len(equity),
        "genome": session["genome"],
        "genome_description": describe(session["genome"]),
        "metrics": metrics,
        "start_time": session["start_time"],
        "end_time": session["end_time"],
    }


def cli(settings: Any, args: Any) -> int:
    import json

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
        try:
            sid = create_session(settings, memory, genome=chunk["genome"] if chunk else None)
        except RuntimeError as exc:
            print(f"paper: {exc}", file=__import__("sys").stderr)
            return 1
        st = paper_status(settings, memory, sid)
        print(f"created paper session {sid}: {st['name']} window={st['window']} bars={st['total_bars']}")
        print(f"  genome: {st['genome_description']}")
        return 0

    session_id = args.session_id
    if session_id is None:
        active = memory.active_paper_session()
        if active is None:
            print("no paper session; create one with `paper --create`", file=__import__("sys").stderr)
            return 1
        session_id = active["id"]

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
        f"cash={st['cash']:.2f} return={st['total_return']*100:.2f}% trades={st['n_trades']}"
    )
    print(f"  genome: {st['genome_description']}")
