"""Command line interface: ``python -m selftrade <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_settings
from .data import split_holdout
from .evaluate import Holdout, evaluate_folds, holdout_eval, metric_value
from .genome import describe, sample_genome
from .memory import Memory, StorageError

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


def _print_account_check(report: dict[str, Any]) -> None:
    """The read-only account check, for a human: one line per step, then the detail."""
    where = "Spot Testnet" if report["host_is_testnet"] else (
        "PRODUCTION - real money" if report["production"] else "unknown host"
    )
    print(f"binance account check ({where}) - read-only: no order can come out of this command")
    print(f"  {'host':<13} {report['base_url']}")
    print(f"  {'mode':<13} {report['mode']}   armed={report['armed']}")
    creds = ", ".join(
        f"{name}={'set' if present else 'MISSING'}"
        for name, present in report["credentials"].items()
    )
    print(f"  {'credentials':<13} {creds}")
    for entry in report["steps"]:
        flag = "ok" if entry["ok"] else "FAIL"
        print(f"  {flag:<4} {entry['name']:<12} {entry['detail']}")
    account = report["account"]
    if account["balances"]:
        print(
            f"  {'balances':<17}{len(account['balances'])} shown of "
            f"{account['balances_total']} non-zero asset(s), quote {account['quote_asset']}"
        )
        for row in account["balances"]:
            print(f"      {row['asset']:<8} {row['free']:>18,.8f}  (locked {row['locked']:,.8f})")
    orders = report["open_orders"]["orders"]
    if orders:
        print(f"  {'open orders':<17}{len(orders)}")
        for order in orders:
            print(
                f"      {order['symbol']} {order['side']} {order['qty']} {order['type']} "
                f"-> {order['status']} (id {order['id']})"
            )
    limits = report.get("limits") or {}
    if limits:
        print(
            f"  {'rate budget':<17}{limits.get('weight_used')}/"
            f"{limits.get('max_weight_per_min')} weight, cooldown {limits.get('cooldown_s')}s"
        )
    for warning in report["warnings"]:
        print(f"  warning: {warning}")
    for problem in report["problems"]:
        print(f"  problem: {problem}")
    for hint in report["hints"]:
        print(f"  hint:    {hint}")
    ready = report.get("trade_ready")
    print(
        f"  verdict: {report['verdict']}"
        + ("" if ready is None else f"   ready to trade: {ready}")
    )


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
    # The memory store is opened first and *guarded*: this command exists to report what is
    # broken, so a store it cannot open must be one of the lines it prints - not the traceback
    # that stops it before it can look at anything else.  Every other subsystem below is
    # guarded the same way.
    mem = None
    memory_note = ""
    try:
        mem = Memory(settings.memory.db)
    except StorageError as exc:
        memory_note = str(exc)
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
    if mem is None:
        print(f"memory: NOT AVAILABLE - {memory_note}")
    else:
        try:
            print(f"memory: {settings.memory.db} {mem.stats()}")
        except Exception as exc:  # noqa: BLE001 - the read itself can fail too
            print(f"memory: NOT AVAILABLE - {type(exc).__name__}: {exc}")
    if mem is None:
        print("doctor: finished, but nothing that stores state will work until the memory "
              "store above is readable (a missing Ollama or REA is not fatal; this is)")
    else:
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
        # What the market did on the very same test slices: the number the folds have to be
        # read against, measured by the same metric and the same cost model.
        "baseline_folds": rep["baseline_folds"],
        "baseline_mean": rep["baseline_mean"],
        "metric_excess": rep["metric_excess"],
        "n_trades": rep["n_trades"],
        "max_drawdown": rep["max_drawdown"],
        "passed": rep["passed"],
        "gate_reason": rep["gate_reason"],
    }
    if args.holdout and len(hold):
        guard = Holdout(hold)
        h = holdout_eval(guard.read(), genome, settings)
        # keep the JSON interface stable: holdout_eval also carries the equity
        # series and the trade frame (the Evolver needs them), which do not belong
        # in a printed payload.
        out["holdout"] = {
            "metrics": h["metrics"],
            "n_trades": h["n_trades"],
            "baseline": h["baseline"],
        }
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
        # The benchmark, said in one line: what the same cash did simply holding the asset
        # over the same test slices, and what the strategy earned over that.
        print(
            f"  market (buy & hold): {settings.evaluation.metric}="
            f"{rep['baseline_mean']:.3f} | excess={rep['metric_excess']:+.3f}"
        )
        if "holdout" in out:
            print(f"  holdout: {out['holdout']['metrics']}")
            hb = out["holdout"]["baseline"]
            print(
                f"  holdout market (buy & hold): {settings.evaluation.metric}="
                f"{metric_value(hb, settings.evaluation.metric):.3f} | return="
                f"{hb.get('total_return', 0.0) * 100:+.2f}%"
            )
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


def _window_simulation(settings, genome, window: str):
    """Simulate one genome on a window; shared by `autopsy` and `critic`."""
    from .backtest import simulate
    from .genome import validate_genome
    from .invest import select_window

    df = _load_df(settings)
    cut = select_window(df, settings, window)
    bt = settings.backtest
    res = simulate(
        cut,
        validate_genome(genome),
        initial_cash=bt.initial_cash,
        fee_bps=bt.fee_bps,
        slippage_bps=bt.slippage_bps,
        position_size=bt.position_size,
        allow_short=bt.allow_short,
        timeframe=settings.data.timeframe,
    )
    return cut, res


def cmd_autopsy(args) -> int:
    """Post-mortem of the losing trades: worst losses, clusters, rules to change."""
    settings = _load(args)
    from . import autopsy as autopsy_mod

    got = _champion_or_family(settings, args)
    if got is None:
        return 1
    genome, origin = got
    cut, res = _window_simulation(settings, genome, args.window)
    report = autopsy_mod.autopsy(
        res.trades,
        cut,
        genome,
        worst_n=args.worst,
        min_trades=settings.evaluation.min_trades,
        window_label=args.window,
        origin=origin,
    )
    lesson = autopsy_mod.lesson_text(report)
    Memory(settings.memory.db).add_lesson(
        None,
        lesson,
        tags=["autopsy", f"family:{report['family']}", f"window:{args.window}"],
    )

    if args.json:
        _print_json(report)
        return 0

    print(f"autopsy ({origin}) window={args.window} bars={len(cut)}")
    for line in autopsy_mod.console_lines(report):
        print(line)
    print(f"  lesson stored in {settings.memory.db}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(autopsy_mod.render_markdown(report), encoding="utf-8")
        print(f"  wrote {out}")
    return 0


def cmd_critic(args) -> int:
    """Judge whether this genome may be deployed against the sealed limits."""
    settings = _load(args)
    from . import critic as critic_mod

    got = _champion_or_family(settings, args)
    if got is None:
        return 1
    genome, origin = got
    cut, res = _window_simulation(settings, genome, args.window)
    verdict = critic_mod.judge(
        res.metrics,
        genome=genome,
        equity=res.equity,
        trades=res.trades,
        settings=settings,
        min_trades=settings.evaluation.min_trades,
    )
    payload = {
        "origin": origin,
        "window": args.window,
        "bars": int(len(cut)),
        "metrics": {
            k: res.metrics.get(k)
            for k in ("total_return", "max_drawdown", "n_trades", "win_rate", "sharpe")
            if k in res.metrics
        },
        **verdict.as_dict(),
    }
    if args.json:
        _print_json(payload)
        return 0 if verdict.approved else 1
    print(f"critic ({origin}) window={args.window} bars={len(cut)}")
    print(critic_mod.render_markdown(verdict))
    if verdict.approved:
        print("\ndeployment is allowed by the sealed risk layer.")
        return 0
    print(
        "\ndeployment refused. `paper --create` will refuse it too; the sealed limits "
        "are not a suggestion.",
        file=sys.stderr,
    )
    return 1


def cmd_stress(args) -> int:
    """Shake the result: resample the trades and shade the parameters."""
    settings = _load(args)
    from . import stress as stress_mod

    got = _champion_or_family(settings, args)
    if got is None:
        return 1
    genome, origin = got
    df = _load_df(settings)
    try:
        deltas = [float(x) for x in str(args.deltas).split(",") if x.strip()]
    except ValueError:
        print(f"stress: --deltas must be numbers, got {args.deltas!r}", file=sys.stderr)
        return 2
    if not deltas:
        deltas = [0.10, 0.25]

    report = stress_mod.stress_report(
        df,
        genome,
        settings,
        window=args.window,
        paths=args.paths,
        seed=args.seed,
        method=args.method,
        block=args.block,
        deltas=deltas,
        include_risk=not args.no_risk,
        origin=origin,
    )
    Memory(settings.memory.db).add_lesson(
        None,
        report["lesson"],
        tags=["stress", f"family:{report['family']}", f"window:{args.window}"],
    )

    if args.json:
        _print_json(report)
        return 0

    print(f"stress ({origin}) window={args.window} bars={report['bars']}")
    for line in stress_mod.console_lines(report):
        print(line)
    print(f"  lesson stored in {settings.memory.db}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(stress_mod.render_markdown(report), encoding="utf-8")
        print(f"  wrote {out}")
    return 0


def cmd_lock_stress(args) -> int:
    """Soak the session lock: writers heartbeating, an operator stopping, the race injected.

    The command is the instrument, not the fix: it runs the real `Session` in a private
    temporary directory (never the operator's `state/session.json`), with the Windows lost
    create race injected, and reports whether a `stop` was overwritten or a write happened
    beside the lock.  `--compare` runs the same soak with **and** without the pre-fix
    reading, so a clean result is only called a pass when the injection demonstrably bites;
    `--legacy` alone runs just the broken reading.  Exit **0** a clean soak (or, for
    `--legacy`, a defect reproduced), **2** a regression, **3** inconclusive.
    """
    from . import lockstress

    shares = (("--race-share", args.race_share), ("--replace-share", args.replace_share))
    for name, value in shares:
        if not 0.0 <= float(value) <= 1.0:
            # A share outside 0..1 would silently switch the injection off and let the soak
            # report a clean run it never earned.
            print(f"lock-stress: {name} must be between 0 and 1 (0.15, not 15)", file=sys.stderr)
            return 2

    options = dict(
        rounds=args.rounds,
        writers=args.writers,
        cycles=args.cycles,
        race_share=args.race_share,
        replace_share=args.replace_share,
        seed=args.seed,
        budget_s=args.budget,
    )
    if args.compare:
        result = lockstress.compare(**options)
        if args.json:
            _print_json(result)
        else:
            for line in lockstress.compare_lines(result):
                print(line)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(lockstress.render_compare_markdown(result), encoding="utf-8")
            print(f"  wrote {out}")
        if result["validated"]:
            return 0
        return 2 if result["verdict"] == "regression" else 3

    report = lockstress.run_soak(legacy=args.legacy, **options)
    if args.json:
        _print_json(report)
    else:
        for line in lockstress.console_lines(report):
            print(line)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(lockstress.render_markdown(report), encoding="utf-8")
        print(f"  wrote {out}")
    if args.legacy:
        return 0 if report["verdict"] == "defect" else 3
    return 0 if report["clean"] else 2


def cmd_risk(args) -> int:
    """Show / verify / seal the read-only risk layer - and measure the account against it.

    ``limits`` answers, from a terminal, the same question the dashboard's *Risk limits*
    panel answers in a browser: it reads the active paper session and reports the room
    left under each sealed cap, through the very functions the pre-trade gate uses.  Its
    exit status is the useful part of a risk read: **0** within the limits, **1** a limit
    that halts trading is breached (the next order arms the stop), **2** nothing to
    measure.  ``|| echo "risk: look at this"`` then does the right thing in a script.
    """
    settings = _load(args)
    from . import notify, risk_rules

    action = args.action or "show"
    if action == "limits":
        from . import risk_report

        report = risk_report.limits_report(settings)
        if getattr(args, "json", False):
            _print_json(report)
        else:
            print("selftrade risk limits (sealed caps vs the active paper session)")
            for line in risk_report.describe(report):
                print(line)
            alerts = report.get("alerts") or {}
            where = "ready" if alerts.get("ready") else str(alerts.get("reason"))
            print(f"  alerts: {where} - `python -m selftrade notify status` says why")
            for notice in notify.outstanding(settings, report):
                band = f" [{notice['band']}]" if notice.get("band") else ""
                print(f"  PENDING [{notice['kind']}]{band} {notice['headline']}")
        if not report["ok"]:
            return 2
        return 1 if report["would_arm"] else 0
    if action == "show":
        print("selftrade sealed risk limits (read-only)")
        print(risk_rules.sealed_block())
        print(f"seal file: {risk_rules.seal_path(settings)}")
        print(
            "this layer is not part of config.yaml: no --set, no genome and no LLM "
            "proposal can reach it"
        )
        return 0
    if action == "seal":
        path = risk_rules.write_seal(settings)
        print(f"sealed digest {risk_rules.digest_of()} -> {path}")
        return 0
    if action == "verify":
        info = risk_rules.verify_seal(settings)
        _print_json(info)
        if not info["ok"]:
            print(f"risk: {info['reason']}", file=sys.stderr)
            return 1
        return 0
    print(f"unknown risk action: {action}", file=sys.stderr)
    return 1


def cmd_notify(args) -> int:
    """The risk alert channel: who would be told, prove it, and watch for it.

    The channel is opt-in (``notify.enabled`` plus two environment variables, read by
    name) and it exists for one reason: the risk layer can stop trading on its own, and
    a stop nobody hears about is not a stop.

    * ``status`` - would I be told, and what is waiting right now (sends nothing);
    * ``test`` - send one message, so the channel is proven before it matters;
    * ``watch`` - the small supervisor for when nobody is at the screen: it re-reads the
      switch and the limits every ``notify.watch_interval_s`` and sends on transitions
      only (each halt once, each warning once per band); ``--once`` does one round;
    * ``reset`` - forget what was announced, so the next halt is news again.

    Exit status: ``watch --once`` and ``test`` return 1 when a message did not go out.
    """
    settings = _load(args)
    from .notify import RiskWatch, readiness, send, sent_log

    action = args.action or "status"
    info = readiness(settings)
    watch = RiskWatch(settings)

    if action == "status":
        snapshot = watch.current()
        pending = watch.pending(snapshot)
        if args.json:
            _print_json(
                {
                    **info,
                    "switch": snapshot["switch"],
                    "pending": pending,
                    "sent": sent_log(settings),
                }
            )
            return 0
        state = "ready" if info["ready"] else f"NOT ready: {info['reason']}"
        print(f"risk alerts: {state} (channel: {info['channel'] or 'none'})")
        print(f"  token env {info['token_env']} | chat env {info['chat_env']} | {info['state_file']}")
        print(
            f"  warns when a halting limit has <= {info['warn_below_frac']*100:.0f}% of its cap "
            f"left, and always when one is already past its cap; "
            f"`notify watch` every {settings.notify.watch_interval_s:g}s"
        )
        switch = snapshot["switch"]
        print(
            f"  kill switch: {'ENGAGED' if switch.get('blocked') else 'clear'}"
            f"{' - ' + switch['reason'] if switch.get('reason') else ''}"
        )
        if not pending:
            # Two different silences: an engaged stop that has already been announced is
            # not the same state as a quiet account, and saying "no halt is engaged" over
            # a blocked switch would be a lie.
            if switch.get("blocked"):
                print(
                    "  nothing waiting to be sent: this halt has already been announced "
                    "(see the last SENT line below)"
                )
            else:
                print(
                    "  nothing to send right now: no limit that can halt is close to - "
                    "or past - its cap"
                )
        for notice in pending:
            first = (notice["text"].splitlines() or [""])[0]
            print(f"  PENDING [{notice['kind']}] {first}")
            print(f"          would be {'sent' if info['ready'] else 'dropped'}: {info['hint']}")
        # What actually went out.  Only delivered notices are recorded, so this is a
        # record of messages, not of decisions - the last few are enough in a terminal.
        for entry in sent_log(settings, limit=3):
            band = f" [{entry['band']}]" if entry.get("band") else ""
            who = "" if entry.get("automatic") is None else (
                " (automatic)" if entry["automatic"] else " (by you)"
            )
            print(f"  SENT {entry.get('at')} [{entry.get('kind')}]{band}{who} {entry.get('reason')}")
        return 0

    if action == "test":
        ok, message = send(
            settings,
            "selftrade test message - the alert channel works. It will carry the halt "
            "(kill switch engaged) and the warning before it (a halting limit close to "
            "its cap, or already past it with nothing stopped yet). "
            "`python -m selftrade notify status` shows what is pending.",
        )
        print(f"notify test: {'sent' if ok else 'NOT sent'} ({message})")
        return 0 if ok else 1

    if action == "reset":
        watch.clear()
        print(f"notify: forgot what was announced ({info['state_file']})")
        return 0

    if action == "watch":
        interval = float(args.interval or settings.notify.watch_interval_s)
        if not info["ready"]:
            print(f"notify watch: nothing would be sent - {info['reason']}", file=sys.stderr)
            print(
                "  set notify.enabled=true and export both variables, then `notify test`",
                file=sys.stderr,
            )
            return 1
        print(
            f"watching risk every {interval:g}s (halts once each, warnings below "
            f"{info['warn_below_frac']*100:.0f}% of a cap and always past one); "
            "Ctrl+C stops"
        )
        while True:
            result = watch.watch_once()
            outcomes = result["outcomes"]
            for outcome in outcomes:
                where = "sent" if outcome["sent"] else "NOT sent"
                print(
                    f"  [{outcome['kind']}] {where}: {outcome['message']} "
                    f"({outcome['reason']})",
                    flush=True,
                )
            if args.once:
                return 1 if any(not o["sent"] for o in outcomes) else 0
            try:
                time.sleep(max(1.0, interval))
            except KeyboardInterrupt:
                print("\nnotify watch: stopped")
                return 0
    print(f"unknown notify action: {action}", file=sys.stderr)
    return 1


def cmd_broker(args) -> int:
    """Drive the simulated broker through the risk gate: orders, fills, stops, ledger.

    This is the acceptance walk from the brief in one command: an order that breaks a
    sealed limit is refused and recorded, a compliant order fills at the next open,
    the hard stop exits the position, and the fill ledger reconciles with the
    broker's own books.  No network, no credentials, no real money.
    """
    settings = _load(args)
    from . import risk_rules
    from .execution import (
        KillSwitch,
        OrderRequest,
        PaperBroker,
        RiskGate,
        Side,
        kill_switch_path,
    )

    df = _load_df(settings).tail(int(args.bars))
    COST_ALL = (settings.backtest.fee_bps + settings.backtest.slippage_bps) / 10_000.0
    mem = Memory(settings.memory.db)
    switch = KillSwitch(kill_switch_path(settings))
    gate = RiskGate.from_config(settings.risk_gate) if settings.risk_gate.enabled else None
    broker = PaperBroker(
        df,
        symbol=args.symbol or settings.data.symbol,
        cash=float(args.cash),
        fee_bps=settings.backtest.fee_bps,
        slippage_bps=settings.backtest.slippage_bps,
        gate=gate,
        kill_switch=switch,
        memory=mem,
    )
    symbol = broker.symbol
    story: list[dict[str, Any]] = []
    events_before = len(mem.risk_events(limit=10_000))

    def record(step: str, **kw: Any) -> None:
        story.append({"step": step, **kw})

    # Where to enter.  `auto` (the default) walks the window to the first bar whose
    # next 60 bars actually test the 2% stop, so the demonstration exercises the
    # mechanism on real prices instead of hoping; the choice is printed, never hidden.
    stop_pct = -0.02
    entry_bar = 0
    if args.entry == "auto" and len(df) > 80:
        paid = df["open"].shift(-1) * (1.0 + COST_ALL)
        stop_level = paid * (1.0 + stop_pct)
        lows = df["low"].shift(-1)
        for i in range(0, len(df) - 61):
            if float(lows.iloc[i : i + 60].min()) <= float(stop_level.iloc[i]):
                entry_bar = i
                break
        broker.advance(entry_bar)
    price = float(df["close"].iloc[broker.index])
    record("entry_point", bar=entry_bar, how=args.entry, price=price)

    # 1. an order far past the sealed per-position cap must be refused
    too_big = OrderRequest(
        client_id="demo-oversized",
        symbol=symbol,
        side=Side.BUY,
        qty=(0.5 * broker.cash) / price,
        stop_loss_pct=stop_pct,
    )
    blocked = broker.submit_order(too_big)
    record(
        "oversized_order",
        status=blocked.status.value,
        reason=blocked.reject_reason,
        limit=risk_rules.LIMITS.MAX_POSITION_PCT,
    )
    # the same client_id retried returns the same refusal instead of doubling down
    retry = broker.submit_order(too_big)
    record("retry_same_client_id", same_order=retry.id == blocked.id, status=retry.status.value)

    # 2. a compliant order: 4.5% of equity, with a 2% hard stop
    qty = (0.045 * broker.cash) / price
    good = broker.submit_order(
        OrderRequest(
            client_id="demo-entry",
            symbol=symbol,
            side=Side.BUY,
            qty=qty,
            stop_loss_pct=stop_pct,
            note="demo entry",
        )
    )
    record("compliant_order", status=good.status.value, qty=qty)
    broker.advance(1)  # fills at the next open
    filled = broker.get_order_status(good.id)
    record(
        "fill",
        status=filled.status.value if filled else "?",
        avg_price=filled.avg_price if filled else None,
        bar=broker.index,
    )

    # 3. let the market walk into the hard stop
    stop_fill = None
    while broker.index < len(df) - 1 and broker.get_positions():
        before = len(broker.get_trade_history())
        broker.advance(1)
        for f in broker.get_trade_history()[before:]:
            if f["reason"] == "stop":
                stop_fill = f
        if stop_fill:
            break
    record(
        "hard_stop",
        triggered=stop_fill is not None,
        price=stop_fill["price"] if stop_fill else None,
        bar=broker.index,
        bars_available=len(df),
    )

    account = broker.get_account_balance()
    mismatches = broker.reconcile()
    events = mem.risk_events(limit=50)
    logged_now = len(mem.risk_events(limit=10_000)) - events_before
    payload = {
        "broker": broker.describe(),
        "sealed_limits": risk_rules.LIMITS.as_dict(),
        "story": story,
        "account": account.as_dict(),
        "equity_points": len(broker.equity_series()),
        "reconcile": [m.as_dict() for m in mismatches],
        "reconcile_ok": not mismatches,
        "risk_events": len(events),
        "risk_events_logged_now": logged_now,
        "kill_switch": switch.describe(),
        "trades": broker.get_trade_history(limit=10),
    }
    if args.json:
        _print_json(payload)
        return 0 if not mismatches else 1

    print(f"paper broker demo on {symbol}, bars={len(df)}")
    print(f"cash {float(args.cash):,.2f} | fee {settings.backtest.fee_bps}bps "
          f"+ slippage {settings.backtest.slippage_bps}bps per side | mode {broker.mode}")
    print(
        f"gate: {'armed' if gate else 'DISABLED by risk_gate.enabled'} "
        f"(max_positions={settings.risk_gate.max_positions}, "
        f"leverage<={settings.risk_gate.max_leverage}, "
        f"risk/trade<={settings.risk_gate.risk_pct * 100:.3f}%) | sealed limits "
        f"still apply: {risk_rules.LIMITS.MAX_POSITION_PCT:.1f}% / "
        f"{risk_rules.LIMITS.MAX_DRAWDOWN_PCT:.1f}% / "
        f"{risk_rules.LIMITS.MAX_DAILY_LOSS_PCT:.1f}%"
    )
    for row in story:
        label = row.pop("step", "")
        bits = "  ".join(f"{k}={v}" for k, v in row.items())
        print(f"  {label:<18} {bits}")
        row["step"] = label
    print(
        f"account: equity {account.equity:,.2f} cash {account.cash:,.2f} "
        f"realized {account.realized_pnl:,.2f} fees {account.fees_paid:,.2f} "
        f"positions {account.open_positions} drawdown {account.drawdown * 100:.2f}%"
    )
    print(f"ledger reconciliation: {'OK' if not mismatches else 'MISMATCH'}")
    for m in mismatches:
        print(f"  ! {m.what}: internal={m.internal} ledger={m.ledger} ({m.detail})")
    print(f"risk events: {logged_now} logged by this demo, {len(events)} in the database")
    for e in events[:2]:
        print(f"  [{e['kind']}] {e['reason'][:110]}")
    print(f"kill switch: {'ENGAGED' if switch.is_blocked() else 'clear'}")
    return 0 if not mismatches else 1


def cmd_switch(args) -> int:
    """Show, arm or reset the kill switch (the one control that stops new orders)."""
    settings = _load(args)
    from .execution import KillSwitch, kill_switch_path

    switch = KillSwitch(kill_switch_path(settings))
    action = args.action or "show"
    if action == "show":
        info = switch.describe()
        _print_json(info)
        print(
            "blocked: no new orders will be accepted"
            if info["blocked"]
            else "clear: new orders are allowed",
            file=sys.stderr,
        )
        return 0 if not info["blocked"] else 1
    if action == "arm":
        state = switch.trigger(args.reason or "armed by the operator", by="operator")
        _print_json({k: state[k] for k in ("blocked", "reason", "triggered_by", "triggered_at")})
        return 0
    if action == "reset":
        state = switch.reset(args.reason or "", by="operator")
        _print_json({k: state[k] for k in ("blocked", "reason", "triggered_by")})
        return 0
    print(f"unknown switch action: {action}", file=sys.stderr)
    return 1


def cmd_binance(args) -> int:
    """Binance: dry run by default, two keys to send an order.

    The first key is ``binance.enabled: true``, the second is ``binance.live: true``
    *plus* ``--live`` here.  Without both, orders are built, signed and recorded but
    never sent, and every API answer and error lands in the ``api_calls`` table.

    The host is the **Spot Testnet** unless ``binance.allow_production: true`` names
    a funded account, and even then every order that is about to be sent passes the
    sealed risk gate and the kill switch first.
    """
    settings = _load(args)
    from . import binance as bn_mod
    from .execution import KillSwitch, RiskGate, kill_switch_path

    cfg = settings.binance
    action = args.action
    mem = Memory(settings.memory.db)

    if action == "calls":
        rows = mem.api_calls(limit=int(args.limit), provider="binance-testnet")
        if args.json:
            _print_json(rows)
        else:
            print(f"{len(rows)} recorded Binance call(s), newest first")
            for row in rows:
                tag = "dry-run" if row["dry_run"] else ("ok" if row["ok"] else "ERROR")
                print(
                    f"  [{tag}] {row['method']} {row['path']} "
                    f"status={row['status']} code={row['code'] or '-'} "
                    f"{row['msg'][:80]}"
                )
        return 0

    if action == "check":
        # Read-only by construction, and answered *before* the arming keys because it
        # has to answer even when the rail is switched off - that is exactly the case
        # worth showing.  ``key_health`` is the same report the dashboard panel renders.
        if args.live:
            print("binance check is read-only: --live is ignored, nothing is sent", file=sys.stderr)
        report = bn_mod.key_health(settings, memory=mem)
        if args.json:
            _print_json(report)
        else:
            _print_account_check(report)
        return 0 if report.get("ok") else 1

    if not cfg.enabled:
        reason = "disabled in config: binance.enabled=false, so nothing was sent"
        info = {
            "provider": "binance-testnet",
            "enabled": False,
            "base_url": cfg.base_url,
            "host_is_testnet": "testnet" in cfg.base_url,
            "reason": reason,
        }
        if args.json:
            _print_json(info)
        else:
            print(f"binance: {info['reason']}")
        return 0 if action == "doctor" else 1

    # ``check`` never reaches this point: it returns above, unarmed.
    armed = bool(args.live)
    if args.live and not cfg.live:
        extra = " (allow_production is on: this would be REAL money)" if cfg.allow_production else ""
        print(
            "binance: refusing --live: set `binance.live: true` in config.yaml too "
            f"(two keys, like the MT5 rail){extra}",
            file=sys.stderr,
        )
        return 1

    if action in ("protect", "unprotect") and not armed:
        # Unlike ``order``, where an unarmed run is a documented dry run, touching the
        # protection of a live position has no meaningful rehearsal: the stop either
        # exists at the exchange or it does not.
        print(
            "binance: refusing to change the exchange protection without both keys: set "
            "`binance.live: true` in config.yaml and pass --live",
            file=sys.stderr,
        )
        return 1

    symbols = list(cfg.allowed_symbols) or [cfg.symbol]
    # The sealed gate and the kill switch ride with the adapter: an armed order
    # cannot be sent without passing them (and a production host is opt-in).
    gate = RiskGate.from_config(settings.risk_gate) if settings.risk_gate.enabled else None
    switch = KillSwitch(kill_switch_path(settings))
    try:
        broker = bn_mod.BinanceTestnetBroker(
            config=cfg, symbols=symbols, memory=mem, live=armed,
            risk_gate=gate, kill_switch=switch,
        )
    except bn_mod.HostRefusedError as exc:
        print(f"binance: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # BrokerError: missing credentials, not enabled, ...
        print(f"binance: {exc}", file=sys.stderr)
        return 1

    if action == "doctor":
        info = broker.describe()
        try:
            info["server_time_ms"] = broker.sync_time()
            info["clock_offset_ms"] = round(broker._time_offset_ms, 1)
        except Exception as exc:  # a doctor must report, not crash
            info["server_time_error"] = str(exc)
        if args.json:
            _print_json(info)
        else:
            print("binance spot (testnet by default)")
            for key in (
                "base_url", "enabled", "armed", "mode", "host_is_testnet", "production",
                "allow_production", "risk_gate", "kill_switch", "symbols",
                "interval", "weight_used", "max_weight_per_min", "credentials",
            ):
                print(f"  {key:<18} {info.get(key)}")
            print(f"  {'mode_note':<18} {info['mode_note']}")
            if "server_time_ms" in info:
                print(f"  ephemeral clock offset {info['clock_offset_ms']} ms")
            elif "server_time_error" in info:
                print(f"  server time unavailable: {info['server_time_error']}")
        return 0

    if action == "exchange":
        info = broker.exchange_info(refresh=True)
        payload = {
            "base_url": broker.base_url,
            "symbols": [broker.rules_for(s).as_dict() for s in symbols],
            "rateLimits": info.get("rateLimits", []),
        }
        _print_json(payload)
        return 0

    if action == "bars":
        df = broker.get_market_data(symbols[0], limit=min(int(args.limit), cfg.kline_bars))
        payload = {
            "symbol": symbols[0],
            "interval": broker.interval,
            "rows": int(len(df)),
            "first": str(df.index[0]) if len(df) else None,
            "last": str(df.index[-1]) if len(df) else None,
            "last_close": float(df["close"].iloc[-1]) if len(df) else None,
            "note": "the testnet does not document a history depth: use production "
            "public data for research",
        }
        _print_json(payload)
        return 0

    if action == "account":
        snap = broker.get_account_balance()
        _print_json({"account": snap.as_dict(), "balances": broker.balances()})
        return 0

    if action == "positions":
        positions = broker.get_positions()
        payload = {
            "positions": [p.as_dict(price=broker.last_price(p.symbol)) for p in positions],
            "note": "Spot has balances, not positions: only configured symbols are shown",
        }
        _print_json(payload)
        return 0

    if action == "history":
        _print_json(broker.get_trade_history(limit=int(args.limit)))
        return 0

    if action == "open":
        _print_json([o.as_dict() for o in broker.open_orders()])
        return 0

    if action == "order":
        if not args.qty:
            print("binance order: --qty is required", file=sys.stderr)
            return 1
        from .execution import OrderRequest, OrderStatus, OrderType, Side

        request = OrderRequest(
            client_id=args.client_id or f"selftd-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            symbol=args.symbol or symbols[0],
            side=Side(args.side),
            qty=float(args.qty),
            type=OrderType.LIMIT if args.type == "limit" else OrderType.MARKET,
            limit_price=args.price,
            stop_loss_pct=args.stop_pct,
            note=args.note or "",
        )
        order = broker.submit_order(request)
        # Only a *filled* entry can be protected: a rejected or dry-run one has no
        # position, and asking the exchange about it would be a signed call for nothing.
        stop = None
        if order.status is OrderStatus.FILLED:
            stop = broker.protection_of(order.client_id)
        payload = {
            "mode": broker.mode,
            "sent": order.status is not OrderStatus.DRY_RUN,
            "order": order.as_dict(),
            "protection": None if stop is None else stop.as_dict(),
            "calls": broker.recorded_calls(limit=4),
        }
        if args.json:
            _print_json(payload)
        else:
            print(f"binance {broker.mode} order {order.id} -> {order.status.value}")
            if order.status is OrderStatus.DRY_RUN:
                print(
                    "  DRY RUN: the request was signed and recorded, not sent. "
                    "Arm it with `binance.live: true` + --live."
                )
            if order.note:
                print(f"  {order.note}")
            if order.reject_reason:
                print(f"  refused: {order.reject_reason}", file=sys.stderr)
        if order.status is OrderStatus.REJECTED:
            return 1
        return 0

    if action == "protect":
        # The way back from an UNPROTECTED note: place the stop for a position that is
        # already open, using the entry's own average fill price.
        if not args.client_id:
            print("binance protect: --client-id is required (the entry to protect)", file=sys.stderr)
            return 1
        if args.stop_pct is None:
            print("binance protect: --stop-pct is required, e.g. --stop-pct -0.02", file=sys.stderr)
            return 1
        try:
            stop = broker.protect(args.client_id, float(args.stop_pct), symbol=args.symbol)
        except bn_mod.BrokerError as exc:
            print(f"binance protect: {exc}", file=sys.stderr)
            return 1
        payload = {"entry": args.client_id, "stop": stop.as_dict()}
        if args.json:
            _print_json(payload)
        else:
            print(
                f"binance protect: {stop.symbol} SELL {stop.type.value} {stop.qty} @ "
                f"{stop.limit_price} -> {stop.status.value}"
            )
            print(f"  guarding the entry {args.client_id} (client {stop.client_id}, id {stop.id})")
        return 0

    if action == "unprotect":
        if not args.client_id:
            print("binance unprotect: --client-id is required (the entry to uncover)", file=sys.stderr)
            return 1
        try:
            stop = broker.cancel_protection(args.client_id, symbol=args.symbol)
        except bn_mod.BrokerError as exc:
            print(f"binance unprotect: {exc}", file=sys.stderr)
            return 1
        if stop is None:
            print(
                f"binance unprotect: no protective stop is known for {args.client_id}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            _print_json({"entry": args.client_id, "stop": stop.as_dict()})
        else:
            print(
                f"binance unprotect: {stop.symbol} {stop.status.value} "
                f"(client {stop.client_id}, id {stop.id})"
            )
        return 0

    if action == "cancel":
        if not args.order_id:
            print("binance cancel: --order-id is required", file=sys.stderr)
            return 1
        order = broker.cancel_order(str(args.order_id))
        _print_json({"mode": broker.mode, "order": order.as_dict()})
        return 0

    print(f"unknown binance action: {action}", file=sys.stderr)
    return 1


def cmd_paper(args) -> int:
    settings = _load(args)
    from . import paper as paper_mod

    return paper_mod.cli(settings, args)


def cmd_capital(args) -> int:
    """The simulated capital account: what the agents trade, and who may credit it.

    A ledger command like ``switch``: it shows the balance, and it writes only when the
    action names an amount and a reason.  It never touches an exchange - the credit is
    recorded locally and traded on paper (``docs/CAPITAL.md``).
    """
    settings = _load(args)
    from . import capital as capital_mod

    return capital_mod.cli(settings, args)


def cmd_password(args) -> int:
    """Read, set or forget the dashboard login password without a dashboard.

    The escape hatch, and an honest one: a shell on this machine can already rewrite the
    state directory, so asking for permission here would be theatre.  What it will *not* do
    is guess - it prompts twice, checks the same length rule as the page, and writes only a
    salted hash (``docs/DASHBOARD.md``).
    """
    import getpass

    from .credentials import (
        DEFAULT_PASSWORD,
        CredentialError,
        CredentialStore,
        check_new_password,
    )

    settings = _load(args)
    store = CredentialStore(settings.dashboard.password_file)
    action = (getattr(args, "action", None) or "status").lower()
    env = os.environ.get(settings.dashboard.password_env)

    if action == "status":
        state = store.describe()
        env_wins = bool(env)
        out = {
            "password": state,
            "environment_variable": settings.dashboard.password_env,
            "environment_set": env_wins,
            "effective_source": "environment" if env_wins else state["source"],
            "dashboard_ready": True,
        }
        if getattr(args, "json", False):
            print(json.dumps(out, indent=2, default=str))
            return 0
        print(f"stored password: {state['source']}  ({state['file']})")
        if state["source"] == "stored":
            print(f"  changed {state['changed_at']} by {state['changed_by']} "
                  f"({state['algorithm']}, {state['iterations']} rounds)")
        if env_wins:
            print(f"  but {settings.dashboard.password_env} is set: it wins, and the dashboard "
                  f"refuses to change the stored one while it is")
        elif state["is_default"]:
            print(f"  the dashboard still accepts the bootstrap password ({DEFAULT_PASSWORD}): "
                  f"change it here or under Settings")
        if state["source"] == "corrupt":
            print(f"  {state['note']}")
            return 1
        return 0

    if action == "reset":
        if not getattr(args, "confirm", False):
            print("refusing: `password --reset` forgets the stored password and goes back to "
                  f"the bootstrap one ({DEFAULT_PASSWORD}); add --confirm if that is what you mean",
                  file=sys.stderr)
            return 1
        removed = store.reset()
        print((f"removed {store.path}" if removed else f"nothing was stored in {store.path}")
              + f" - the dashboard now accepts the bootstrap password ({DEFAULT_PASSWORD})")
        if env:
            print(f"note: {settings.dashboard.password_env} is set and still wins over it")
        return 0

    value = getattr(args, "value", None)
    if not value:
        try:
            value = getpass.getpass("new dashboard password: ")
            again = getpass.getpass("repeat it: ")
        except (EOFError, KeyboardInterrupt):
            print("\nno password was read: nothing changed", file=sys.stderr)
            return 1
        if value != again:
            print("the two entries differ: nothing changed", file=sys.stderr)
            return 1
    refusal = check_new_password(value, min_length=settings.dashboard.password_min_length)
    if refusal:
        print(f"refusing: {refusal}", file=sys.stderr)
        return 1
    try:
        store.set(value, by="cli", min_length=settings.dashboard.password_min_length)
    except CredentialError as exc:
        print(f"password: {exc}", file=sys.stderr)
        return 1
    print(f"password written to {store.path} as {store.describe()['algorithm']} "
          f"({store.describe()['iterations']} rounds, salted) - the password itself is not stored")
    if env:
        print(f"warning: {settings.dashboard.password_env} is set, so it still decides the login; "
              f"unset it for the stored password to take effect")
    return 0


def cmd_serve(args) -> int:
    settings = _load(args)
    from .dashboard import serve

    host = args.host or settings.dashboard.host
    port = args.port or settings.dashboard.port
    serve(settings, host=host, port=port)
    return 0


def cmd_session(args) -> int:
    """Start and steer the tracked loop - the same runner the dashboard's buttons use.

    ``start`` really launches ``evolve --session`` (detached, logged to
    ``state/loop.log``) instead of writing a request no one reads; ``pause``, ``resume``
    and ``stop`` are refused when no loop is running, because a request in a file with
    nothing on the other end is not a control (see :mod:`selftrade.loop`).
    """
    settings = _load(args)
    from .loop import LoopRunner
    from .session import Session

    runner = LoopRunner(settings, config_path=getattr(args, "config", None))
    action = args.action
    if action == "status":
        state = Session(settings.session_file).status()
        state["loop"] = runner.status()
        print(json.dumps(state, indent=2, default=str))
        loop = state["loop"]
        print(
            f"loop: {'running pid ' + str(loop['pid']) if loop['running'] else 'not running'}"
            f" | log {loop['log']}",
            file=sys.stderr,
        )
        return 0
    if action == "start":
        code, out = runner.start(args.generations or settings.evolution.generations)
        if code != 200:
            print(f"could not start the loop: {out.get('error')}", file=sys.stderr)
            return 1
        loop = out["loop"]
        print(
            f"loop started: pid {loop['pid']} | {loop['generations_total']} generations "
            f"| logging to {loop['log']}"
        )
        print(f"  {out['command']}")
        print("  `session pause|resume|stop` steer it, `session status` reads it")
        return 0
    code, out = runner.request(action)
    if code != 200:
        print(out.get("error") or f"{action} refused", file=sys.stderr)
        return 1
    print(out.get("note") or f"{action} requested")
    return 0


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

    for name, func, extra in (
        (
            "autopsy",
            cmd_autopsy,
            "post-mortem of losing trades: worst losses and clustered failure modes",
        ),
        ("critic", cmd_critic, "judge deployment against the sealed risk limits"),
    ):
        sp = sub.add_parser(name, help=extra)
        sp.add_argument("--champion", action="store_true")
        sp.add_argument("--family")
        sp.add_argument("--window", choices=["holdout", "full", "recent"], default="holdout")
        sp.add_argument("--json", action="store_true")
        if name == "autopsy":
            sp.add_argument("--worst", type=int, default=5)
            sp.add_argument("--out")
        sp.set_defaults(func=func)

    sp = sub.add_parser(
        "stress",
        help="Monte Carlo over the trade sequence + parameter sensitivity",
    )
    sp.add_argument("--champion", action="store_true")
    sp.add_argument("--family")
    sp.add_argument("--window", choices=["holdout", "full", "recent"], default="holdout")
    sp.add_argument("--paths", type=int, default=2000)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--method", choices=["bootstrap", "shuffle", "block"], default="bootstrap")
    sp.add_argument("--block", type=int, default=5)
    sp.add_argument(
        "--deltas",
        default="0.10,0.25",
        help="comma-separated relative perturbations of the parameters",
    )
    sp.add_argument("--no-risk", action="store_true", help="skip the risk-knob perturbations")
    sp.add_argument("--out")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stress)

    sp = sub.add_parser(
        "lock-stress",
        help="soak the session lock: writers, an operator stop, and the Windows race injected",
    )
    sp.add_argument("--rounds", type=int, default=15, help="stop-checks are rounds x cycles")
    sp.add_argument("--writers", type=int, default=3, help="concurrent heartbeat writers")
    sp.add_argument("--cycles", type=int, default=3, help="stop/confirm cycles per round")
    sp.add_argument(
        "--race-share",
        type=float,
        default=0.15,
        help="share of lock creates that lose the create race (only while another writer "
        "really holds the lock), as Windows reports it",
    )
    sp.add_argument(
        "--replace-share",
        type=float,
        default=0.05,
        help="share of renames that hit a reader holding the file open",
    )
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--budget", type=float, default=30.0, help="seconds after which rounds stop")
    sp.add_argument(
        "--legacy",
        action="store_true",
        help="run the pre-fix lock reading (the soak is expected to fail: it proves the "
        "detector bites)",
    )
    sp.add_argument(
        "--compare",
        action="store_true",
        help="run both readings and pass only when the shipped one is clean and the pre-fix "
        "one is not",
    )
    sp.add_argument("--out")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_lock_stress)

    sp = sub.add_parser("risk", help="sealed, read-only risk limits")
    sp.add_argument(
        "action", nargs="?", choices=["show", "verify", "seal", "limits"], default="show"
    )
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_risk)

    sp = sub.add_parser("notify", help="risk alerts: the halt, and the warning before it")
    sp.add_argument(
        "action", nargs="?", choices=["status", "test", "watch", "reset"], default="status"
    )
    sp.add_argument("--interval", type=float, help="seconds between `watch` rounds")
    sp.add_argument("--once", action="store_true", help="`watch`: one round, then exit")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_notify)

    sp = sub.add_parser("broker", help="simulated broker: orders, fills, stops, ledger")
    sp.add_argument("--symbol")
    sp.add_argument("--bars", type=int, default=400)
    sp.add_argument("--entry", choices=["auto", "first"], default="auto")
    sp.add_argument("--cash", type=float, default=10_000.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_broker)

    sp = sub.add_parser(
        "binance",
        help="Binance Spot Testnet adapter (dry run by default); `check` probes a key read-only",
    )
    sp.add_argument(
        "action",
        choices=["check", "doctor", "account", "positions", "bars", "exchange", "history",
                 "open", "order", "protect", "unprotect", "cancel", "calls"],
    )
    sp.add_argument("--symbol")
    sp.add_argument("--side", choices=["buy", "sell"], default="buy")
    sp.add_argument("--qty", type=float)
    sp.add_argument("--type", choices=["market", "limit"], default="market")
    sp.add_argument("--price", type=float)
    sp.add_argument("--client-id", dest="client_id")
    sp.add_argument("--order-id", dest="order_id")
    sp.add_argument("--note", default="")
    sp.add_argument(
        "--stop-pct",
        dest="stop_pct",
        type=float,
        help="protective stop as a fraction of the entry (negative, e.g. -0.02): the rail "
        "places a resting SELL STOP_LOSS_LIMIT as soon as the entry fills",
    )
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--live", action="store_true", help="send it (binance.live must also be true)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_binance)

    sp = sub.add_parser("switch", help="kill switch: stop new orders right now")
    sp.add_argument("action", nargs="?", choices=["show", "arm", "reset"], default="show")
    sp.add_argument("--reason")
    sp.set_defaults(func=cmd_switch)

    sp = sub.add_parser("paper", help="forward paper trading")
    sp.add_argument("--create", action="store_true")
    sp.add_argument("--run", action="store_true")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--reset", action="store_true")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--session", type=int, dest="session_id")
    sp.add_argument("--champion", action="store_true")
    sp.add_argument(
        "--fund",
        type=float,
        help="allocate this much from the capital account into the new/active session "
        "(refused, with nothing created, when the free balance is short)",
    )
    sp.add_argument(
        "--release",
        action="store_true",
        help="return the session's allocated capital to the free balance (unfunded "
        "sessions only: a session that traded keeps its capital until an explicit reset)",
    )
    sp.add_argument("--reason", default="", help="why the capital moved (kept in the ledger)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_paper)

    sp = sub.add_parser(
        "capital", help="simulated capital account: credit money the agents can work"
    )
    sp.add_argument(
        "action",
        nargs="?",
        choices=["status", "deposit", "withdraw", "allocate", "release", "ledger", "policy"],
        default="status",
    )
    sp.add_argument("amount", nargs="?", type=float)
    sp.add_argument("--reason", default="")
    sp.add_argument("--currency", help="currency of this credit (default: funding.currency)")
    sp.add_argument("--session", type=int, dest="session_id")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_capital)

    sp = sub.add_parser(
        "password",
        help="the dashboard login password (a salted hash; the escape hatch for a forgotten one)",
    )
    sp.add_argument("action", nargs="?", choices=["status", "set", "reset"], default="status")
    sp.add_argument(
        "--value",
        help="the new password; omit it to be prompted (the safer path: an argument is kept "
        "in the shell history and in this machine's process list)",
    )
    sp.add_argument(
        "--confirm",
        action="store_true",
        help="reset: fall back to the bootstrap password (``0000``); required, it changes "
        "how anyone signs in",
    )
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_password)

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
