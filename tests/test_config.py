import pytest

from selftrade.config import (
    ConfigError,
    Settings,
    apply_override,
    as_dict,
    load_settings,
)


def write_cfg(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_load():
    s = load_settings(None)
    assert isinstance(s, Settings)
    assert s.data.source == "csv"
    assert s.evaluation.metric == "sharpe"
    assert s.mt5.require_demo is True


def test_reference_config_roundtrips(tmp_path):
    import shutil

    src = "config.yaml"
    dst = tmp_path / "config.yaml"
    shutil.copy(src, dst)
    s = load_settings(dst)
    assert s.project == "selftrade"
    assert s.mt5.password_env == "SELFTD_MT5_PASSWORD"
    assert isinstance(s.rea.targets, list)


def test_unknown_key_stops_execution(tmp_path):
    p = write_cfg(tmp_path, "backtest:\n  fee_bp: 3\n")
    with pytest.raises(ConfigError) as exc:
        load_settings(p)
    assert "fee_bp" in str(exc.value)


def test_unknown_top_level_key(tmp_path):
    p = write_cfg(tmp_path, "projct: oops\n")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_override_types():
    s = load_settings(None)
    apply_override(s, "backtest.fee_bps=1.5")
    assert s.backtest.fee_bps == 1.5
    apply_override(s, "evaluation.folds=3")
    assert s.evaluation.folds == 3
    apply_override(s, "backtest.allow_short=true")
    assert s.backtest.allow_short is True
    apply_override(s, "data.symbol=EURUSD")
    assert s.data.symbol == "EURUSD"
    apply_override(s, "mt5.allowed_symbols=[\"EURUSD\",\"GBPUSD\"]")
    assert s.mt5.allowed_symbols == ["EURUSD", "GBPUSD"]


def test_override_unknown_key():
    s = load_settings(None)
    with pytest.raises(ConfigError):
        apply_override(s, "backtest.does_not_exist=1")
    with pytest.raises(ConfigError):
        apply_override(s, "nope.key=1")


def test_override_bad_syntax():
    s = load_settings(None)
    with pytest.raises(ConfigError):
        apply_override(s, "backtest.fee_bps")


def test_override_bad_value():
    s = load_settings(None)
    with pytest.raises(ConfigError):
        apply_override(s, "evaluation.folds=abc")


def test_range_validation(tmp_path):
    p = write_cfg(tmp_path, "backtest:\n  position_size: 2.0\n")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_enum_validation(tmp_path):
    p = write_cfg(tmp_path, "evaluation:\n  metric: made_up\n")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_dashboard_must_be_loopback(tmp_path):
    p = write_cfg(tmp_path, "dashboard:\n  host: 0.0.0.0\n")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_risk_gate_defaults_and_overrides():
    s = load_settings(None)
    assert s.risk_gate.enabled is True
    assert s.risk_gate.max_positions == 1
    assert s.risk_gate.require_stop is True
    assert s.risk_gate.allowed_symbols == []
    apply_override(s, "risk_gate.max_positions=3")
    apply_override(s, "risk_gate.min_seconds_between_orders=30")
    apply_override(s, "risk_gate.allowed_symbols=[\"BTC/USDT\"]")
    assert s.risk_gate.max_positions == 3
    assert s.risk_gate.min_seconds_between_orders == 30.0
    assert s.risk_gate.allowed_symbols == ["BTC/USDT"]


def test_risk_gate_does_not_carry_the_sealed_limits():
    """The sealed layer must stay unreachable from config.yaml."""
    from selftrade.risk_rules import LIMITS

    s = load_settings(None)
    keys = set(as_dict(s)["risk_gate"])
    assert not keys & set(LIMITS.as_dict())
    with pytest.raises(ConfigError):
        apply_override(s, "risk_gate.MAX_DRAWDOWN_PCT=99")


def test_risk_gate_range_validation(tmp_path):
    for bad in (
        "risk_gate:\n  risk_pct: 2.0\n",
        "risk_gate:\n  max_leverage: 0\n",
        "risk_gate:\n  max_positions: -1\n",
        "risk_gate:\n  min_seconds_between_orders: -5\n",
    ):
        with pytest.raises(ConfigError):
            load_settings(write_cfg(tmp_path, bad))


def test_reference_config_has_the_risk_gate_section():
    s = load_settings("config.yaml")
    assert s.risk_gate.max_positions >= 1
    assert 0 < s.risk_gate.risk_pct <= 1


def test_as_dict_shape():
    s = load_settings(None)
    d = as_dict(s)
    assert d["data"]["timeframe"] == "1h"
    assert d["memory"]["db"].endswith("selftrade.db")
