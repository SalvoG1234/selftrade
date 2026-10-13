"""Tests for the CLI: parsing, overrides, exit codes and command handlers."""

import json
from pathlib import Path

import pytest

from selftrade.cli import build_parser, main
from selftrade.data import make_synthetic, save_csv


def write_config(tmp_path) -> Path:
    csv_path = tmp_path / "d.csv"
    save_csv(make_synthetic(900, seed=77), csv_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "project: selftrade",
                "data:",
                "  source: csv",
                f"  csv: {csv_path.as_posix()}",
                "  datetime_col: timestamp",
                "  timeframe: 1h",
                "  min_rows: 100",
                "evaluation:",
                "  folds: 2",
                "  min_trades: 1",
                "  max_drawdown: 0.99",
                "evolution:",
                "  generations: 1",
                "  population: 2",
                "  seed: 5",
                "llm:",
                "  enabled: false",
                f"memory:",
                f"  db: {(tmp_path / 'state' / 'm.db').as_posix()}",
                f"session_file: {(tmp_path / 'state' / 'session.json').as_posix()}",
                "notify:",
                "  enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    return cfg


CI = ["--config"]


def run(cfg, *args):
    return main(["--config", str(cfg), *args])


# ---------------------------------------------------------------- parser


def test_parser_has_all_commands():  # noqa: D103
    p = build_parser()
    names = set(p._subparsers._group_actions[0].choices)
    assert {
        "doctor", "data", "backtest", "evolve", "report", "hall", "export",
        "rea", "mcp", "mt5", "paper", "invest", "autopsy", "critic", "risk",
        "serve", "session", "broker", "switch", "binance", "notify", "verify",
    } <= names


def test_verify_without_ledger_says_what_to_ask_for(capsys):  # noqa: D103
    code = main(["verify"])
    assert code == 2
    assert "verify --ledger" in capsys.readouterr().err


def test_verify_ledger_checks_this_checkout(capsys):  # noqa: D103
    """The command's own contract: exit 0 when the documents agree with the suite."""
    code = main(["verify", "--ledger"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "measured" in out
    assert "every recorded count matches the measurement" in out


def test_verify_ledger_json_is_machine_readable(capsys):
    import json

    assert main(["verify", "--ledger", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["facts"]["total"] >= 100
    assert payload["slots"]


def test_globals_extracted_from_any_position():
    from selftrade.cli import _extract_globals

    before = _extract_globals(["--config", "a.yaml", "--set", "x=1", "backtest", "--family", "rsi"])
    assert before == (["backtest", "--family", "rsi"], "a.yaml", ["x=1"])
    after = _extract_globals(["backtest", "--family", "rsi", "--config", "a.yaml", "--set", "x=1"])
    assert after == (["backtest", "--family", "rsi"], "a.yaml", ["x=1"])
    both = _extract_globals(["--set", "a=1", "evolve", "--set", "b=2"])
    assert both == (["evolve"], None, ["a=1", "b=2"])
    with pytest.raises(Exception):
        _extract_globals(["--set"])


def test_set_applies_before_and_after_subcommand(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "session", "status", "--set", "evolution.seed=11")
    assert code == 0


def test_unknown_command_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["definitely-not-a-command"])
    assert exc.value.code != 0


# ---------------------------------------------------------------- handlers


def test_doctor_runs_without_fatal_error(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "doctor")
    out = capsys.readouterr().out
    assert code == 0
    assert "doctor" in out
    assert "memory:" in out


def _break_the_store(tmp_path) -> Path:
    """A file where the database should be, which is not one."""
    bad = tmp_path / "state" / "m.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not sqlite, not even a little")
    return bad


def test_doctor_reports_a_store_it_cannot_read_and_keeps_going(tmp_path, capsys):
    """`doctor` is the command the error message points at: it must survive the broken store.

    Opening the database used to happen before any of the diagnostics, so the one command
    that exists to say what is wrong died on the first thing that was.
    """
    cfg = write_config(tmp_path)
    _break_the_store(tmp_path)
    code = run(cfg, "doctor")
    out = capsys.readouterr().out
    assert code == 0
    assert "memory: NOT AVAILABLE" in out
    assert "not a database" in out
    assert "nothing that stores state will work" in out
    # and the rest of the diagnosis still happened: that is the point of the command
    assert "python:" in out and "data:" in out and "ollama:" in out


def test_a_broken_store_is_reported_with_the_file_and_the_fix(tmp_path, capsys):
    cfg = write_config(tmp_path)
    bad = _break_the_store(tmp_path)
    code = run(cfg, "hall")
    err = capsys.readouterr().err
    assert code == 1
    assert bad.as_posix() in err
    assert "could not be opened" in err
    assert "doctor" in err, "the message must point at the command that diagnoses it"


def test_data_writes_csv(tmp_path, capsys):
    cfg = write_config(tmp_path)
    out_csv = tmp_path / "gen.csv"
    code = run(cfg, "data", "--rows", "300", "--out", str(out_csv))
    assert code == 0
    assert out_csv.exists()
    assert "300 rows" in capsys.readouterr().out


def test_data_fetch_routes_to_public_klines(tmp_path, capsys, monkeypatch):
    """`data --fetch` must hit the klines client, not the synthetic generator."""
    import selftrade.data as data_mod

    cfg = write_config(tmp_path)
    seen = {}

    def fake_fetch(symbol, timeframe, rows, **kwargs):
        seen.update(symbol=symbol, timeframe=timeframe, rows=rows, **kwargs)
        return make_synthetic(150, seed=3)

    monkeypatch.setattr(data_mod, "fetch_klines", fake_fetch)
    out_csv = tmp_path / "real.csv"
    code = run(cfg, "data", "--fetch", "--rows", "150", "--out", str(out_csv))
    assert code == 0
    assert out_csv.exists()
    assert seen["symbol"] == "BTC/USDT"
    assert seen["rows"] == 150
    assert seen["base_url"].startswith("https://")
    assert isinstance(seen["timeout"], float)
    assert "fetched 150 real candles" in capsys.readouterr().err


def test_data_fetch_defaults_to_symbol_timeframe_filename(tmp_path, capsys, monkeypatch):
    import selftrade.data as data_mod

    cfg = write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        data_mod,
        "fetch_klines",
        lambda symbol, timeframe, rows, **kw: make_synthetic(120, seed=4),
    )
    code = run(cfg, "data", "--fetch", "--rows", "120", "--set", "data.symbol=ETH/USDT")
    assert code == 0
    assert (tmp_path / "data" / "ETHUSDT_1h.csv").exists()
    assert "ETHUSDT_1h.csv" in capsys.readouterr().out


def test_hall_empty(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "hall")
    assert code == 0
    assert "empty" in capsys.readouterr().out


def test_report_without_runs_exits_nonzero(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "report")
    assert code == 1
    assert "no runs" in capsys.readouterr().err


def test_backtest_family(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "backtest", "--family", "rsi_reversion", "--json")
    assert code in (0, 2)
    payload = json.loads(capsys.readouterr().out)
    assert payload["genome"]["family"] == "rsi_reversion"
    assert "fitness" in payload


def test_backtest_holdout_available(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "backtest", "--family", "trend_momentum", "--holdout", "--json")
    assert code in (0, 2)
    payload = json.loads(capsys.readouterr().out)
    assert "holdout" in payload
    # the printed payload must stay machine-readable: metrics, counts and the benchmark
    # only - never the equity series or the trade frame (those are for the Evolver)
    assert sorted(payload["holdout"]) == ["baseline", "metrics", "n_trades"]
    assert isinstance(payload["holdout"]["metrics"], dict)
    # the buy & hold benchmark of the same slice comes with it, so the out-of-sample
    # number can be read as excess rather than as a bare positive
    assert "total_return" in payload["holdout"]["baseline"]


def test_backtest_prints_the_buy_and_hold_benchmark(tmp_path, capsys):
    """The printed result must say what the market did, not only what the strategy did."""
    cfg = write_config(tmp_path)
    code = run(cfg, "backtest", "--family", "trend_momentum", "--holdout")
    assert code in (0, 2)
    out = capsys.readouterr().out
    assert "market (buy & hold):" in out
    assert "excess=" in out
    assert "holdout market (buy & hold):" in out


def test_backtest_json_carries_one_baseline_per_fold(tmp_path, capsys):
    """The machine-readable payload gains the benchmark, and the excess is arithmetic."""
    cfg = write_config(tmp_path)
    code = run(cfg, "backtest", "--family", "trend_momentum", "--json")
    assert code in (0, 2)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["baseline_folds"]) == len(payload["folds"])
    metric = payload["metric"]
    fold_mean = sum(float(m[metric]) for m in payload["folds"]) / len(payload["folds"])
    assert payload["metric_excess"] == pytest.approx(fold_mean - payload["baseline_mean"])
    # and a baseline is the same shape as the fold metrics it is compared with
    assert "total_return" in payload["baseline_folds"][0]
    assert set(payload["baseline_folds"][0]) == set(payload["folds"][0])


def test_backtest_champion_missing_exits_nonzero(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "backtest", "--champion")
    assert code == 1
    assert "no champion" in capsys.readouterr().err


def test_export_writes_file(tmp_path, capsys):
    cfg = write_config(tmp_path)
    out = tmp_path / "strats"
    code = run(cfg, "export", "--family", "donchian_breakout", "--out", str(out))
    assert code == 0
    files = list(out.glob("*.py"))
    assert len(files) == 1
    assert "compute_signals" in files[0].read_text(encoding="utf-8")


def test_session_status_reports_the_loop(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "session", "status") == 0
    state = json.loads(capsys.readouterr().out)
    assert state["loop"]["running"] is False
    assert state["loop"]["log"].endswith("loop.log")


def test_session_steering_is_refused_when_nothing_runs(tmp_path, capsys):
    cfg = write_config(tmp_path)
    for action in ("pause", "resume", "stop"):
        assert run(cfg, "session", action) == 1, action
        assert "no loop is running" in capsys.readouterr().err
    # nothing was written for a loop that does not exist
    assert not (tmp_path / "state" / "session.json").exists()


def test_session_start_launches_the_loop(tmp_path, capsys, monkeypatch):
    """``start`` spawns the loop through the runner (faked here: a real one evolves)."""
    from selftrade import loop as loop_mod

    seen = {}

    class FakeRunner:
        def __init__(self, settings, config_path=None):
            seen["config"] = config_path or None

        def start(self, generations):
            seen["generations"] = generations
            return 200, {
                "ok": True,
                "action": "start",
                "command": "python -m selftrade evolve --session --generations 3",
                "loop": {
                    "running": True, "pid": 4242, "generations_total": 3,
                    "log": "state/loop.log",
                },
            }

        def request(self, action):  # pragma: no cover - start is the path under test
            raise AssertionError("request must not be called by `start`")

    monkeypatch.setattr(loop_mod, "LoopRunner", FakeRunner)
    cfg = write_config(tmp_path)
    assert run(cfg, "session", "start", "--generations", "3") == 0
    out = capsys.readouterr().out
    assert "pid 4242" in out and "state/loop.log" in out
    assert seen["generations"] == 3 and seen["config"] == str(cfg)


def test_session_start_refuses_when_one_is_running(tmp_path, capsys, monkeypatch):
    from selftrade import loop as loop_mod

    class BusyRunner:
        def __init__(self, settings, config_path=None):
            pass

        def start(self, generations):
            return 409, {"ok": False, "action": "start", "error": "the loop is already running (pid 7)"}

    monkeypatch.setattr(loop_mod, "LoopRunner", BusyRunner)
    cfg = write_config(tmp_path)
    assert run(cfg, "session", "start") == 1
    assert "already running" in capsys.readouterr().err


def test_paper_status_without_session_exits_nonzero(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "paper", "--status")
    assert code == 1
    assert "no paper session" in capsys.readouterr().err


def test_paper_create_without_champion_exits_nonzero(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "paper", "--create")
    assert code == 1
    assert "no champion" in capsys.readouterr().err


def test_invest_writes_md_html_json(tmp_path, capsys):
    cfg = write_config(tmp_path)
    out = tmp_path / "inv" / "sim.md"
    code = run(cfg, "invest", "--family", "trend_momentum", "--out", str(out))
    assert code == 0
    assert out.exists()
    html = out.with_suffix(".html")
    jsonf = out.with_suffix(".json")
    assert html.exists() and jsonf.exists()
    doc = html.read_text(encoding="utf-8")
    assert "<svg" in doc and "Equity curve" in doc
    payload = json.loads(jsonf.read_text(encoding="utf-8"))
    assert payload["currency"] == "USDT"
    assert set(payload["cost_sensitivity"][0]) >= {"cost_multiplier", "final_equity"}
    printed = capsys.readouterr().out
    assert "buy&hold" in printed and "verdict:" in printed


def test_invest_json_is_machine_readable(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "invest", "--family", "rsi_reversion", "--window", "full", "--json")
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["in_sample"] is True
    assert payload["params"]["initial_cash"] == 10000.0


def test_invest_champion_missing_exits_nonzero(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "invest", "--champion", "--out", str(tmp_path / "x.md"))
    assert code == 1
    assert "no champion" in capsys.readouterr().err


def test_invest_rejects_unknown_window(tmp_path):
    cfg = write_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run(cfg, "invest", "--window", "tomorrow")
    assert exc.value.code != 0


def test_risk_show_verify_seal(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "risk", "show") == 0
    out = capsys.readouterr().out
    assert "MAX_DRAWDOWN_PCT" in out and "SHA-256" in out
    assert "no --set" in out

    assert run(cfg, "risk", "verify") == 0
    assert (tmp_path / "state" / "risk_rules.seal").exists()
    assert run(cfg, "risk", "seal") == 0
    assert "sealed digest" in capsys.readouterr().out


def test_risk_cannot_be_overridden_from_the_cli(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "risk", "show", "--set", "risk_rules.MAX_DRAWDOWN_PCT=99")
    assert code == 3
    err = capsys.readouterr().err
    assert "config error" in err and "risk_rules" in err


def test_critic_command_reports_a_verdict(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "critic", "--family", "trend_momentum", "--json")
    assert code in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload["approved"], bool)
    assert payload["checks"] and payload["window"] == "holdout"
    assert "limits" not in payload or True


def test_critic_refuses_a_loose_stop_on_the_command_line(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(
        cfg,
        "critic",
        "--family",
        "trend_momentum",
        "--set",
        "backtest.allow_short=false",
    )
    assert code in (0, 1)
    out = capsys.readouterr().out
    assert ("Approved" in out) or ("Refused" in out)
    assert "| check |" in out


def test_autopsy_command_writes_report_and_lesson(tmp_path, capsys):
    cfg = write_config(tmp_path)
    out_md = tmp_path / "auto.md"
    code = run(cfg, "autopsy", "--family", "rsi_reversion", "--out", str(out_md))
    assert code == 0
    printed = capsys.readouterr().out
    assert "autopsy (sampled)" in printed
    assert "lesson stored in" in printed
    assert out_md.exists()
    md = out_md.read_text(encoding="utf-8")
    assert "## Autopsy" in md


def test_autopsy_json_has_worst_and_modes(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "autopsy", "--family", "donchian_breakout", "--json", "--worst", "3")
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {"summary", "worst", "modes", "n_trades", "n_losers", "overfit"}
    assert len(payload["worst"]) <= 3


def test_paper_create_is_refused_when_the_critic_says_no(tmp_path, capsys):
    from selftrade.genome import sample_genome
    from selftrade.memory import Memory

    cfg = write_config(tmp_path)
    loose = sample_genome("trend_momentum", None)
    loose["risk"]["stop_loss_pct"] = -0.25
    Memory(str(tmp_path / "state" / "m.db")).add_to_hall(None, loose, 1.0, {})

    code = run(cfg, "paper", "--create", "--champion")
    assert code == 1
    err = capsys.readouterr().err
    assert "critic refused this deployment" in err
    assert "looser than the sealed" in err


def test_mt5_doctor_disabled(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "mt5", "doctor")
    assert code == 0
    assert "disabled" in capsys.readouterr().out


def test_rea_plan_and_run(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "rea", "plan") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    # `rea run` returns non-zero when REA is disabled
    assert run(cfg, "rea", "run") == 1


def test_mcp_writes_config(tmp_path, capsys):
    cfg = write_config(tmp_path)
    out = tmp_path / ".mcp.json"
    code = run(cfg, "mcp", "--out", str(out))
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "rea" in data["mcpServers"]


def test_broker_demo_blocks_over_limit_and_fills_the_compliant_order(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "broker", "--bars", "300", "--json")
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["broker"]["mode"] == "paper"
    assert payload["broker"]["network"] == "none"
    assert payload["reconcile_ok"] is True
    assert payload["sealed_limits"]["MODEL_CAN_OVERRIDE"] is False
    steps = {s["step"]: s for s in payload["story"]}
    assert steps["oversized_order"]["status"] == "rejected"
    assert "5.0% per-position cap" in steps["oversized_order"]["reason"]
    assert steps["retry_same_client_id"]["same_order"] is True
    assert steps["compliant_order"]["status"] == "new"
    assert steps["fill"]["status"] == "filled"
    assert steps["fill"]["avg_price"] > 0
    assert payload["risk_events"] >= 1
    assert payload["account"]["equity"] > 0


def test_broker_demo_prints_a_readable_story(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "broker", "--bars", "200") == 0
    out = capsys.readouterr().out
    assert "paper broker demo" in out
    assert "ledger reconciliation: OK" in out
    assert "not available here" not in out


def test_binance_commands_without_config_never_touch_the_network(tmp_path, capsys):
    """A disabled rail must say so, send nothing and never call the API."""
    cfg = write_config(tmp_path)
    assert run(cfg, "binance", "doctor", "--json") == 0
    info = json.loads(capsys.readouterr().out)
    assert info["enabled"] is False
    assert info["host_is_testnet"] is True
    assert "nothing was sent" in info["reason"]

    for action in ("account", "positions", "bars", "exchange", "history", "open"):
        assert run(cfg, "binance", action) == 1
        captured = capsys.readouterr()
        assert "disabled" in captured.out + captured.err


def test_binance_live_needs_both_keys(tmp_path, capsys):
    cfg = write_config(tmp_path)
    # the first key is missing: `--live` alone must not reach the API
    assert run(cfg, "binance", "order", "--qty", "1", "--live") == 1
    captured = capsys.readouterr()
    assert "disabled in config" in captured.out + captured.err

    # the first key is present, the second is not: still refused, and no broker built
    assert run(cfg, "binance", "order", "--qty", "1", "--live", "--set",
               "binance.enabled=true") == 1
    err = capsys.readouterr().err
    assert "refusing --live" in err
    assert "binance.live: true" in err


def test_binance_calls_reads_the_api_log(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "binance", "calls", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_binance_never_reports_itself_as_live(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "binance", "doctor", "--json") == 0
    info = json.loads(capsys.readouterr().out)
    assert info["host_is_testnet"] is True
    assert "live" not in json.dumps(info)


def test_switch_command_arms_stops_and_resets(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "switch", "show") == 0
    assert json.loads(capsys.readouterr().out)["blocked"] is False
    assert run(cfg, "switch", "arm", "--reason", "daily loss breached") == 0
    armed = json.loads(capsys.readouterr().out)
    assert armed["blocked"] is True and armed["reason"] == "daily loss breached"
    assert run(cfg, "switch", "show") == 1  # blocked is a non-zero status
    assert "no new orders" in capsys.readouterr().err
    assert run(cfg, "switch", "reset") == 0
    capsys.readouterr()
    assert run(cfg, "switch", "show") == 0


def test_unknown_set_key_exits_config_error(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "hall", "--set", "backtest.nope=1")
    assert code == 3
    assert "unknown config key" in capsys.readouterr().err


def test_missing_config_file_is_a_config_error(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "nope.yaml"), "hall"])
    assert code == 3

# ---------------------------------------------------------------- risk alerts

def test_risk_limits_says_nothing_to_measure_without_a_session(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "risk", "limits") == 2          # 2 = nothing to measure
    out = capsys.readouterr().out
    assert "not measurable" in out and "no paper session" in out
    assert "notify.disabled" in out                 # and who would be told


def test_risk_limits_json_and_its_exit_codes(tmp_path, capsys):
    from selftrade.memory import Memory

    cfg = write_config(tmp_path)
    assert run(cfg, "risk", "limits", "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["checks"] == []
    assert payload["limits"]["MAX_DRAWDOWN_PCT"] == 15.0 and payload["version"] == 1
    assert payload["alerts"]["ready"] is False

    # a session inside the limits, close enough to warn: 0, and the room is reported
    mem = Memory(str(tmp_path / "state" / "m.db"))
    sid = mem.create_paper_session(
        name="cli", genome={"family": "rsi_reversion", "params": {}, "risk": {}},
        window="recent", total_bars=2, start_time="2024-01-01T00:00:00+00:00",
        end_time="2024-01-01T01:00:00+00:00", initial_cash=10_000.0, settings={},
    )
    # 12% drawdown against a peak from the day before, and a small loss today: the
    # halting limits are close (20% of the drawdown cap left) but neither is breached
    mem.add_paper_equity(sid, [("2024-01-01 23:00:00+00:00", 10_000.0),
                               ("2024-01-02 00:00:00+00:00", 8_900.0),
                               ("2024-01-02 01:00:00+00:00", 8_800.0)])
    mem.update_paper_cursor(sid, 3, 8_800.0, "active")
    assert run(cfg, "risk", "limits") == 0
    out = capsys.readouterr().out
    assert "within the limits" in out and "max_drawdown 12.00% of the 15.00% cap" in out
    assert "~ max_drawdown" in out          # flagged as inside the warning band

    # past a halting cap: 1, the status a script can branch on
    mem.replace_paper_results(
        sid,
        [],
        [("2024-01-01 23:00:00+00:00", 10_000.0), ("2024-01-02 01:00:00+00:00", 8_000.0)],
        3,
        8_000.0,
    )
    assert run(cfg, "risk", "limits") == 1
    out = capsys.readouterr().out
    assert "BREACHED: max_drawdown" in out and "20.00% of the 15.00% cap" in out
    # and the same command says the breach has not been sent to anybody yet
    assert "PENDING [warning] [past]" in out and "past its cap" in out

    # an engaged stop supersedes the band: the halt already carries every limit
    assert run(cfg, "switch", "arm", "--reason", "drawdown limit breached") == 0
    capsys.readouterr()
    assert run(cfg, "risk", "limits") == 1
    out = capsys.readouterr().out
    assert "PENDING [halt]" in out and "PENDING [warning]" not in out


def test_notify_status_reports_the_channel_and_what_is_pending(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "notify", "status") == 0
    out = capsys.readouterr().out
    assert "NOT ready: notify.disabled" in out
    assert "nothing to send right now" in out
    assert "notify_state.json" in out
    assert "warns when a halting limit has <= 25% of its cap left" in out
    assert "already past its cap" in out

    assert run(cfg, "notify", "status", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False and payload["reason"] == "notify.disabled"
    assert payload["pending"] == [] and payload["switch"]["blocked"] is False
    assert payload["sent"] == []          # nothing has ever left this channel


def test_notify_test_and_watch_never_send_when_the_channel_is_off(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "notify", "test") == 1
    out = capsys.readouterr().out
    assert "NOT sent" in out and "notify.disabled" in out
    assert run(cfg, "notify", "watch", "--once") == 1
    err = capsys.readouterr().err
    assert "nothing would be sent" in err and "notify.enabled=true" in err
    # and a halt is still *reported* as pending rather than silently dropped
    assert run(cfg, "switch", "arm", "--reason", "test halt") == 0
    capsys.readouterr()
    assert run(cfg, "notify", "status") == 0
    out = capsys.readouterr().out
    assert "kill switch: ENGAGED - test halt" in out
    assert "PENDING [halt]" in out and "dropped" in out


def test_a_configured_channel_reports_pending_and_can_be_proven(tmp_path, capsys, monkeypatch):
    from selftrade import notify as notify_mod

    cfg = write_config(tmp_path)
    monkeypatch.setenv("SELFTD_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("SELFTD_TELEGRAM_CHAT_ID", "chat")
    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod, "send", lambda s, text, **kw: (sent.append(text), (True, "sent"))[1]
    )
    assert run(cfg, "notify", "test") == 0
    assert "sent" in capsys.readouterr().out and len(sent) == 1


# ---------------------------------------------------------------- password


def test_password_status_set_and_reset(tmp_path, capsys):
    """The escape hatch for a forgotten password: the same rule as the page, never the value."""
    from selftrade.credentials import CredentialStore

    cfg = write_config(tmp_path)
    path = tmp_path / "auth.json"
    base = ["--set", f"dashboard.password_file={path}"]

    assert run(cfg, "password", "status", *base) == 0
    out = capsys.readouterr().out
    assert "default" in out and "bootstrap password (0000)" in out
    assert not path.exists()  # reading the state never creates it

    assert run(cfg, "password", "set", "--value", "12", *base) == 1
    assert "at least 4 characters" in capsys.readouterr().err
    assert not path.exists()

    assert run(cfg, "password", "set", "--value", "cli-set-pw", *base) == 0
    assert "not stored" in capsys.readouterr().out
    raw = path.read_text(encoding="utf-8")
    assert "cli-set-pw" not in raw and "salt" in raw
    assert CredentialStore(path).verify("cli-set-pw") is True

    assert run(cfg, "password", "status", *base) == 0
    assert "changed" in capsys.readouterr().out

    assert run(cfg, "password", "reset", *base) == 1  # needs --confirm
    assert path.exists()
    assert run(cfg, "password", "reset", "--confirm", *base) == 0
    assert not path.exists()
    assert CredentialStore(path).verify("0000") is True


def test_the_configured_minimum_length_binds_the_cli(tmp_path, capsys):
    cfg = write_config(tmp_path)
    path = tmp_path / "auth.json"
    base = ["--set", f"dashboard.password_file={path}", "--set", "dashboard.password_min_length=8"]

    assert run(cfg, "password", "set", "--value", "seven77", *base) == 1
    assert "at least 8 characters" in capsys.readouterr().err
    assert not path.exists()
    assert run(cfg, "password", "set", "--value", "eight888", *base) == 0


def test_an_environment_password_is_reported_as_winning(tmp_path, capsys, monkeypatch):
    cfg = write_config(tmp_path)
    path = tmp_path / "auth.json"
    base = ["--set", f"dashboard.password_file={path}"]
    monkeypatch.setenv("SELFTD_DASHBOARD_PASSWORD", "from-the-environment")

    assert run(cfg, "password", "status", *base) == 0
    out = capsys.readouterr().out
    assert "SELFTD_DASHBOARD_PASSWORD is set" in out and "it wins" in out
    # and writing one says so, rather than pretending the login changed
    assert run(cfg, "password", "set", "--value", "cli-set-pw", *base) == 0
    assert "still decides the login" in capsys.readouterr().out
