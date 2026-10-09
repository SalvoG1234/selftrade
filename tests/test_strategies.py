import numpy as np
import pandas as pd
import pytest

from selftrade.data import make_synthetic
from selftrade.genome import sample_genome, validate_genome
from selftrade.params import FAMILY_ORDER, FAMILY_PARAMS
from selftrade.strategies import SIGNAL_COLUMNS, generate_signals, get_strategy


@pytest.fixture(scope="module")
def df():
    return make_synthetic(1500, seed=21)


def test_registry_covers_all_families():
    for fam in FAMILY_ORDER:
        assert get_strategy(fam).family == fam


def test_unknown_family_raises():
    with pytest.raises(KeyError):
        get_strategy("nope")


def default_params(fam):
    return {k: v.default for k, v in FAMILY_PARAMS[fam].items()}


def test_signal_frame_shape(df):
    for fam in FAMILY_ORDER:
        g = validate_genome({"family": fam, "params": default_params(fam)})
        sig = generate_signals(g, df)
        assert list(sig.columns) == SIGNAL_COLUMNS
        assert len(sig) == len(df)
        assert sig.dtypes.eq(bool).all()


def test_signals_are_causal(df):
    """Shifting the future must not change past signals for any family."""
    cut = 900
    for fam in FAMILY_ORDER:
        g = validate_genome({"family": fam, "params": default_params(fam)})
        full = generate_signals(g, df)
        tampered = df.copy()
        tampered.iloc[cut:] = tampered.iloc[cut:] * 1.9 + 25.0
        after = generate_signals(g, tampered)
        pd.testing.assert_frame_equal(full.iloc[:cut], after.iloc[:cut])


def test_all_families_produce_some_signal(df):
    for fam in FAMILY_ORDER:
        g = validate_genome({"family": fam, "params": default_params(fam)})
        sig = generate_signals(g, df)
        assert sig.to_numpy().sum() > 0, f"{fam} produced no signals"


def test_sampled_genomes_are_safe(df):
    rng = np.random.default_rng(2)
    for fam in FAMILY_ORDER:
        for _ in range(3):
            g = sample_genome(fam, rng)
            sig = generate_signals(g, df)
            assert len(sig) == len(df)
            assert not sig.isna().any().any()


def test_entry_and_exit_are_opposites_for_trend(df):
    g = validate_genome({"family": "trend_momentum", "params": default_params("trend_momentum")})
    sig = generate_signals(g, df)
    # Trend family: long_exit should be the complement of long_entry regime.
    assert (sig["long_entry"] & sig["long_exit"]).sum() == 0
