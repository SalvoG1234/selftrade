import numpy as np
import pandas as pd
import pytest

from selftrade.config import load_settings
from selftrade.data import make_synthetic, split_holdout
from selftrade.evaluate import (
    Holdout,
    HoldoutError,
    evaluate_folds,
    make_folds,
    regime_luck_warning,
)
from selftrade.genome import sample_genome, validate_genome


@pytest.fixture(scope="module")
def settings():
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.data.min_rows = 100
    return s


@pytest.fixture(scope="module")
def dev():
    df = make_synthetic(4000, seed=17)
    d, _ = split_holdout(df, 0.2)
    return d


def test_make_folds_counts_and_ordering(dev):
    folds = make_folds(dev, 4, 0.6)
    assert len(folds) == 4
    for f in folds:
        assert f["test"].index[0] > f["train"].index[-1]
        assert len(f["test"]) > 0
        assert len(f["train"]) > 0
    # test windows are consecutive / non-overlapping
    for a, b in zip(folds, folds[1:]):
        assert b["test"].index[0] > a["test"].index[-1]


def test_evaluate_folds_shape(dev, settings):
    g = validate_genome({"family": "trend_momentum", "params": {}})
    rep = evaluate_folds(dev, g, settings)
    for key in ("fitness", "metric_mean", "metric_std", "n_trades", "max_drawdown", "passed", "complexity"):
        assert key in rep
    assert len(rep["folds"]) == len(rep["fold_values"])


def test_fitness_formula(dev, settings):
    from selftrade.evaluate import INSTABILITY_WEIGHT

    g = validate_genome({"family": "rsi_reversion", "params": {}})
    rep = evaluate_folds(dev, g, settings)
    expected = (
        rep["metric_mean"]
        - settings.evaluation.complexity_penalty * rep["complexity"]
        - INSTABILITY_WEIGHT * rep["metric_std"]
    )
    assert rep["fitness"] == pytest.approx(expected)


def test_gate_fails_on_min_trades(dev, settings):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evaluation.min_trades = 10_000
    g = validate_genome({"family": "rsi_reversion", "params": {}})
    rep = evaluate_folds(dev, g, s)
    assert rep["passed"] is False
    assert "min_trades" in rep["gate_reason"]


def test_gate_fails_on_drawdown(dev, settings):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evaluation.max_drawdown = 1e-6
    g = validate_genome({"family": "trend_momentum", "params": {}})
    rep = evaluate_folds(dev, g, s)
    assert rep["passed"] is False
    assert "max_drawdown" in rep["gate_reason"]


def test_gate_passes_with_lax_thresholds(dev, settings):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evaluation.min_trades = 1
    s.evaluation.max_drawdown = 0.99
    g = validate_genome({"family": "donchian_breakout", "params": {}})
    rep = evaluate_folds(dev, g, s)
    assert rep["passed"] is True
    assert rep["gate_reason"] == ""


def test_complexity_penalty_reduces_fitness(dev, settings):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evaluation.complexity_penalty = 5.0
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {"use_sma_filter": True, "rsi_period": 20},
            "risk": {"take_profit_pct": 0.1, "trailing_pct": 0.05},
        }
    )
    rep = evaluate_folds(dev, g, s)
    assert rep["complexity"] >= 3
    assert rep["fitness"] < rep["metric_mean"]


def test_reproducibility(dev, settings):
    g = sample_genome("trend_momentum", np.random.default_rng(123))
    a = evaluate_folds(dev, g, settings)
    b = evaluate_folds(dev, g, settings)
    assert a["fitness"] == b["fitness"]
    assert a["fold_values"] == b["fold_values"]


def test_holdout_read_once():
    df = make_synthetic(1000, seed=1)
    h = Holdout(df)
    assert h.reads == 0
    out = h.read()
    assert len(out) == 1000
    assert h.reads == 1
    assert h.consumed
    with pytest.raises(HoldoutError):
        h.read()
    assert h.reads == 1  # a rejected read must not bump the counter


def test_holdout_reads_exactly_once_per_run(dev, settings):
    """Simulate an evolution run: many fold evaluations, one holdout read."""
    _, hold = split_holdout(make_synthetic(4000, seed=17), 0.2)
    guard = Holdout(hold)
    reads_during_evolution = guard.reads
    for _ in range(5):
        g = sample_genome(rng=np.random.default_rng(1))
        evaluate_folds(dev, g, settings)
    # Evolution never touches the holdout.
    assert guard.reads == reads_during_evolution == 0
    # The final check reads it once.
    champ = validate_genome({"family": "trend_momentum", "params": {}})
    guard.read()
    assert guard.reads == 1
    from selftrade.evaluate import holdout_eval

    holdout_eval(guard.df, champ, settings)


def test_regime_luck_warning():
    assert regime_luck_warning([0.1, 0.2, 0.15], 5.0) is not None
    assert regime_luck_warning([-0.3, -0.2], 0.4) is not None
    assert regime_luck_warning([0.5, 0.52, 0.48], 0.51) is None
    assert regime_luck_warning([], 1.0) is None


# ------------------------------------------------------------- the buy & hold benchmark

def _frame(opens, closes):
    """A price frame with just enough columns for the engine to read."""
    idx = pd.date_range("2024-01-01", periods=len(opens), freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [float(v) for v in opens],
            "high": [float(max(o, c)) for o, c in zip(opens, closes)],
            "low": [float(min(o, c)) for o, c in zip(opens, closes)],
            "close": [float(v) for v in closes],
            "volume": [1.0] * len(opens),
        },
        index=idx,
    )


def test_the_benchmark_is_buy_and_hold_with_the_entry_cost_paid(settings):
    """A market that doubles: the benchmark captures the move, minus the one entry cost."""
    from selftrade.evaluate import baseline_metrics

    s = load_settings("config.yaml")
    s.backtest.fee_bps = 10.0
    s.backtest.slippage_bps = 5.0
    s.backtest.initial_cash = 1000.0
    cost = (s.backtest.fee_bps + s.backtest.slippage_bps) / 10_000.0
    df = _frame([100.0] * 6, [100.0, 120.0, 140.0, 160.0, 180.0, 200.0])
    m = baseline_metrics(df, s)
    # cash parked at the first open (+cost), held to the last close
    assert m["total_return"] == pytest.approx(2.0 / (1.0 + cost) - 1.0)
    assert m["final_equity"] == pytest.approx(1000.0 * 2.0 / (1.0 + cost))
    # and it never claims a trade: it is a holding, not a round trip
    assert m["n_trades"] == 0


def test_a_flat_market_costs_the_entry_and_nothing_else(settings):
    """A flat benchmark is not zero: the entry is paid once, and that is the whole loss.

    The drawdown reads `0.0` here and that is the metric's own definition, not a bug: the
    recorded curve is constant, and a drawdown is measured peak-to-trough *within* it, so
    the entry cost is a level the curve opens at rather than a fall inside it.  A strategy's
    curve behaves the same way on its entry bar, so the two columns stay comparable.
    """
    from selftrade.evaluate import baseline_metrics

    s = load_settings("config.yaml")
    s.backtest.fee_bps = 10.0
    s.backtest.slippage_bps = 0.0
    cost = s.backtest.fee_bps / 10_000.0
    m = baseline_metrics(_frame([100.0] * 5, [100.0] * 5), s)
    assert m["total_return"] == pytest.approx(-cost / (1.0 + cost), rel=1e-9)
    assert m["max_drawdown"] == 0.0
    assert m["n_trades"] == 0


def test_the_folds_carry_the_benchmark_of_their_own_test_slice(dev, settings):
    """The benchmark is measured where the strategy was: the test window, never the train."""
    from selftrade.evaluate import baseline_metrics

    g = validate_genome({"family": "trend_momentum", "params": {}})
    rep = evaluate_folds(dev, g, settings)
    folds = make_folds(dev, settings.evaluation.folds, settings.evaluation.train_frac)
    assert len(rep["baseline_folds"]) == len(rep["folds"]) == len(folds)
    for got, f in zip(rep["baseline_folds"], folds):
        expected = baseline_metrics(f["test"], settings)
        assert got["total_return"] == pytest.approx(expected["total_return"])
        assert got["max_drawdown"] == pytest.approx(expected["max_drawdown"])
    # the benchmark over the whole development series would be a different number, so
    # matching the test slice is a real constraint and not an accident of arithmetic
    whole = baseline_metrics(dev, settings)
    assert rep["baseline_folds"][0]["total_return"] != pytest.approx(whole["total_return"])


def test_the_excess_is_the_metric_over_the_benchmark(dev, settings):
    g = validate_genome({"family": "trend_momentum", "params": {}})
    rep = evaluate_folds(dev, g, settings)
    assert rep["metric_excess"] == pytest.approx(rep["metric_mean"] - rep["baseline_mean"])
    assert rep["baseline_mean"] == pytest.approx(float(np.mean(rep["baseline_values"])))


def test_the_search_never_sees_the_benchmark(dev, settings, monkeypatch):
    """The fitness, the gates and the elite must be unchanged by the benchmark's presence.

    Patching the benchmark to nonsense has to leave every number the search uses exactly
    where it was; if it does not, the benchmark is quietly steering the evolution.
    """
    g = validate_genome({"family": "rsi_reversion", "params": {}})
    before = evaluate_folds(dev, g, settings)

    monkeypatch.setattr(
        "selftrade.evaluate.baseline_metrics",
        lambda df, s, **kw: {"total_return": 99.0, "max_drawdown": 99.0, "sharpe": 99.0},
    )
    after = evaluate_folds(dev, g, settings)
    for key in ("fitness", "metric_mean", "metric_std", "passed", "gate_reason", "complexity"):
        assert after[key] == before[key], key
    assert after["fold_values"] == before["fold_values"]
    # while the benchmark itself did move, which proves the patch took effect
    assert after["baseline_folds"][0]["total_return"] == 99.0


def test_the_holdout_result_carries_the_benchmark_of_the_holdout_slice(dev, settings):
    from selftrade.data import make_synthetic, split_holdout
    from selftrade.evaluate import baseline_metrics, holdout_eval

    _, hold = split_holdout(make_synthetic(4000, seed=17), 0.2)
    g = validate_genome({"family": "trend_momentum", "params": {}})
    out = holdout_eval(hold, g, settings)
    expected = baseline_metrics(hold, settings)
    assert out["baseline"]["total_return"] == pytest.approx(expected["total_return"])
    # measured on the holdout, not on the development series it was chosen on
    assert out["baseline"]["total_return"] != pytest.approx(
        baseline_metrics(dev, settings)["total_return"]
    )
