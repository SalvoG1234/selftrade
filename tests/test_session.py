import json
import threading
import time
from pathlib import Path

import pytest

from selftrade.agent import Evolver
from selftrade.config import load_settings
from selftrade.data import make_synthetic
from selftrade.memory import Memory
from selftrade.session import Session


def wait_until(pred, timeout=30.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------- unit


def test_missing_file_defaults(tmp_path):
    s = Session(tmp_path / "nope.json")
    st = s.read()
    assert st["status"] == "idle"
    assert st["generations_done"] == 0


def test_corrupt_file_does_not_raise(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = Session(p)
    st = s.read()
    assert st["corrupt"] is True
    # A checkpoint on a corrupt file must recover, not crash.
    assert s.checkpoint(0, 3) == "continue"
    st2 = Session(p).read()
    assert st2["status"] == "running"


def test_request_and_status(tmp_path):
    s = Session(tmp_path / "s.json")
    s.mark_running(generations=5)
    assert s.read()["status"] == "running"
    s.request("pause")
    assert s.read()["pending"] == "pause"
    assert s.read()["last_request"]["action"] == "pause"


def test_checkpoint_consumes_stop(tmp_path):
    s = Session(tmp_path / "s.json")
    s.mark_running(generations=5)
    s.request("stop")
    assert s.checkpoint(0, 5) == "stop"
    assert s.read()["status"] == "stopped"


def test_checkpoint_pause_blocks_until_resume(tmp_path):
    s = Session(tmp_path / "s.json", poll_interval=0.02)
    s.mark_running(generations=5)
    s.request("pause")

    results = []

    def worker():
        results.append(s.checkpoint(0, 5))

    t = threading.Thread(target=worker)
    t.start()
    assert wait_until(lambda: s.read()["status"] == "paused", timeout=5)
    time.sleep(0.15)
    assert t.is_alive(), "checkpoint must be blocked while paused"
    assert s.read()["generations_done"] == 0, "progress must be frozen"
    s.request("resume")
    t.join(timeout=5)
    assert not t.is_alive()
    assert results == ["continue"]
    assert s.read()["status"] == "running"
    assert s.read()["generations_done"] == 1


def test_checkpoint_stop_while_paused(tmp_path):
    s = Session(tmp_path / "s.json", poll_interval=0.02)
    s.mark_running(generations=5)
    s.request("pause")
    results = []
    t = threading.Thread(target=lambda: results.append(s.checkpoint(0, 5)))
    t.start()
    assert wait_until(lambda: s.read()["status"] == "paused", timeout=5)
    s.request("stop")
    t.join(timeout=5)
    assert results == ["stop"]


# ---------------------------------------------------------------- integration


def small_settings(tmp_path):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evolution.population = 3
    s.evolution.elite = 1
    s.evolution.seed = 4
    s.evaluation.min_trades = 1
    s.evaluation.max_drawdown = 0.99
    s.memory.db = str(tmp_path / "state" / "m.db")
    return s


def test_evolver_pause_freezes_then_completes(tmp_path):
    settings = small_settings(tmp_path)
    df = make_synthetic(3000, seed=61)
    session = Session(tmp_path / "s.json", poll_interval=0.02)
    session.mark_running(generations=3)
    mem = Memory(settings.memory.db)
    ev = Evolver(
        settings, memory=mem, df=df, session=session,
        log=lambda *_: None, report_dir=str(tmp_path / "reports"),
    )
    holder = {}

    def worker():
        holder["res"] = ev.run(generations=3, use_llm=False, quiet=True)

    # Request the pause before starting so the first checkpoint always sees it
    # (the run itself can be faster than the request otherwise).
    session.request("pause")
    t = threading.Thread(target=worker)
    t.start()
    assert wait_until(lambda: session.read()["status"] == "paused", timeout=20)
    frozen = session.read()["generations_done"]
    time.sleep(0.5)
    assert session.read()["generations_done"] == frozen, "generations_done must not move while paused"

    session.request("resume")
    t.join(timeout=60)
    assert not t.is_alive()
    final = session.read()
    assert final["status"] == "done"
    assert final["generations_done"] == 3
    res = holder["res"]
    assert res.report_md and Path(res.report_md).exists()
    assert len(res.generations) == 3


def test_evolver_stop_writes_report(tmp_path):
    settings = small_settings(tmp_path)
    df = make_synthetic(3000, seed=62)
    session = Session(tmp_path / "s.json", poll_interval=0.02)
    session.mark_running(generations=5)
    mem = Memory(settings.memory.db)
    ev = Evolver(
        settings, memory=mem, df=df, session=session,
        log=lambda *_: None, report_dir=str(tmp_path / "reports"),
    )
    holder = {}
    session.request("stop")
    t = threading.Thread(target=lambda: holder.update(res=ev.run(generations=5, use_llm=False, quiet=True)))
    t.start()
    t.join(timeout=60)
    assert not t.is_alive()
    res = holder["res"]
    assert res.stopped is True
    assert res.report_md and Path(res.report_md).exists()
    assert session.read()["status"] == "stopped"
