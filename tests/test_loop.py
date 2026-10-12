"""Tests for ``selftrade.loop``: the runner that makes the loop controls mean something.

A *fake* loop stands in for ``evolve --session`` (see ``tests/fake_loop.py``): the real
one takes minutes per generation, and what these tests are about is the plumbing - the
pid, the refusal, the log, and whether ``pause``/``resume``/``stop`` reach a process.
"""

import os
import subprocess
import sys
import time

import pytest

from selftrade.config import load_settings
from selftrade.loop import LoopRunner, loop_argv, loop_log_path, pid_alive
from selftrade.session import Session

from tests.fake_loop import fake_argv


def _settings(tmp_path):
    s = load_settings("config.yaml")
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.session_file = str(tmp_path / "state" / "session.json")
    return s


def _wait(predicate, timeout=15.0, interval=0.05):
    """Poll until ``predicate`` holds; return the last value either way."""
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


@pytest.fixture()
def runner(tmp_path):
    s = _settings(tmp_path)
    r = LoopRunner(s, argv_builder=lambda n: fake_argv(s.session_file, n))
    yield r, s
    child = r._child
    if child is not None and child.poll() is None:  # never leave a child behind
        Session(s.session_file).request("stop")
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - the fake always stops
            child.kill()
    r.close()


# ---------------------------------------------------------------- liveness probe


def test_pid_alive_on_this_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_on_nothing():
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive("") is False
    assert pid_alive("not-a-pid") is None


def test_pid_alive_on_a_finished_child():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert pid_alive(child.pid) is False


# ----------------------------------------------------------------------- argv


def test_loop_argv_is_the_tracked_loop():
    argv = loop_argv(4, config_path="cfg.yaml")
    assert argv == [
        sys.executable, "-u", "-m", "selftrade", "--config", "cfg.yaml",
        "evolve", "--session", "--generations", "4",
    ]
    assert loop_argv(2) == [
        sys.executable, "-u", "-m", "selftrade", "evolve", "--session", "--generations", "2",
    ]


def test_log_sits_next_to_the_session_file(tmp_path):
    s = _settings(tmp_path)
    assert loop_log_path(s) == tmp_path / "state" / "loop.log"
    assert LoopRunner(s).log_path == tmp_path / "state" / "loop.log"


# ------------------------------------------------------------------- starting


def test_start_runs_the_loop(runner, tmp_path):
    r, s = runner
    log_path = tmp_path / "state" / "loop.log"
    assert r.log_path == log_path
    code, out = r.start(3)
    assert code == 200 and out["ok"] is True
    loop = out["loop"]
    assert loop["pid"] == r._child.pid
    assert loop["running"] is True and loop["owned"] is True
    assert loop["generations_total"] == 3
    assert out["command"].startswith(sys.executable)
    # the session file is written *before* the child says anything, with the child's pid
    state = Session(s.session_file).read()
    assert state["status"] == "running" and state["pid"] == r._child.pid
    # and the child's own output went to the log, not to the void
    assert _wait(lambda: "fake loop: pid" in log_path.read_text(encoding="utf-8"))
    log = log_path.read_text(encoding="utf-8")
    assert "=== loop started" in log and "| 3 generations |" in log


def test_start_refuses_while_one_is_running(runner):
    r, s = runner
    assert r.start(20)[0] == 200
    code, out = r.start(20)
    assert code == 409 and out["ok"] is False
    assert "already running" in out["error"]
    # the refusal changed nothing: the running loop keeps its pid
    assert Session(s.session_file).read()["pid"] == r._child.pid


def test_start_after_a_finished_run_works_again(runner):
    r, _ = runner
    assert r.start(1)[0] == 200
    assert _wait(lambda: r.status()["running"] is False, timeout=20)
    assert r.status()["exit_code"] == 0
    assert r.start(1)[0] == 200


def test_every_start_gets_a_header_in_the_log(runner, tmp_path):
    r, _ = runner
    r.start(1)
    _wait(lambda: r.status()["running"] is False, timeout=20)
    r.start(1)
    _wait(lambda: r.status()["running"] is False, timeout=20)
    log = (tmp_path / "state" / "loop.log").read_text(encoding="utf-8")
    assert log.count("=== loop started") == 2
    # anchored on its own line: the header echoes the command, and the fake's *source*
    # contains this string too
    assert log.count("\nfake loop: finished\n") == 2


# ------------------------------------------------------------------ steering


def test_requests_are_refused_when_nothing_runs(runner):
    r, s = runner
    code, out = r.request("stop")
    assert code == 409 and out["ok"] is False
    assert "no loop is running" in out["error"]
    # nothing was written: no request that no one will ever read
    assert Session(s.session_file).read()["pending"] is None


def test_pause_resume_and_stop_reach_the_loop(runner):
    r, s = runner
    # asked for *immediately* after the click, while the child may still be importing:
    # the request must survive the child's own `mark_running` and land at its first
    # generation boundary (that race is the reason `mark_running` keeps a pause)
    assert r.start(200)[0] == 200
    session = Session(s.session_file)

    code, out = r.request("pause")
    assert code == 200 and out["ok"] is True
    assert "generation boundary" in out["note"]
    assert _wait(lambda: session.read()["status"] == "paused")
    # a paused loop is still a live process - pausing is not stopping
    assert r.status()["running"] is True

    code, out = r.request("resume")
    assert code == 200
    assert _wait(lambda: session.read()["status"] == "running")

    code, out = r.request("stop")
    assert code == 200 and "stop" in out["note"].lower()
    assert _wait(lambda: r.status()["running"] is False)
    assert session.read()["status"] == "stopped"
    assert r.status()["exit_code"] == 0


def test_a_loop_that_died_is_not_reported_as_running(runner):
    r, s = runner
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    # a session file left behind by a loop that is gone: the pid has the last word
    Session(s.session_file).write({"status": "running", "pid": dead.pid, "pending": None})
    status = r.status()
    assert status["running"] is False
    assert status["pid"] is None
