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
        "rea", "mcp", "mt5", "paper", "invest", "serve", "session",
    } <= names


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
    code = run(cfg, "session", "start", "--generations", "4", "--set", "evolution.seed=11")
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


def test_session_commands(tmp_path, capsys):
    cfg = write_config(tmp_path)
    assert run(cfg, "session", "status") == 0
    assert run(cfg, "session", "pause") == 0
    state = json.loads((tmp_path / "state" / "session.json").read_text(encoding="utf-8"))
    assert state["pending"] == "pause"
    assert run(cfg, "session", "resume") == 0
    assert run(cfg, "session", "start", "--generations", "3") == 0
    state = json.loads((tmp_path / "state" / "session.json").read_text(encoding="utf-8"))
    assert state["generations_total"] == 3


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


def test_unknown_set_key_exits_config_error(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = run(cfg, "hall", "--set", "backtest.nope=1")
    assert code == 3
    assert "unknown config key" in capsys.readouterr().err


def test_missing_config_file_is_a_config_error(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "nope.yaml"), "hall"])
    assert code == 3
