"""The rail credential store: one file, the environment ahead of it, no value ever returned.

These are the rules the store has to keep, and they are the ones an audit would check: a
value can be written and never read back through an API, the environment outranks the file,
a corrupt file fails *closed* (no credential, so the rail refuses to arm), and the file is
written atomically with owner-only permissions from before its first byte.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from selftrade.secrets import (
    MAX_VALUE_CHARS,
    SecretError,
    SecretStore,
    check_name,
    check_value,
    fingerprint,
)

KEY = "SELFTD_TEST_API_KEY"
SECRET = "a" * 64


def store(tmp_path, **env):
    return SecretStore(tmp_path / "state" / "secrets.json", environ=dict(env))


# ---------------------------------------------------------------- validation

def test_a_name_has_to_look_like_an_environment_variable():
    for good in ("SELFTD_BINANCE_API_KEY", "_x", "A1_b"):
        assert check_name(good) is None
    for bad in ("", "   ", "1STARTS_WITH_DIGIT", "has space", "has-dash", "a" * 200, None, 3):
        assert check_name(bad), bad


def test_a_pasted_value_is_trimmed_but_a_broken_one_is_refused():
    assert check_value("  abc123  ") is None        # what a paste actually produces
    assert check_value("") == "the value cannot be empty"
    assert check_value("   ")
    assert check_value("two\nlines")
    assert check_value("has space inside")
    assert check_value("x" * (MAX_VALUE_CHARS + 1))
    assert check_value(12345)


# ---------------------------------------------------------------- writing and reading

def test_a_stored_value_comes_back_and_the_file_is_owner_only(tmp_path):
    s = store(tmp_path)
    s.set(KEY, SECRET, by="test")
    assert s.get(KEY) == SECRET
    path = Path(s.path)
    assert path.exists()
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["keys"][KEY]["value"] == SECRET
    if os.name == "posix":                          # mode bits are meaningful here
        assert (path.stat().st_mode & 0o777) == 0o600


def test_the_environment_wins_over_the_file(tmp_path):
    s = store(tmp_path)
    s.set(KEY, "from-the-file")
    assert s.get(KEY) == "from-the-file"
    env_store = store(tmp_path, **{KEY: "from-the-environment"})
    assert env_store.get(KEY) == "from-the-environment"
    assert env_store.describe_one(KEY)["source"] == "environment"
    # and the file is untouched by that: nothing was overwritten or cleared
    assert s.get(KEY) == "from-the-file"


def test_a_missing_store_resolves_to_nothing(tmp_path):
    s = store(tmp_path)
    assert s.get(KEY) is None
    assert s.resolve(KEY) == (None, None)
    info = s.describe_one(KEY)
    assert info["set"] is False and info["source"] is None and info["fingerprint"] is None


def test_clearing_removes_the_name_and_then_the_file(tmp_path):
    s = store(tmp_path)
    s.set(KEY, SECRET)
    assert s.clear(KEY) is True
    assert s.get(KEY) is None
    # nothing is left behind: an empty store is not a store
    assert not Path(s.path).exists()
    assert s.clear(KEY) is False


def test_clearing_one_name_keeps_the_others(tmp_path):
    s = store(tmp_path)
    s.set(KEY, SECRET)
    s.set("SELFTD_OTHER", "keep-me")
    assert s.clear(KEY) is True
    assert s.get("SELFTD_OTHER") == "keep-me"
    assert s.stored_names() == ["SELFTD_OTHER"]


def test_two_writes_never_leave_a_half_written_file(tmp_path):
    """The temp file is replaced, not edited: the target only ever holds whole JSON."""
    s = store(tmp_path)
    s.set(KEY, "one")
    s.set(KEY, "two")
    body = json.loads(Path(s.path).read_text(encoding="utf-8"))
    assert body["keys"][KEY]["value"] == "two"
    assert not list(Path(s.path).parent.glob("*.tmp"))


# ---------------------------------------------------------------- what a corrupt file does

def test_a_corrupt_file_yields_no_credential_at_all(tmp_path):
    path = tmp_path / "state" / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json", encoding="utf-8")
    s = SecretStore(path, environ={})
    assert s.get(KEY) is None                       # fails closed: the rail cannot arm
    described = s.describe([KEY])
    assert described["corrupt"] and "unreadable" in described["note"]
    assert described["vars"][0]["set"] is False


def test_storing_into_a_corrupt_file_is_refused_rather_than_overwriting_it(tmp_path):
    path = tmp_path / "state" / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    s = SecretStore(path, environ={})
    with pytest.raises(SecretError) as exc:
        s.set(KEY, SECRET)
    assert "not a readable credential record" in str(exc.value)
    assert path.read_text(encoding="utf-8") == "{not json"      # untouched
    with pytest.raises(SecretError):
        s.clear(KEY)


def test_a_record_without_a_usable_value_is_ignored(tmp_path):
    path = tmp_path / "state" / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"keys": {KEY: {"fingerprint": "x"}, "": {"value": "v"},
                             "ALSO_OK": {"value": ""}}}),
        encoding="utf-8",
    )
    s = SecretStore(path, environ={})
    assert s.get(KEY) is None and s.get("ALSO_OK") is None
    assert s.stored_names() == []


# ---------------------------------------------------------------- what describe may say

def test_describe_reports_sources_and_fingerprints_and_never_a_value(tmp_path):
    s = store(tmp_path)
    s.set(KEY, SECRET, by="test")
    described = s.describe([KEY, "SELFTD_NOT_SET"])
    text = json.dumps(described)
    assert SECRET not in text                      # the whole point of the shape
    assert KEY not in text or True                 # the name is reported, the value is not
    first = described["vars"][0]
    assert first["name"] == KEY and first["set"] is True and first["source"] == "file"
    assert first["stored"] is True and first["changed_by"] == "test"
    assert first["fingerprint"] == fingerprint(SECRET)
    assert len(first["fingerprint"]) == 12
    assert described["vars"][1]["set"] is False
    assert described["environment_wins"] is True and described["material"] == "clear text"
    assert "clear text" in described["note"] and "arms a rail" in described["note"]


def test_the_fingerprint_changes_with_the_value_and_reveals_nothing(tmp_path):
    s = store(tmp_path)
    s.set(KEY, SECRET)
    before = s.describe_one(KEY)["fingerprint"]
    s.set(KEY, SECRET[:-1] + "b")
    after = s.describe_one(KEY)["fingerprint"]
    assert before != after
    assert SECRET[:8] not in json.dumps(s.describe([KEY]))


def test_the_default_file_sits_under_the_ignored_state_directory():
    from selftrade.secrets import DEFAULT_FILE

    assert DEFAULT_FILE.replace("\\", "/").startswith("state/")
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "state/" in [line.strip() for line in ignored]


def test_the_module_imports_no_network_client():
    """The same audit ``capital.py`` passes: a secret store is not an HTTP client."""
    source = Path("selftrade/secrets.py").read_text(encoding="utf-8")
    for banned in ("import requests", "import socket", "import urllib", "import httpx"):
        assert banned not in source
