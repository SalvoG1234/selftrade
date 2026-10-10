"""Tests for the sealed risk layer: frozen, pinned, and unreachable by design."""

import dataclasses
import json

import pytest

from selftrade.config import ConfigError, apply_override, load_settings
from selftrade.genome import validate_genome
from selftrade.risk_rules import (
    LIMITS,
    SEALED_DIGEST,
    RiskLimits,
    RiskRulesError,
    assert_no_levers,
    assert_pinned,
    digest_of,
    effective_position_size,
    is_sealed,
    seal_path,
    sealed_block,
    verify_seal,
    write_seal,
)


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.session_file = str(tmp_path / "state" / "session.json")
    return s


def test_limits_are_frozen():
    assert LIMITS.MODEL_CAN_OVERRIDE is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        LIMITS.MAX_DRAWDOWN_PCT = 99.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        LIMITS.MODEL_CAN_OVERRIDE = True  # type: ignore[misc]


def test_digest_is_pinned_and_tamper_evident():
    assert digest_of() == SEALED_DIGEST
    assert assert_pinned() == SEALED_DIGEST
    edited = RiskLimits(MAX_DRAWDOWN_PCT=99.0)
    assert digest_of(edited) != SEALED_DIGEST
    with pytest.raises(RiskRulesError) as exc:
        assert_pinned(SEALED_DIGEST, edited)
    assert "sealed risk limits were edited" in str(exc.value)


def test_no_config_key_can_reach_the_limits(settings):
    for key in ("risk_rules.MAX_DRAWDOWN_PCT=99", "risk_rules.HARD_STOP_LOSS_PCT=99"):
        with pytest.raises(ConfigError):
            apply_override(settings, key)


def test_no_genome_can_widen_the_limits():
    g = validate_genome(
        {
            "family": "trend_momentum",
            "params": {"ema_fast": 10, "ema_slow": 30},
            "risk": {
                "stop_loss_pct": -0.04,
                "MAX_DRAWDOWN_PCT": 99.0,
                "MODEL_CAN_OVERRIDE": True,
            },
        }
    )
    assert "MAX_DRAWDOWN_PCT" not in g["risk"]
    assert "MODEL_CAN_OVERRIDE" not in g["risk"]


def test_environment_cannot_widen_them(monkeypatch):
    monkeypatch.setenv("SELFTD_ALLOW_RISK_OVERRIDE", "1")
    with pytest.raises(RiskRulesError) as exc:
        is_sealed()
    assert "does not provide a way" in str(exc.value)
    monkeypatch.delenv("SELFTD_ALLOW_RISK_OVERRIDE")
    assert is_sealed() is True


def test_open_override_lever_is_refused():
    with pytest.raises(RiskRulesError):
        assert_no_levers(RiskLimits(MODEL_CAN_OVERRIDE=True))
    with pytest.raises(RiskRulesError):
        assert_no_levers(RiskLimits(HARD_STOP_LOSS_PCT=0.0))


def test_seal_lifecycle(settings, tmp_path):
    path = seal_path(settings)
    assert path == tmp_path / "state" / "risk_rules.seal"
    assert not path.exists()

    first = verify_seal(settings)
    assert first["ok"] is True and "sealed now" in first["reason"]
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["digest"] == SEALED_DIGEST

    assert verify_seal(settings)["ok"] is True

    # tampering with the sealed file is detected
    payload["digest"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    broken = verify_seal(settings)
    assert broken["ok"] is False
    assert "changed after they were sealed" in broken["reason"]

    write_seal(settings)
    assert verify_seal(settings)["ok"] is True


def test_corrupt_seal_file_refuses(settings):
    path = seal_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    info = verify_seal(settings)
    assert info["ok"] is False
    assert "unreadable or corrupt" in info["reason"]


def test_effective_position_size_uses_the_tighter_value(settings):
    settings.backtest.position_size = 0.95
    assert effective_position_size(settings) == pytest.approx(LIMITS.MAX_POSITION_PCT / 100.0)
    settings.backtest.position_size = 0.02
    assert effective_position_size(settings) == pytest.approx(0.02)


def test_sealed_block_shows_every_limit_and_the_hash():
    block = sealed_block()
    for key in LIMITS.as_dict():
        assert key in block
    assert "SHA-256" in block
    assert SEALED_DIGEST[:32] in block
