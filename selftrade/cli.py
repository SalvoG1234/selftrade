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
        h = holdout_eval(guard.read(), genome, settings)
        # keep the JSON interface stable: holdout_eval also carries the equity
        # series and the trade frame (the Evolver needs them), which do not belong
        # in a printed payload.
        out["holdout"] = {"metrics": h["metrics"], "n_trades": h["n_trades"]}
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


def cmd_risk(args) -> int:
    """Show / verify / seal the read-only risk layer."""
    settings = _load(args)
    from . import risk_rules

    action = args.action or "show"
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

    sp = sub.add_parser("risk", help="sealed, read-only risk limits")
    sp.add_argument("action", nargs="?", choices=["show", "verify", "seal"], default="show")
    sp.set_defaults(func=cmd_risk)

    sp = sub.add_parser("broker", help="simulated broker: orders, fills, stops, ledger")
    sp.add_argument("--symbol")
    sp.add_argument("--bars", type=int, default=400)
    sp.add_argument("--entry", choices=["auto", "first"], default="auto")
    sp.add_argument("--cash", type=float, default=10_000.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_broker)

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
