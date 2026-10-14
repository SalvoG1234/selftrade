"""Configuration: safe defaults, named secrets, and problems that are refused."""

import json

import pytest

from socialmanager import config


def test_defaults_are_safe_without_a_file(tmp_path):
    settings = config.load(tmp_path / "nope.yaml")
    assert settings.existed is False
    assert str(settings.mode()) == "draft"          # mode A by default
    assert settings.autonomy.kill_switch is False
    assert settings.x.enabled is False              # no connector runs unasked
    assert settings.x.dry_run is True
    assert settings.instagram.enabled is False
    assert settings.instagram.dry_run is True
    assert settings.autonomy.allow_replies is False
    assert settings.autonomy.allow_issue_proposals is False
    assert settings.notify.enabled is False
    assert settings.editorial.languages == ["it", "en"]
    assert config.validate(settings) == []


def test_a_file_is_read_and_unknown_keys_are_reported(tmp_path):
    path = tmp_path / "social.yaml"
    path.write_text(
        "project:\n  brand: SelfTrade\n"
        "autonomy:\n  mode: controlled\n"
        "editorial:\n  min_gap_minutes: 30\n"
        "connectors:\n  x:\n    enabled: true\n",   # not a section of Settings
        encoding="utf-8",
    )
    settings = config.load(path)
    assert settings.existed is True
    assert str(settings.mode()) == "controlled"
    assert settings.editorial.min_gap_minutes == 30
    assert "connectors" in settings.unknown_keys
    assert any("unknown configuration key" in problem for problem in config.validate(settings))


def test_overrides_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_OVERRIDES", "autonomy.mode=advanced,dashboard.port=9001")
    settings = config.load(tmp_path / "nope.yaml")
    assert str(settings.mode()) == "advanced"
    assert settings.dashboard.port == 9001
    assert "autonomy.mode='advanced'" in settings.overrides


def test_an_override_for_a_missing_key_is_reported(tmp_path):
    settings = config.load(tmp_path / "nope.yaml", overrides="nope.nothing=1")
    assert "nope.nothing" in settings.unknown_keys


def test_a_token_pasted_into_the_config_is_refused(tmp_path):
    path = tmp_path / "social.yaml"
    path.write_text("github:\n  token_env: ghp_" + "a" * 30 + "\n", encoding="utf-8")
    settings = config.load(path)
    problems = config.validate(settings)
    assert any("looks like a token" in problem for problem in problems)


def test_a_non_loopback_bind_without_auth_is_refused(tmp_path):
    settings = config.load(tmp_path / "nope.yaml")
    settings.dashboard.host = "0.0.0.0"
    settings.dashboard.auth = False
    assert any("non-loopback bind" in problem for problem in config.validate(settings))
    settings.dashboard.auth = True
    assert any("needs a password" in problem for problem in config.validate(settings))


def test_unsupported_language_and_mode_are_reported(tmp_path):
    settings = config.load(tmp_path / "nope.yaml")
    settings.editorial.languages = ["it", "de"]
    settings.autonomy.mode = "yolo"
    problems = config.validate(settings)
    assert any("not supported" in problem for problem in problems)
    assert any("autonomy.mode" in problem for problem in problems)


def test_postgres_is_reported_as_unavailable_in_this_build(tmp_path):
    settings = config.load(tmp_path / "nope.yaml")
    settings.database.url = "postgresql://user@host/db"
    assert settings.is_postgres() is True
    assert any("SQLite only" in problem for problem in config.validate(settings))


def test_instagram_needs_an_https_media_base(tmp_path):
    settings = config.load(tmp_path / "nope.yaml")
    settings.instagram.public_media_base_url = "http://localhost:8000/media"
    assert any("must be https" in problem for problem in config.validate(settings))


def test_secret_names_are_names_and_presence_is_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_X_ACCESS_TOKEN", "x" * 40)
    settings = config.load(tmp_path / "nope.yaml")
    dumped = settings.to_dict()
    payload = json.dumps(dumped)
    assert dumped["env_present"]["SOCIAL_X_ACCESS_TOKEN"] is True
    assert dumped["env_present"]["SOCIAL_IG_ACCESS_TOKEN"] is False
    assert "x" * 40 not in payload             # only the name travels
    assert "SOCIAL_X_ACCESS_TOKEN" in payload


def test_dumps_is_the_effective_configuration(tmp_path):
    settings = config.load(tmp_path / "nope.yaml", overrides="editorial.min_gap_minutes=45")
    text = config.dumps(settings)
    assert "min_gap_minutes: 45" in text
    assert "overrides" not in text             # internal bookkeeping is not configuration


def test_a_broken_file_is_an_error_not_a_silent_default(tmp_path):
    path = tmp_path / "social.yaml"
    path.write_text("project: [this is not a mapping]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(path)


def test_instagram_graph_version_is_normalised(tmp_path):
    settings = config.load(tmp_path / "nope.yaml")
    settings.instagram.graph_version = "25.0"
    config._coerce(settings)
    assert settings.instagram.graph_version == "v25.0"
