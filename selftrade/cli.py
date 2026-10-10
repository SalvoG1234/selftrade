"""Command line interface: ``python -m selftrade <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_settings
from .data import split_holdout
from .evaluate import Holdout, evaluate_folds, holdout_eval
from .genome import describe, sample_genome
from .memory import Memory

DEFAULT_CONFIG = "config.yaml"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load(args) -> Any:
    overrides = getattr(args, "set", None) or []
    config = getattr(args, "config", None) or DEFAULT_CONFIG
    path = config if Path(config).exists() else None
    if path is None and config != DEFAULT_CONFIG:
        raise ConfigError(f"config file not found: {config}")
    return load_settings(path, overrides)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _load_df(settings):
    from .data import load_dataframe

    return load_dataframe(settings)


def _champion_or_family(settings, args):
    mem = Memory(settings.memory.db)
    if getattr(args, "champion", False):
        champ = mem.champion()
        if champ is None:
            print("no champion in the hall of fame yet; run `evolve` first", file=sys.stderr)
            return None
        return champ["genome"], "champion"
    family = getattr(args, "family", None)
    g = sample_genome(family, None) if family else sample_genome(None, None)
    return g, "sampled"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(args) -> int:
    settings = _load(args)
    mem = Memory(settings.memory.db)
    from . import data as data_mod
    from . import indicators, backtest, evaluate  # noqa: F401

    print(f"selftrade {__version__} doctor")
    print(f"python: {sys.version.split()[0]}")
    try:
        import numpy, pandas  # noqa: F401

        print(f"deps: numpy {numpy.__version__}, pandas {pandas.__version__} OK")
    except Exception as exc:  # pragma: no cover
        print(f"deps: MISSING ({exc})")

    # data
    try:
        df = _load_df(settings)
        print(
            f"data: source={settings.data.source} rows={len(df)} "
            f"[{df.index[0]} .. {df.index[-1]}] timeframe={settings.data.timeframe}"
        )
    except Exception as exc:
        print(f"data: NOT AVAILABLE ({exc}) - run `data --rows N`")

    # ollama
    from .llm import OllamaClient

    if settings.llm.enabled:
        client = OllamaClient(
            settings.llm.base_url,
            settings.llm.model,
            settings.llm.embed_model,
            settings.llm.num_ctx,
            settings.llm.num_predict,
            settings.llm.temperature,
            min(settings.llm.timeout, 10.0),
        )
        if client.available():
            print(f"ollama: UP at {settings.llm.base_url}; models={client.models()}")
        else:
            print(
                f"ollama: DOWN at {settings.llm.base_url} "
                "(proposals will be deterministic; this is not fatal)"
            )
    else:
        print("ollama: disabled in config (llm.enabled=false)")

    # REA
    try:
        from . import rea as rea_mod

        print("rea: " + rea_mod.doctor_line(settings))
    except Exception as exc:  # pragma: no cover - subsystem optional
        print(f"rea: unavailable ({exc})")

    # MT5
    try:
        from . import mt5 as mt5_mod

        for line in mt5_mod.doctor_lines(settings):
            print("mt5: " + line)
    except Exception as exc:  # pragma: no cover - subsystem optional
        print(f"mt5: unavailable ({exc})")

    # memory
    st = mem.stats()
    print(f"memory: {settings.memory.db} {st}")
    print("doctor: finished (missing subsystems are reported, not fatal)")
    return 0


def cmd_data(args) -> int:
    settings = _load(args)
    from .data import fetch_klines, make_synthetic, save_csv

    rows = args.rows or settings.data.synthetic_rows
    if args.fetch_mt5:
        from . import mt5 as mt5_mod

        df = mt5_mod.fetch_snapshot(settings)
    elif args.fetch:
        print(
            f"fetching real candles: {settings.data.symbol} "
            f"{settings.data.timeframe} x{rows} from {settings.data.fetch_base_url}",
            file=sys.stderr,
        )
        df = fetch_klines(
            settings.data.symbol,
            settings.data.timeframe,
            rows,
            base_url=settings.data.fetch_base_url,
            timeout=settings.data.fetch_timeout,
        )
        print(
            f"fetched {len(df)} real candles {df.index[0]} .. {df.index[-1]} "
            f"(close {df['close'].iloc[0]:.2f} -> {df['close'].iloc[-1]:.2f})",
            file=sys.stderr,
        )
    else:
        df = make_synthetic(rows, seed=settings.evolution.seed, timeframe=settings.data.timeframe)

    if args.out:
        out = args.out
    elif args.fetch:
        sym = settings.data.symbol.replace("/", "").upper()
        out = f"data/{sym}_{settings.data.timeframe}.csv"
    else:
        out = settings.data.csv
    p = save_csv(df, out)
    print(f"wrote {len(df)} rows to {p} ({df.index[0]} .. {df.index[-1]})")
    return 0


def cmd_backtest(args) -> int:
    settings = _load(args)
    df = _load_df(settings)
    got = _champion_or_family(settings, args)
    if got is None:
        return 1
    genome, origin = got
    dev, hold = split_holdout(df, settings.evaluation.holdout_frac)
    rep = evaluate_folds(dev, genome, settings)
    out = {
        "origin": origin,
        "genome": genome,
        "description": describe(genome),
        "fitness": rep["fitness"],
        "metric": rep["metric"],
        "folds": rep["folds"],
        "n_trades": rep["n_trades"],
        "max_drawdown": rep["max_drawdown"],
        "passed": rep["passed"],
        "gate_reason": rep["gate_reason"],
    }
    if args.holdout and len(hold):
        guard = Holdout(hold)
        out["holdout"] = holdout_eval(guard.read(), genome, settings)
    if args.json:
        _print_json(out)
    else:
        print(f"backtest ({origin}) {describe(genome)}")
        print(f"  fitness={rep['fitness']:.4f} metric={rep['metric']} folds={len(rep['folds'])}")
        print(f"  n_trades={rep['n_trades']} max_drawdown={rep['max_drawdown']:.3f}")
        print(f"  gate: {'PASS' if rep['passed'] else 'FAIL: ' + rep['gate_reason']}")
        for i, m in enumerate(rep["folds"]):
            print(
                f"  fold {i}: {settings.evaluation.metric}={m.get(settings.evaluation.metric, 0):.3f} "
                f"trades={int(m.get('n_trades', 0))} dd={m.get('max_drawdown', 0):.3f}"
            )
        if "holdout" in out:
            print(f"  holdout: {out['holdout']['metrics']}")
    return 0 if rep["passed"] else 2


def cmd_evolve(args) -> int:
    settings = _load(args)
    from .agent import Evolver

    session = None
    if args.session:
        from .session import Session

        session = Session(settings.session_file)
        session.mark_running(generations=args.generations or settings.evolution.generations)
    evolver = Evolver(settings, session=session, log=print)
    evolver.no_rea = bool(getattr(args, "no_rea", False))
    try:
        result = evolver.run(
            generations=args.generations,
            note=args.note or "",
            use_llm=not args.no_llm,
            quiet=args.quiet,
        )
    except Exception:  # keep the session file honest on failure
        if session is not None:
            session.mark_finished(status="error")
        raise
    if session is not None:
        session.mark_finished(status="stopped" if result.stopped else "done")
    print(f"evolve: run #{result.run_id} -> {result.report_md}")
    if result.champion:
        print(f"evolve: champion {describe(result.champion.genome)} fitness={result.champion.fitness:.3f}")
    else:
        print("evolve: no candidate passed all gates (nothing promoted)")
    if result.regime_warning:
        print(f"evolve: WARNING {result.regime_warning}")
    return 0


def cmd_report(args) -> int:
    settings = _load(args)
    mem = Memory(settings.memory.db)
    run = mem.last_run()
    if run is None:
        print("no runs recorded yet", file=sys.stderr)
        return 1
    if args.json:
        _print_json(run)
    else:
        print(f"run #{run['id']} at {run['ts']} outcome={run['outcome']}")
        print(f"note: {run['note']}")
        s = run["summary"] or {}
        for k in ("generations", "candidates", "passed", "champion", "champion_fitness"):
            if k in s:
                print(f"  {k}: {s[k]}")
        if s.get("report_md"):
            print(f"  report: {s['report_md']}")
            print(f"  json:   {s.get('report_json')}")
        h = s.get("holdout")
        if h:
            print(f"  holdout metrics: {h}")
    return 0


def cmd_hall(args) -> int:
    settings = _load(args)
    mem = Memory(settings.memory.db)
    rows = mem.hall_of_fame(limit=args.limit, min_fitness=args.min_fitness)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("hall of fame is empty (no candidate has passed all gates yet)")
        return 0
    for r in rows:
        print(f"#{r['id']} fitness={r['fitness']:.4f} {describe(r['genome'])}")
    return 0


def cmd_export(args) -> int:
    settings = _load(args)
    from .freqtrade_export import export_strategy

    mem = Memory(settings.memory.db)
    if args.champion:
        champ = mem.champion()
        if champ is None:
            print("no champion in the hall of fame yet", file=sys.stderr)
            return 1
        genome = champ["genome"]
    else:
        genome = sample_genome(args.family, None)
    out, path = export_strategy(
        genome, args.name, args.timeframe or settings.data.timeframe, args.out or "user_data/strategies"
    )
    print(f"exported {out} -> {path}")
    return 0


def cmd_invest(args) -> int:
    """Simulate really investing a fixed amount in the chosen genome."""
    settings = _load(args)
    from . import invest as invest_mod

    df = _load_df(settings)
    got = _champion_or_family(settings, args)
    if got is None:
        return 1
    genome, origin = got
    cash = args.initial_cash or settings.backtest.initial_cash
    try:
        report = invest_mod.simulate_investment(
            df,
            genome,
            settings,
            window=args.window,
            initial_cash=cash,
            origin=origin,
            data_path=settings.data.csv,
        )
    except invest_mod.InvestError as exc:
        print(f"invest: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(report)
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    md_path = args.out or f"reports/invest_{stamp}.md"
    html_path = str(Path(md_path).with_suffix(".html"))
    json_path = str(Path(md_path).with_suffix(".json"))
    written = invest_mod.write_reports(
        report, out_md=md_path, out_html=html_path, out_json=json_path
    )

    s, b, cd = report["strategy"], report["buy_hold"], report["cost_drag"]
    cur = report["currency"]
    print(f"investment simulation ({report['origin']}) {report['description']}")
    print(
        f"  data: {report['data']['symbol']} {report['data']['timeframe']} "
        f"{report['data']['rows_window']} bars "
        f"{report['data']['start'][:10]}..{report['data']['end'][:10]} "
        f"window={report['window']}"
    )
    print(f"  cash {report['params']['initial_cash']:,.0f} {cur}")
    print(
        f"  strategy : {s['final_equity']:,.2f} {cur} "
        f"({s['total_return'] * 100:+.1f}%, CAGR {s['cagr'] * 100:+.2f}%, DD {s['max_drawdown'] * 100:.1f}%)"
    )
    print(
        f"  buy&hold : {b['final_equity']:,.2f} {cur} "
        f"({b['total_return'] * 100:+.1f}%, CAGR {b['cagr'] * 100:+.2f}%, DD {b['max_drawdown'] * 100:.1f}%)"
    )
    print(
        f"  trades {s['n_trades']}, win {s['win_rate'] * 100:.1f}%, "
        f"PF {s['profit_factor']:.2f}, costs paid {cd['cost_paid']:,.2f} {cur}"
    )
    print(f"  verdict: {report['verdict']['text']}")
    for kind, path in written.items():
        print(f"  wrote {kind}: {path}")
    return 0


def cmd_paper(args) -> int:
    settings = _load(args)
    from . import paper as paper_mod

    return paper_mod.cli(settings, args)


def cmd_serve(args) -> int:
    settings = _load(args)
    from .dashboard import serve

    host = args.host or settings.dashboard.host
    port = args.port or settings.dashboard.port
    serve(settings, host=host, port=port)
    return 0


def cmd_session(args) -> int:
    settings = _load(args)
    from .session import Session

    s = Session(settings.session_file)
    action = args.action
    if action == "status":
        print(json.dumps(s.status(), indent=2, default=str))
        return 0
    if action == "start":
        s.request_start(generations=args.generations or settings.evolution.generations)
        print(f"start requested in {settings.session_file}")
        return 0
    if action == "pause":
        s.request("pause")
        print("pause requested (takes effect at the next generation boundary)")
        return 0
    if action == "resume":
        s.request("resume")
        print("resume requested")
        return 0
    if action == "stop":
        s.request("stop")
        print("stop requested (the run finishes the generation then writes the report)")
        return 0
    print(f"unknown session action: {action}", file=sys.stderr)
    return 1


def cmd_rea(args) -> int:
    settings = _load(args)
    from . import rea as rea_mod

    return rea_mod.cli(settings, args)


def cmd_mcp(args) -> int:
    settings = _load(args)
    from . import rea as rea_mod

    path = rea_mod.write_mcp_config(settings, args.out)
    print(f"wrote MCP config -> {path}")
    return 0


def cmd_mt5(args) -> int:
    settings = _load(args)
    from . import mt5 as mt5_mod

    return mt5_mod.cli(settings, args)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def _extract_globals(argv: list[str]) -> tuple[list[str], str | None, list[str]]:
    """Pull ``--config`` / ``--set`` out of ``argv`` wherever they appear.

    ``argparse`` subparsers parse into a fresh namespace and copy every
    attribute back, so a subparser default would silently clobber a value given
    before the subcommand.  Handling these two options here keeps them
    position-independent and mergeable.
    """
    rest: list[str] = []
    config: str | None = None
    sets: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--config":
            if i + 1 >= len(argv):
                raise ConfigError("--config requires a path")
            config = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--config="):
            config = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--set":
            if i + 1 >= len(argv):
                raise ConfigError("--set requires section.key=value")
            sets.append(argv[i + 1])
            i += 2
            continue
        if tok.startswith("--set="):
            sets.append(tok.split("=", 1)[1])
            i += 1
            continue
        rest.append(tok)
        i += 1
    return rest, config, sets


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="selftrade", description="self-improving trading strategy search")
    p.add_argument("--version", action="version", version=f"selftrade {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("doctor", help="diagnose subsystems (never fails on a missing one)")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("data", help="generate / download / snapshot OHLCV")
    sp.add_argument("--rows", type=int)
    sp.add_argument("--fetch", action="store_true")
    sp.add_argument("--fetch-mt5", action="store_true")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_data)

    sp = sub.add_parser("backtest", help="evaluate one genome")
    sp.add_argument("--family")
    sp.add_argument("--champion", action="store_true")
    sp.add_argument("--holdout", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("evolve", help="run the search loop")
    sp.add_argument("--generations", type=int)
    sp.add_argument("--no-llm", action="store_true")
    sp.add_argument("--no-rea", action="store_true")
    sp.add_argument("--rea-live", action="store_true")
    sp.add_argument("--session", action="store_true")
    sp.add_argument("--note", default="")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_evolve)

    sp = sub.add_parser("report", help="report of the last run")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("hall", help="hall of fame")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--min-fitness", type=float, default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_hall)

    sp = sub.add_parser("export", help="export a freqtrade strategy")
    sp.add_argument("--champion", action="store_true")
    sp.add_argument("--family")
    sp.add_argument("--out")
    sp.add_argument("--name")
    sp.add_argument("--timeframe")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("rea", help="REA: reverse-engineering evidence (optional)")
    sp.add_argument("action", choices=["plan", "run", "doctor"])
    sp.add_argument("--target", type=int)
    sp.add_argument("--live", action="store_true")
    sp.set_defaults(func=cmd_rea)

    sp = sub.add_parser("mcp", help="generate MCP config for REA + Ruflo")
    sp.add_argument("--out", default=".mcp.json")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("mt5", help="MetaTrader 5 / Trading.com bridge")
    sp.add_argument(
        "action",
        choices=["doctor", "symbols", "bars", "account", "positions", "costs", "order", "close"],
    )
    sp.add_argument("--symbol")
    sp.add_argument("--side", choices=["buy", "sell"], default="buy")
    sp.add_argument("--volume", type=float)
    sp.add_argument("--sl-points", type=int)
    sp.add_argument("--tp-points", type=int)
    sp.add_argument("--ticket", type=int)
    sp.add_argument("--out")
    sp.add_argument("--live", action="store_true", help="press the trigger (config mt5.live must also be armed)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_mt5)

    sp = sub.add_parser("invest", help="what if we had really invested this cash")
    sp.add_argument("--champion", action="store_true")
    sp.add_argument("--family")
    sp.add_argument("--window", choices=["holdout", "full", "recent"], default="holdout")
    sp.add_argument("--initial-cash", type=float, dest="initial_cash")
    sp.add_argument("--out", help="markdown path (.html/.json written next to it)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_invest)

    sp = sub.add_parser("paper", help="forward paper trading")
    sp.add_argument("--create", action="store_true")
    sp.add_argument("--run", action="store_true")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--reset", action="store_true")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--session", type=int, dest="session_id")
    sp.add_argument("--champion", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_paper)

    sp = sub.add_parser("serve", help="local dashboard")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("session", help="control the tracked loop")
    sp.add_argument("action", choices=["status", "start", "pause", "resume", "stop"])
    sp.add_argument("--generations", type=int)
    sp.set_defaults(func=cmd_session)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        rest, config, sets = _extract_globals(argv)
        args = parser.parse_args(rest)
        args.config = config or DEFAULT_CONFIG
        args.set = sets
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
