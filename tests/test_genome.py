import numpy as np
import pytest

from selftrade.genome import (
    GenomeError,
    complexity,
    crossover_genomes,
    describe,
    genome_key,
    mutate_genome,
    sample_genome,
    validate_genome,
)
from selftrade.params import FAMILY_ORDER, FAMILY_PARAMS


def test_validate_rejects_unknown_family():
    with pytest.raises(GenomeError):
        validate_genome({"family": "nope", "params": {}})
    with pytest.raises(GenomeError):
        validate_genome("not a dict")


def test_validate_drops_unknown_params():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {"rsi_period": 14, "evil_key": 999, "rsi_buy": 30, "rsi_exit": 60},
        }
    )
    assert "evil_key" not in g["params"]
    assert set(g["params"]) == set(FAMILY_PARAMS["rsi_reversion"])


def test_validate_clamps_int_and_float():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {"rsi_period": 9999, "rsi_buy": -50, "rsi_exit": 900},
        }
    )
    specs = FAMILY_PARAMS["rsi_reversion"]
    assert g["params"]["rsi_period"] == specs["rsi_period"].high
    assert g["params"]["rsi_buy"] >= specs["rsi_buy"].low
    assert g["params"]["rsi_exit"] <= specs["rsi_exit"].high


def test_validate_fixes_ema_constraint():
    g = validate_genome(
        {
            "family": "trend_momentum",
            "params": {"ema_fast": 200, "ema_slow": 30},
        }
    )
    assert g["params"]["ema_fast"] < g["params"]["ema_slow"]


def test_validate_fixes_rsi_constraint():
    g = validate_genome(
        {"family": "rsi_reversion", "params": {"rsi_buy": 80, "rsi_exit": 40}}
    )
    assert g["params"]["rsi_buy"] < g["params"]["rsi_exit"]


def test_validate_fixes_donchian_constraint():
    g = validate_genome(
        {"family": "donchian_breakout", "params": {"entry_period": 10, "exit_period": 60}}
    )
    assert g["params"]["exit_period"] <= g["params"]["entry_period"]


def test_validate_risk_clamping():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {},
            "risk": {
                "stop_loss_pct": -5.0,
                "take_profit_pct": 99.0,
                "trailing_pct": 0.0001,
                "trade_shorts": "yes",
            },
        }
    )
    assert -0.25 <= g["risk"]["stop_loss_pct"] <= -0.008
    assert g["risk"]["take_profit_pct"] <= 0.35
    assert g["risk"]["trailing_pct"] is None or g["risk"]["trailing_pct"] >= 0.02
    assert g["risk"]["trade_shorts"] is True


def test_validate_risk_null_forms():
    g = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {},
            "risk": {"take_profit_pct": None, "trailing_pct": "null"},
        }
    )
    assert g["risk"]["take_profit_pct"] is None
    assert g["risk"]["trailing_pct"] is None


def test_validate_garbage_llm_output_is_legal():
    # An LLM might return junk; validation must still yield something legal.
    junk = {
        "family": "trend_momentum",
        "params": {"ema_fast": "abc", "ema_slow": None, "rsi_min": "high"},
        "risk": {"stop_loss_pct": "tight"},
    }
    g = validate_genome(junk)
    assert g["params"]["ema_fast"] < g["params"]["ema_slow"]
    assert isinstance(g["risk"]["stop_loss_pct"], float)


def test_sample_genome_is_valid_all_families():
    rng = np.random.default_rng(0)
    for fam in FAMILY_ORDER:
        for _ in range(20):
            g = sample_genome(fam, rng)
            assert g == validate_genome(g)


def test_sample_genome_deterministic():
    a = sample_genome("rsi_reversion", np.random.default_rng(5))
    b = sample_genome("rsi_reversion", np.random.default_rng(5))
    assert a == b


def test_mutate_produces_valid_and_deterministic():
    rng = np.random.default_rng(3)
    base = sample_genome("trend_momentum", rng)
    m1 = mutate_genome(base, np.random.default_rng(99), 0.5)
    m2 = mutate_genome(base, np.random.default_rng(99), 0.5)
    assert m1 == m2
    assert m1 == validate_genome(m1)


def test_mutate_rate_zero_is_identity():
    rng = np.random.default_rng(4)
    base = sample_genome("donchian_breakout", rng)
    out = mutate_genome(base, np.random.default_rng(1), 0.0)
    assert out == base


def test_crossover_valid():
    rng = np.random.default_rng(7)
    a = sample_genome("bollinger_reversion", rng)
    b = sample_genome("donchian_breakout", rng)
    child = crossover_genomes(a, b, rng)
    assert child == validate_genome(child)
    assert child["family"] in ("bollinger_reversion", "donchian_breakout")


def test_crossover_same_family_keeps_family():
    rng = np.random.default_rng(8)
    a = sample_genome("rsi_reversion", rng)
    b = sample_genome("rsi_reversion", rng)
    child = crossover_genomes(a, b, rng)
    assert child["family"] == "rsi_reversion"


def test_complexity_defaults_zero():
    g = validate_genome({"family": "rsi_reversion", "params": {}, "risk": {}})
    assert complexity(g) >= 0


def test_complexity_counts_risk_rules():
    base = validate_genome(
        {
            "family": "rsi_reversion",
            "params": {k: v.default for k, v in FAMILY_PARAMS["rsi_reversion"].items()},
            "risk": {"stop_loss_pct": -0.05, "take_profit_pct": None, "trailing_pct": None, "trade_shorts": False},
        }
    )
    assert complexity(base) == 0
    with_risk = validate_genome(
        {
            "family": "rsi_reversion",
            "params": base["params"],
            "risk": {"stop_loss_pct": -0.05, "take_profit_pct": 0.1, "trailing_pct": 0.05, "trade_shorts": True},
        }
    )
    assert complexity(with_risk) == 3


def test_genome_key_stable_and_distinct():
    rng = np.random.default_rng(11)
    a = sample_genome("rsi_reversion", rng)
    b = validate_genome(a)
    assert genome_key(a) == genome_key(b)
    c = mutate_genome(a, np.random.default_rng(12), 1.0)
    assert genome_key(a) != genome_key(c)


def test_describe_contains_family():
    g = sample_genome("rsi_reversion", np.random.default_rng(1))
    assert "rsi_reversion" in describe(g)
