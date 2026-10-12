"""Tests for the dashboard credential: the stored hash, its refusals and its recovery.

No server here - this is the file-level layer (salt, hash, atomic write, corrupt file).
The login and the change endpoint are exercised over real HTTP in ``test_dashboard``.
"""

import json
import os
import stat

import pytest

from selftrade.credentials import (
    ALGORITHM,
    DEFAULT_PASSWORD,
    ITERATIONS,
    CredentialError,
    CredentialStore,
    check_new_password,
)


def test_a_fresh_store_accepts_the_bootstrap_password_and_says_so(tmp_path):
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    assert store.exists() is False
    assert store.verify(DEFAULT_PASSWORD) is True
    assert store.verify("anything-else") is False
    assert store.is_default() is True
    assert store.describe()["source"] == "default"
    assert not path.exists()  # reading the state must never create it


def test_setting_a_password_stores_a_hash_and_never_the_password(tmp_path):
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    secret = "correct horse battery staple"
    store.set(secret, by="test")

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    record = json.loads(raw)
    assert record["algorithm"] == ALGORITHM and record["iterations"] == ITERATIONS
    assert len(bytes.fromhex(record["salt"])) == 16
    assert len(bytes.fromhex(record["hash"])) == 32
    assert record["changed_by"] == "test"
    assert record["changed_at"]

    assert store.verify(secret) is True
    assert store.verify(DEFAULT_PASSWORD) is False
    assert store.is_default() is False
    assert store.describe()["source"] == "stored"


def test_two_stores_salted_with_the_same_password_differ(tmp_path):
    a, b = CredentialStore(tmp_path / "a.json"), CredentialStore(tmp_path / "b.json")
    a.set("same-password")
    b.set("same-password")
    ra = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    rb = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert ra["salt"] != rb["salt"]
    assert ra["hash"] != rb["hash"]  # same password, different bytes on disk
    assert a.verify("same-password") and b.verify("same-password")


def test_storing_the_bootstrap_password_again_is_still_reported_as_the_default(tmp_path):
    """What the banner reports is the *effective* password, not the history of it."""
    store = CredentialStore(tmp_path / "auth.json")
    store.set(DEFAULT_PASSWORD)
    assert store.verify(DEFAULT_PASSWORD) is True
    assert store.is_default() is True


def test_a_password_that_fails_the_rule_writes_nothing(tmp_path):
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    for bad in ("", "   ", "12", None, 42):
        with pytest.raises(CredentialError):
            store.set(bad)
        assert not path.exists(), f"{bad!r} must not have written a file"
    assert "cannot be empty" in (check_new_password("") or "")
    assert "at least 4 characters" in (check_new_password("12") or "")
    assert check_new_password("12", min_length=2) is None
    assert "current one" in (check_new_password("abcd", current="abcd") or "")
    assert check_new_password("abcd") is None


def test_the_min_length_is_a_parameter_not_a_constant(tmp_path):
    store = CredentialStore(tmp_path / "auth.json")
    with pytest.raises(CredentialError) as exc:
        store.set("abcd", min_length=8)
    assert "at least 8" in str(exc.value)
    assert not store.exists()
    store.set("abcdefgh", min_length=8)
    assert store.verify("abcdefgh") is True


def test_the_file_is_owner_only_and_replaced_without_leaving_a_temp_file(tmp_path):
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    store.set("first-password")
    store.set("second-password")
    assert store.verify("second-password") is True
    assert store.verify("first-password") is False
    assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]  # no .tmp behind
    if os.name == "posix":  # Windows has no POSIX mode bits to assert
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_corrupt_store_locks_the_login_instead_of_opening_it(tmp_path):
    """Fail closed: a file someone edited is not a reason to accept the bootstrap password."""
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    store.set("a-real-password")

    path.write_text("{ this is not json", encoding="utf-8")
    assert store.verify("a-real-password") is False
    assert store.verify(DEFAULT_PASSWORD) is False  # NOT the bootstrap fallback
    assert store.is_default() is False
    assert store.describe()["source"] == "corrupt"
    assert store.describe()["changeable"] is False
    assert "reset" in store.describe()["note"]  # and it says how to recover

    # a record that parses but carries no salt/hash is corrupt too
    path.write_text(json.dumps({"salt": "", "hash": ""}), encoding="utf-8")
    assert store.verify(DEFAULT_PASSWORD) is False
    assert store.describe()["source"] == "corrupt"


def test_deleting_the_file_is_the_documented_way_back_to_the_bootstrap_password(tmp_path):
    """The trust boundary, pinned: whoever can write this file owns the dashboard."""
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    store.set("a-real-password")
    assert store.verify(DEFAULT_PASSWORD) is False

    assert store.reset() is True
    assert store.reset() is False  # idempotent
    assert store.verify(DEFAULT_PASSWORD) is True
    assert store.is_default() is True
    assert store.describe()["source"] == "default"
    assert not path.exists()


def test_a_change_on_disk_cannot_go_stale_behind_the_default_cache(tmp_path):
    """``is_default`` caches by the file's own bytes, so the next read sees the change."""
    store = CredentialStore(tmp_path / "auth.json")
    assert store.is_default() is True  # fills the cache for a missing file
    store.set("no-longer-the-default")
    assert store.is_default() is False
    assert store.verify("no-longer-the-default") is True
    store.reset()
    assert store.is_default() is True


def test_verify_never_raises_and_never_matches_on_a_weird_input(tmp_path):
    store = CredentialStore(tmp_path / "auth.json")
    store.set("a-real-password")
    for weird in ("", "x" * 10_000, "a-real-password ", "A-REAL-PASSWORD", None, 0, [], {"a": 1}):
        assert store.verify(weird) is False, weird


def test_describe_publishes_the_source_and_never_the_record(tmp_path):
    path = tmp_path / "auth.json"
    store = CredentialStore(path)
    store.set("a-real-password")
    described = store.describe()
    record = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(described)
    assert record["hash"] not in text
    assert record["salt"] not in text
    assert "a-real-password" not in text
    assert set(described) == {
        "file", "exists", "source", "is_default", "changeable", "algorithm", "iterations",
        "changed_at", "changed_by", "note",
    }
