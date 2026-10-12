"""Tests for reproducibility: what code and what data produced a run.

The ids are checked for the property that makes them useful (determinism), and every
degradation path - no git, no data file, a dirty worktree - is checked to *report*
itself rather than raise, because a run that cannot be fingerprinted must still be
written down.
"""

import hashlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from selftrade import repro
from selftrade.data import make_synthetic


def _write_csv(path, rows=3):
    lines = ["timestamp,open,high,low,close,volume"]
    for i in range(rows):
        lines.append(f"2024-01-0{i + 1}T00:00:00Z,1.0,2.0,0.5,1.5,10.0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _settings_with(csv_path, source="csv"):
    data = SimpleNamespace(source=source, csv=str(csv_path), symbol="BTC/USDT", timeframe="4h")
    return SimpleNamespace(data=data)


# ------------------------------------------------------------------ the id


def test_repro_id_is_deterministic():
    a = repro.repro_id("a" * 40, "b" * 64)
    assert a == repro.repro_id("a" * 40, "b" * 64)
    assert a is not None and len(a) == 16


def test_repro_id_changes_with_either_half():
    base = repro.repro_id("a" * 40, "b" * 64)
    assert repro.repro_id("c" * 40, "b" * 64) != base   # different code
    assert repro.repro_id("a" * 40, "d" * 64) != base   # different data


def test_repro_id_needs_both_halves():
    assert repro.repro_id(None, "b" * 64) is None
    assert repro.repro_id("a" * 40, None) is None
    assert repro.repro_id("", "") is None


# ---------------------------------------------------------------- the code


@pytest.fixture()
def git_repo(tmp_path):
    if shutil.which("git") is None:  # pragma: no cover - depends on the machine
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    env_args = ["-c", "user.email=t@example.com", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", *env_args, "commit", "-q", "-m", "first"], cwd=repo, check=True)
    return repo


def test_code_fingerprint_reads_the_commit(git_repo):
    fp = repro.code_fingerprint(git_repo)
    assert fp["available"] is True
    assert len(fp["commit"]) == 40 and fp["short"] and fp["commit"].startswith(fp["short"])
    assert fp["dirty"] is False
    assert fp["note"] is None


def test_code_fingerprint_notices_a_dirty_worktree(git_repo):
    (git_repo / "f.txt").write_text("changed\n", encoding="utf-8")
    fp = repro.code_fingerprint(git_repo)
    assert fp["available"] is True and fp["dirty"] is True


def test_code_fingerprint_without_git_says_so(tmp_path, monkeypatch):
    """A machine without git (or outside a repo) reports N/D instead of guessing."""
    monkeypatch.setattr(repro, "_git", lambda args, where: None)
    fp = repro.code_fingerprint(tmp_path)
    assert fp["available"] is False and fp["commit"] is None
    assert "git could not read" in fp["note"]


# ---------------------------------------------------------------- the data


def test_dataset_fingerprint_hashes_the_file_bytes(tmp_path):
    path = _write_csv(tmp_path / "d.csv", rows=3)
    fp = repro.dataset_fingerprint(_settings_with(path))
    assert fp["method"] == "file"
    assert fp["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert fp["bytes"] == path.stat().st_size
    assert fp["rows"] == 3                     # the header line is not a bar
    assert fp["path"].endswith("d.csv")
    assert fp["symbol"] == "BTC/USDT" and fp["timeframe"] == "4h"


def test_dataset_fingerprint_changes_when_the_file_changes(tmp_path):
    path = _write_csv(tmp_path / "d.csv", rows=3)
    first = repro.dataset_fingerprint(_settings_with(path))["sha256"]
    _write_csv(path, rows=4)
    assert repro.dataset_fingerprint(_settings_with(path))["sha256"] != first


def test_dataset_fingerprint_falls_back_to_the_frame(tmp_path):
    df = make_synthetic(300, seed=11)
    s = _settings_with(tmp_path / "missing.csv", source="synthetic")
    fp = repro.dataset_fingerprint(s, df)
    assert fp["method"] == "frame"
    assert fp["rows"] == 300
    assert fp["first_ts"] and fp["last_ts"] and fp["first_ts"] < fp["last_ts"]
    # the same prices hash to the same digest; different prices do not
    assert fp["sha256"] == repro.dataset_fingerprint(s, df)["sha256"]
    assert fp["sha256"] != repro.dataset_fingerprint(s, make_synthetic(300, seed=12))["sha256"]


def test_dataset_fingerprint_without_inputs_admits_it():
    fp = repro.dataset_fingerprint(None)
    assert fp["sha256"] is None and fp["short"] is None
    assert "available to hash" in fp["note"] and fp["method"] == "unavailable"


# ------------------------------------------------------------ the whole thing


def test_fingerprint_of_a_clean_tree_is_reproducible(tmp_path, monkeypatch):
    path = _write_csv(tmp_path / "d.csv")
    monkeypatch.setattr(
        repro, "code_fingerprint",
        lambda repo=None: {"available": True, "commit": "a" * 40, "short": "a" * 12,
                           "branch": "master", "dirty": False, "note": None},
    )
    fp = repro.fingerprint(_settings_with(path), repo=tmp_path)
    assert fp["repro_id"] == repro.repro_id("a" * 40, hashlib.sha256(path.read_bytes()).hexdigest())
    assert fp["commit_short"] == "a" * 12 and fp["branch"] == "master"
    assert fp["dataset"]["method"] == "file"
    assert fp["reproducible"] is True and fp["notes"] == []


def test_fingerprint_flags_a_dirty_tree_without_hiding_the_id(tmp_path, monkeypatch):
    path = _write_csv(tmp_path / "d.csv")
    monkeypatch.setattr(
        repro, "code_fingerprint",
        lambda repo=None: {"available": True, "commit": "a" * 40, "short": "a" * 12,
                           "branch": "master", "dirty": True, "note": None},
    )
    fp = repro.fingerprint(_settings_with(path), repo=tmp_path)
    assert fp["repro_id"] is not None          # the id is still emitted...
    assert fp["reproducible"] is False         # ...but the run is not reproducible from it
    assert any("uncommitted changes" in n for n in fp["notes"])


def test_two_fingerprints_of_the_same_inputs_agree(tmp_path, monkeypatch):
    """Same commit, same dataset -> the same id.  This is the whole point of it."""
    path = _write_csv(tmp_path / "d.csv")
    monkeypatch.setattr(
        repro, "code_fingerprint",
        lambda repo=None: {"available": True, "commit": "b" * 40, "short": "b" * 12,
                           "branch": "master", "dirty": False, "note": None},
    )
    s = _settings_with(path)
    first, second = repro.fingerprint(s, repo=tmp_path), repro.fingerprint(s, repo=tmp_path)
    assert first["repro_id"] == second["repro_id"]
    assert first["reproducible"] is True and second["reproducible"] is True


def test_fingerprint_with_no_provenance_at_all_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(
        repro, "code_fingerprint",
        lambda repo=None: {"available": False, "commit": None, "short": None,
                           "branch": None, "dirty": None, "note": "no git here"},
    )
    fp = repro.fingerprint(None, repo=tmp_path)
    assert fp["repro_id"] is None and fp["reproducible"] is False
    assert fp["code_available"] is False
    assert "no git here" in fp["notes"]
