"""Tests for the stress module: Monte Carlo paths and parameter sensitivity.

The point of these tests is not that the numbers look plausible.  It is that the
sampler is *the* thing claimed: a fixed seed reproduces to the last digit, a
shuffle holds the realized return exactly and only moves the path, and the
sensitivity axis reports the perturbation that actually ran (clamps included).
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selftrade import stress
from selftrade.backtest import simulate
from selftrade.cli import build_parser, main
from selftrade.data import make_synthetic, save_csv
from selftrade.genome import validate_genome
from selftrade.risk_rules import LIMITS

TRADE_COLUMNS = [
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "size",
    "pnl",
    "return_pct",
    "bars_held",
    "exit_reason",
]


def trades(returns, reasons=None, equity=10_000.0):
    """A minimal but well-formed trade frame; only return_pct/exit_reason matter."""
    r = np.asarray(returns, dtype="float64")
    n = len(r)
    idx = pd.date_range("2021-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "direction": ["long"] * n,
            "entry_time": idx,
            "exit_time": idx,
            "entry_price": 100.0,
            "exit_price": 100.0,
            "size": 1.0,
            "pnl": r * equity,
            "return_pct": r,
            "bars_held": 1,
            "exit_reason": list(reasons) if reasons is not None else ["signal"] * n,
        },
        columns=TRADE_COLUMNS,
    )


def compound(returns) -> float:
    r = np.asarray(returns, dtype="float64")
    return 0.0 if not len(r) else float(np.prod(1.0 + r) - 1.0)


GEN = validate_genome(
    {
        "family": "donchian_breakout",
        "params": {"entry_period": 100, "exit_period": 10, "atr_period": 14},
        "risk": {"stop_loss_pct": -0.05, "take_profit_pct": None, "trailing_pct": None, "trade_shorts": False},
    }
)

#: A genome that actually trades on the synthetic series the report tests use, so the
#: path axis is exercised with a real sequence rather than an empty one.
TRADING_GEN = validate_genome(
    {
        "family": "rsi_reversion",
        "params": {"rsi_period": 7, "rsi_buy": 35, "rsi_exit": 65, "sma_filter_period": 100, "use_sma_filter": False},
        "risk": {"stop_loss_pct": -0.05, "take_profit_pct": 0.15, "trailing_pct": None, "trade_shorts": False},
    }
)


# ---------------------------------------------------------------- the sequence


def test_closed_trades_drops_the_end_of_data_mark():
    t = trades([0.1, -0.05, 0.02], reasons=["signal", "stop", "end_of_data"])
    closed = stress.closed_trades(t)
    assert len(closed) == 2
    assert "end_of_data" not in set(closed["exit_reason"])
    assert stress.trade_returns(t).tolist() == [0.1, -0.05]


def test_trade_returns_on_an_empty_frame_is_empty():
    assert len(stress.trade_returns(None)) == 0
    assert len(stress.trade_returns(pd.DataFrame(columns=TRADE_COLUMNS))) == 0


def test_path_stats_are_the_compounded_equity():
    # 10% up, 50% down, 20% up from 10,000: 11000 -> 5500 -> 6600.
    t = trades([0.10, -0.50, 0.20])
    realized = stress.realized_path(t, initial_cash=10_000.0)
    assert realized["final_return"] == pytest.approx(0.66 - 1.0, rel=1e-12)
    assert realized["max_drawdown"] == pytest.approx(0.5, rel=1e-12)
    assert realized["max_losing_streak"] == pytest.approx(1.0)
    assert realized["worst_trade_return"] == pytest.approx(-0.50)


def test_losing_streaks_count_the_longest_run():
    t = trades([0.1, -0.1, -0.1, -0.1, 0.2, -0.1, 0.3])
    assert stress.realized_path(t)["max_losing_streak"] == pytest.approx(3.0)


def test_realized_path_matches_the_engine_to_floating_point():
    """Every trade's return_pct is pnl/equity_before, so compounding reproduces the engine."""
    df = make_synthetic(1500, seed=5)
    res = simulate(df, GEN, initial_cash=10_000.0, fee_bps=10.0, slippage_bps=5.0, position_size=0.95)
    all_ret = res.trades["return_pct"].to_numpy(dtype="float64")
    assert compound(all_ret) == pytest.approx(res.metrics["total_return"], rel=1e-9, abs=1e-12)

    closed = stress.trade_returns(res.trades)
    realized = stress.realized_path(res.trades, initial_cash=10_000.0)
    assert realized["final_return"] == pytest.approx(compound(closed), rel=1e-12, abs=1e-15)


# ---------------------------------------------------------------- Monte Carlo


def test_monte_carlo_is_reproducible_for_a_fixed_seed():
    r = list(np.linspace(-0.03, 0.04, 60))
    a = stress.monte_carlo(r, paths=200, seed=7)
    b = stress.monte_carlo(r, paths=200, seed=7)
    assert a["percentiles"] == b["percentiles"]
    assert a["probabilities"] == b["probabilities"]
    assert a["path_digest"] == b["path_digest"]
    assert len(a["path_digest"]) == 64


def test_a_different_seed_moves_the_percentiles():
    r = list(np.linspace(-0.03, 0.04, 60))
    a = stress.monte_carlo(r, paths=200, seed=7)
    b = stress.monte_carlo(r, paths=200, seed=8)
    assert a["path_digest"] != b["path_digest"]


def test_shuffle_keeps_the_realized_return_and_only_moves_the_path():
    r = [0.05, -0.02, 0.03, -0.04, 0.01, 0.02, -0.01, 0.06]
    mc = stress.monte_carlo(r, paths=64, seed=3, method="shuffle")
    realized = mc["realized"]["final_return"]
    for q in stress.PERCENTILES:
        assert mc["percentiles"]["final_return"][f"p{q}"] == pytest.approx(realized, rel=1e-12)
    # the drawdown is order dependent, so it must have spread
    assert mc["percentiles"]["max_drawdown"]["p95"] > mc["percentiles"]["max_drawdown"]["p5"]


def test_block_longer_than_the_sequence_is_the_identity():
    r = [0.05, -0.02, 0.03, -0.04]
    mc = stress.monte_carlo(r, paths=16, seed=1, method="block", block=99)
    assert mc["percentiles"]["final_return"]["p50"] == pytest.approx(
        mc["realized"]["final_return"], rel=1e-12
    )
    assert mc["percentiles"]["max_drawdown"]["p50"] == pytest.approx(
        mc["realized"]["max_drawdown"], rel=1e-12
    )


def test_bootstrap_percentiles_are_ordered():
    r = list(np.linspace(-0.04, 0.05, 80))
    mc = stress.monte_carlo(r, paths=300, seed=2)
    for key in stress.PATH_METRICS:
        vals = [mc["percentiles"][key][f"p{q}"] for q in stress.PERCENTILES]
        assert vals == sorted(vals)


def test_drawdown_limit_defaults_to_the_sealed_limit():
    mc = stress.monte_carlo([0.01, -0.01], paths=10, seed=1)
    assert mc["drawdown_limit"] == pytest.approx(LIMITS.MAX_DRAWDOWN_PCT / 100.0)
    over = stress.monte_carlo([0.01, -0.01], paths=10, seed=1, drawdown_limit=0.001)
    assert over["drawdown_limit"] == pytest.approx(0.001)
    assert over["probabilities"]["drawdown_over_limit"] >= mc["probabilities"]["drawdown_over_limit"]


def test_unknown_method_is_refused():
    with pytest.raises(ValueError):
        stress.monte_carlo([0.01], paths=2, method="teleport")


def test_monte_carlo_without_trades_says_so():
    mc = stress.monte_carlo([], paths=100, seed=1)
    assert mc["paths"] == 0 and mc["trades"] == 0
    assert mc["percentiles"] == {}
    assert "no closed trades" in mc["note"]


# ---------------------------------------------------------------- perturbations


def test_perturbations_move_every_numeric_parameter_both_ways():
    ps = stress.perturbations(GEN, deltas=(0.10, 0.25))
    names = {p["param"] for p in ps}
    assert {"entry_period", "exit_period", "atr_period", "stop_loss_pct"} == names
    # booleans and the null risk knobs have nothing to shade
    assert "use_atr_filter" not in names
    assert "take_profit_pct" not in names and "trailing_pct" not in names
    for p in ps:
        assert p["delta"] != 0
        assert p["value"] != p["base"]
    assert len(ps) == 4 * 4  # 3 numeric params + the stop, two deltas, two directions


def test_risk_perturbations_keep_the_stop_negative():
    ps = [p for p in stress.perturbations(GEN) if p["param"] == "stop_loss_pct"]
    assert len(ps) == 4
    assert all(p["value"] < 0 for p in ps)
    assert {round(p["delta"], 2) for p in ps} == {-0.25, -0.1, 0.1, 0.25}


def test_apply_perturbation_does_not_mutate_the_input():
    before = json.dumps(GEN, sort_keys=True)
    out = stress.apply_perturbation(GEN, {"param": "entry_period", "path": "params", "value": 140})
    assert out["params"]["entry_period"] == 140
    assert json.dumps(GEN, sort_keys=True) == before


def test_apply_perturbation_clamps_to_the_genome_bounds():
    out = stress.apply_perturbation(GEN, {"param": "entry_period", "path": "params", "value": 10_000})
    assert out["params"]["entry_period"] == 150  # IntParam(10, 150)


# ---------------------------------------------------------------- sensitivity


def _runner(sharpe=lambda g: 1.0, n_trades=lambda g: 20.0, dd=lambda g: 0.2):
    def run(genome):
        return {
            "sharpe": float(sharpe(genome)),
            "total_return": 0.05,
            "max_drawdown": float(dd(genome)),
            "n_trades": float(n_trades(genome)),
        }

    return run


def test_sensitivity_runs_every_perturbation_and_reports_the_counts():
    se = stress.sensitivity(GEN, _runner(), objective="sharpe", min_trades=8, max_drawdown=0.6)
    assert se["count"]["perturbations"] == 16
    assert se["count"]["clamped"] == 0
    assert se["baseline"]["passed"] is True
    assert len(se["parameters"]) == 4
    for param in se["parameters"]:
        assert len(param["perturbations"]) == 4
        for p in param["perturbations"]:
            assert p["objective"] == pytest.approx(1.0)


def test_sensitivity_flags_a_perturbation_that_the_bounds_clamp():
    low = validate_genome(
        {
            "family": "donchian_breakout",
            "params": {"entry_period": 10, "exit_period": 10, "atr_period": 14},
            "risk": {"stop_loss_pct": -0.05, "take_profit_pct": None, "trailing_pct": None, "trade_shorts": False},
        }
    )
    se = stress.sensitivity(low, _runner(), objective="sharpe", min_trades=8, max_drawdown=0.6)
    probes = [p for e in se["parameters"] for p in e["perturbations"]]
    clamped = [p for p in probes if p["clamped"]]
    assert clamped, "a -25% move on entry_period=10 has to clamp back to the floor"
    assert all(p["value"] == 10 for p in clamped if p["param"] == "entry_period")
    assert se["count"]["clamped"] == len(clamped)


def test_sensitivity_measures_the_degradation_and_names_the_fragile_parameter():
    def sharpe(g):
        return 1.0 if g["risk"]["stop_loss_pct"] == -0.05 else 0.1

    se = stress.sensitivity(GEN, _runner(sharpe=sharpe), objective="sharpe", min_trades=8, max_drawdown=0.6)
    assert se["worst"]["param"] == "stop_loss_pct"
    assert se["worst"]["objective"] == pytest.approx(0.1)
    assert se["worst"]["objective_share"] == pytest.approx(0.9)
    assert se["fragile_params"] == ["stop_loss_pct"]
    # the untouched parameters perturb to the same 1.0 and are not fragile
    other = next(e for e in se["parameters"] if e["param"] == "atr_period")
    assert other["worst_share"] == pytest.approx(0.0)
    assert other["fragile"] is False


def test_sensitivity_counts_gate_breaks_away_from_the_baseline():
    def n_trades(g):
        return 1.0 if g["params"]["entry_period"] != 100 else 20.0

    se = stress.sensitivity(GEN, _runner(n_trades=n_trades), objective="sharpe", min_trades=8, max_drawdown=0.6)
    assert se["baseline"]["passed"] is True
    assert se["count"]["gate_breaks"] == 4  # the four entry_period probes
    assert all(p["param"] == "entry_period" for p in se["gate_breaks"])
    assert all("n_trades=1 < min_trades=8" == p["gate_reason"] for p in se["gate_breaks"])
    entry = next(e for e in se["parameters"] if e["param"] == "entry_period")
    assert entry["breaks_gate"] is True


def test_sensitivity_reads_the_return_metric_under_its_other_name():
    """`evaluation.metric: return` lives under `total_return`; without the alias
    every perturbation would read 0.0 and be declared harmless."""
    run = lambda g: {"total_return": 0.5, "n_trades": 10.0, "max_drawdown": 0.1}  # noqa: E731
    se = stress.sensitivity(GEN, run, objective="return", min_trades=5, max_drawdown=0.9)
    assert se["baseline"]["value"] == pytest.approx(0.5)
    assert se["count"]["perturbations"] == 16
    assert all(
        p["objective"] == pytest.approx(0.5)
        for e in se["parameters"]
        for p in e["perturbations"]
    )


def test_a_baseline_that_breaks_the_gates_is_recorded_as_such():
    se = stress.sensitivity(
        GEN, _runner(n_trades=lambda g: 2.0), objective="sharpe", min_trades=8, max_drawdown=0.6
    )
    assert se["baseline"]["passed"] is False
    assert "n_trades=2 < min_trades=8" == se["baseline"]["gate_reason"]


# ---------------------------------------------------------------- the report


def _settings(tmp_path):
    from selftrade.config import load_settings

    csv_path = tmp_path / "d.csv"
    save_csv(make_synthetic(1200, seed=21), csv_path)
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
                "  metric: sharpe",
                "evolution:",
                "  generations: 1",
                "  population: 2",
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
    return load_settings(str(cfg))


def test_stress_report_is_reproducible_on_real_pipeline_data(tmp_path):
    from selftrade.data import load_dataframe

    settings = _settings(tmp_path)
    df = load_dataframe(settings)
    a = stress.stress_report(df, TRADING_GEN, settings, paths=200, seed=7, window="full")
    b = stress.stress_report(df, TRADING_GEN, settings, paths=200, seed=7, window="full")
    assert a["n_trades"] > 0
    assert a["monte_carlo"]["path_digest"] == b["monte_carlo"]["path_digest"]
    assert a["monte_carlo"]["percentiles"] == b["monte_carlo"]["percentiles"]
    assert a["degradation"]["parameters"]["worst"] == b["degradation"]["parameters"]["worst"]
    assert set(a) >= {"monte_carlo", "sensitivity", "degradation", "findings", "summary", "lesson"}


def test_stress_report_degradation_agrees_with_the_axes(tmp_path):
    from selftrade.data import load_dataframe

    settings = _settings(tmp_path)
    df = load_dataframe(settings)
    rep = stress.stress_report(df, TRADING_GEN, settings, paths=200, seed=5, window="full")
    deg = rep["degradation"]
    assert rep["n_trades"] > 0
    if rep["n_trades"]:
        assert deg["path"]["realized_return"] == pytest.approx(
            rep["monte_carlo"]["realized"]["final_return"], rel=1e-12
        )
        assert deg["path"]["p95_drawdown"] == pytest.approx(
            rep["monte_carlo"]["percentiles"]["max_drawdown"]["p95"], rel=1e-12
        )
        assert 0.0 <= deg["path"]["prob_drawdown_over_limit"] <= 1.0
    assert deg["parameters"]["baseline"] == pytest.approx(rep["sensitivity"]["baseline"]["value"])
    assert deg["parameters"]["worst"] == pytest.approx(rep["sensitivity"]["worst"]["objective"])
    assert rep["lesson"]


def test_shuffle_report_holds_the_realized_return_on_real_data(tmp_path):
    from selftrade.data import load_dataframe

    settings = _settings(tmp_path)
    df = load_dataframe(settings)
    rep = stress.stress_report(df, TRADING_GEN, settings, paths=100, seed=3, method="shuffle", window="full")
    assert rep["n_trades"] > 0
    if rep["n_trades"]:
        realized = rep["monte_carlo"]["realized"]["final_return"]
        table = rep["monte_carlo"]["percentiles"]["final_return"]
        assert table["p5"] == pytest.approx(realized, rel=1e-9)
        assert table["p95"] == pytest.approx(realized, rel=1e-9)


def test_render_markdown_and_console_do_not_crash(tmp_path):
    from selftrade.data import load_dataframe

    settings = _settings(tmp_path)
    df = load_dataframe(settings)
    rep = stress.stress_report(df, TRADING_GEN, settings, paths=50, seed=1, window="full")
    assert rep["n_trades"] > 0
    md = stress.render_markdown(rep)
    assert "## Stress" in md and "### Path risk" in md
    lines = stress.console_lines(rep)
    assert lines and "Stress (" in lines[0]
    assert any("sensitivity" in line for line in lines)


# ---------------------------------------------------------------- CLI


def write_config(tmp_path) -> Path:
    csv_path = tmp_path / "d.csv"
    save_csv(make_synthetic(1200, seed=77), csv_path)
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


def test_parser_exposes_stress():
    p = build_parser()
    names = set(p._subparsers._group_actions[0].choices)
    assert "stress" in names


def _seed_champion(tmp_path):
    from selftrade.memory import Memory

    Memory(str(tmp_path / "state" / "m.db")).add_to_hall(None, TRADING_GEN, 1.0, {})


def test_stress_command_writes_a_report_and_a_lesson(tmp_path, capsys):
    cfg = write_config(tmp_path)
    _seed_champion(tmp_path)
    out_md = tmp_path / "stress.md"
    code = main(
        [
            "--config",
            str(cfg),
            "stress",
            "--champion",
            "--window",
            "full",
            "--paths",
            "50",
            "--seed",
            "7",
            "--out",
            str(out_md),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "stress (champion)" in printed
    assert "lesson stored in" in printed
    assert out_md.exists()
    assert "## Stress" in out_md.read_text(encoding="utf-8")


def test_stress_command_json_payload(tmp_path, capsys):
    cfg = write_config(tmp_path)
    _seed_champion(tmp_path)
    code = main(
        [
            "--config",
            str(cfg),
            "stress",
            "--champion",
            "--window",
            "full",
            "--paths",
            "50",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {"monte_carlo", "sensitivity", "degradation", "findings", "lesson"}
    assert payload["monte_carlo"]["seed"] == 7
    assert payload["monte_carlo"]["paths"] == 50
    assert payload["n_trades"] > 0


def test_stress_command_rejects_bad_deltas(tmp_path, capsys):
    cfg = write_config(tmp_path)
    code = main(["--config", str(cfg), "stress", "--family", "rsi_reversion", "--deltas", "nope"])
    assert code == 2
    assert "must be numbers" in capsys.readouterr().err
