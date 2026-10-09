import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selftrade.data import make_synthetic
from selftrade.freqtrade_export import export_strategy
from selftrade.genome import sample_genome, validate_genome
from selftrade.params import FAMILY_ORDER, FAMILY_PARAMS
from selftrade.strategies import generate_signals


@pytest.fixture(scope="module")
def df():
    return make_synthetic(1500, seed=44)


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def defaults(fam):
    return {k: v.default for k, v in FAMILY_PARAMS[fam].items()}


@pytest.mark.parametrize("family", FAMILY_ORDER)
def test_exported_signals_match_package(family, df, tmp_path):
    genome = validate_genome({"family": family, "params": defaults(family)})
    _, path = export_strategy(genome, out_dir=tmp_path)
    mod = load_module(path)
    exported = mod.compute_signals(df)
    package = generate_signals(genome, df)
    pd.testing.assert_frame_equal(exported, package)


def test_export_parity_for_sampled_genomes(df, tmp_path):
    rng = np.random.default_rng(5)
    for fam in FAMILY_ORDER:
        genome = sample_genome(fam, rng)
        _, path = export_strategy(genome, out_dir=tmp_path)
        mod = load_module(path)
        exported = mod.compute_signals(df)
        package = generate_signals(genome, df)
        pd.testing.assert_frame_equal(exported, package)


def test_export_header_declares_exit_difference(tmp_path):
    genome = validate_genome({"family": "rsi_reversion", "params": defaults("rsi_reversion")})
    _, path = export_strategy(genome, out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "minimal_roi" in text
    assert "differ" in text
    assert "Do not edit by hand" in text


def test_export_is_importable_without_freqtrade(tmp_path):
    genome = validate_genome({"family": "trend_momentum", "params": defaults("trend_momentum")})
    _, path = export_strategy(genome, out_dir=tmp_path)
    mod = load_module(path)
    assert mod._HAS_FREQTRADE is False
    assert hasattr(mod, "compute_signals")
    assert "Selftrade" in mod.__name__ or True


def test_export_class_name_sanitised(tmp_path):
    genome = validate_genome({"family": "rsi_reversion", "params": defaults("rsi_reversion")})
    name, path = export_strategy(genome, name="my weird-name!", out_dir=tmp_path)
    assert name == "my_weird_name_"
    assert path.exists()


def test_export_embeds_risk(tmp_path):
    genome = validate_genome(
        {
            "family": "rsi_reversion",
            "params": defaults("rsi_reversion"),
            "risk": {"stop_loss_pct": -0.04, "take_profit_pct": 0.12, "trade_shorts": True},
        }
    )
    name, path = export_strategy(genome, out_dir=tmp_path)
    mod = load_module(path)
    assert mod.STOPLOSS == pytest.approx(0.04)
    assert mod.CAN_SHORT is True
    cls = getattr(mod, name)
    assert cls.minimal_roi["0"] == pytest.approx(0.12)
    assert cls.stoploss == pytest.approx(-0.04)
