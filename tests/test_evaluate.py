import numpy as np
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
