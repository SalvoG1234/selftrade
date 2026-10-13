import json
import os
import threading
import time
from pathlib import Path

import pytest

from selftrade.agent import Evolver
from selftrade.config import load_settings
from selftrade.data import make_synthetic
from selftrade.memory import Memory
from selftrade.session import LOCK_STALE_S, Session


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


def test_a_write_survives_a_reader_that_holds_the_file_open(tmp_path, monkeypatch):
    """Windows refuses to rename onto a file another handle has open without share-delete.

    The dashboard polls this file from its own process while the loop writes it, so a
    sharing violation would throw away a prepared state write - on the platform the
    project targets.  The error is raised by hand rather than provoked, because on POSIX
    the rename simply succeeds and a test that can only fail on Windows is a test nobody
    sees fail.
    """
    import selftrade.session as sess

    s = Session(tmp_path / "s.json", poll_interval=0.01)
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            err = PermissionError(13, "The process cannot access the file")
            err.winerror = 32  # ERROR_SHARING_VIOLATION
            raise err
        return real(src, dst)

    monkeypatch.setattr(sess.os, "replace", flaky)
    s.write({"status": "running", "pending": "stop"})
    assert calls["n"] == 3, "the rename must be retried on a sharing violation"
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["pending"] == "stop"


def test_another_permission_error_is_not_retried(tmp_path, monkeypatch):
    """Only the Windows sharing violation is retried; a real denial is raised at once."""
    import selftrade.session as sess

    s = Session(tmp_path / "s.json")
    calls = {"n": 0}

    def denied(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sess.os, "replace", denied)
    with pytest.raises(PermissionError):
        s.write({"status": "running"})
    assert calls["n"] == 1


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


def test_a_stop_survives_the_loops_own_heartbeat_write(tmp_path):
    """The loop and the operator are two writers on one file.

    Both do read-modify-write, so a heartbeat write built from a snapshot taken before the
    operator's ``stop`` arrived used to erase it: the dashboard said "stop requested" and
    the run kept going.  The lock is what makes the two writes indivisible.

    The test only means something while the two writers really do overlap, so it *waits*
    for the heartbeat instead of counting its beats afterwards: a beat count measures how
    fast this machine hands out the GIL, and the same code produced nine beats on one CI
    runner and ten on another - a failure that said nothing about the lock.
    """
    s = Session(tmp_path / "s.json", poll_interval=0.01)
    s.request_start(50, pid=os.getpid())
    writing = threading.Event()
    beat = threading.Event()
    beats = [0]

    def heartbeat():
        while not writing.is_set():
            s.update(lambda st: st.update({"heartbeat": time.time()}) or st)
            beats[0] += 1
            beat.set()
            time.sleep(0.002)  # the loop's cadence: once a generation, not a spin

    t = threading.Thread(target=heartbeat)
    t.start()
    try:
        assert beat.wait(timeout=30), "the heartbeat writer never wrote - this test would prove nothing"
        before = beats[0]
        lost = []
        for n in range(25):
            s.request("stop")
            if s.read()["pending"] != "stop":
                lost.append(n)
        deadline = time.monotonic() + 30
        while beats[0] == before and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        writing.set()
        t.join(timeout=5)
    assert beats[0] > before, "no heartbeat landed while the operator was writing"
    assert lost == [], f"{len(lost)} stop request(s) were overwritten by the loop's own write"


def test_a_lost_create_race_is_read_as_a_held_lock_not_a_broken_state_dir(tmp_path, monkeypatch):
    """Windows answers a lost create race with EACCES, not EEXIST.

    Reading that as "this directory is not writable" let a writer run *without* the lock,
    and two unlocked read-modify-writes is how an operator's ``stop`` disappears: with that
    reading, 6 of 40 instrumented rounds lost a stop and writers collided on the same
    temporary file.  The assertion is the acquisition count, not a duration, so the test
    states the difference the fix makes instead of trusting a clock: with the bad reading
    the second attempt never happens, because the writer has already given up on the lock.
    """
    import selftrade.session as sess

    s = Session(tmp_path / "s.json", poll_interval=0.01)
    s.mark_running(generations=1)
    real_open = os.open
    seen = {"creates": 0, "acquired": 0}

    def racing_open(path, flags, *args, **kwargs):
        if Path(path) != s.lock_path or not (flags & os.O_EXCL):
            return real_open(path, flags, *args, **kwargs)
        seen["creates"] += 1
        if seen["creates"] == 1:
            s.lock_path.write_text("", encoding="utf-8")  # the other writer holds it
            err = PermissionError(13, "The process cannot access the file")
            err.winerror = 5  # ERROR_ACCESS_DENIED, as Windows reports a lost race
            raise err
        if seen["creates"] == 2:
            s.lock_path.unlink()  # ...and here it is done and has released it again
        fd = real_open(path, flags, *args, **kwargs)
        seen["acquired"] += 1
        return fd

    monkeypatch.setattr(sess.os, "open", racing_open)
    s.request("stop")
    assert seen["creates"] >= 2, "the writer gave up on the lock instead of waiting for it"
    assert seen["acquired"] == 1, "the write must happen holding the lock, not beside it"
    assert s.read()["pending"] == "stop"
    assert not s.lock_path.exists(), "the lock must be released, not left for the next writer"


def test_a_lock_left_behind_by_a_dead_process_is_broken(tmp_path):
    """A run must not be frozen out by the last process that died holding the lock."""
    s = Session(tmp_path / "s.json")
    s.lock_path.parent.mkdir(parents=True, exist_ok=True)
    s.lock_path.write_text("", encoding="utf-8")
    stale = time.time() - (LOCK_STALE_S + 5)
    os.utime(s.lock_path, (stale, stale))
    s.mark_running(generations=1)
    assert s.read()["status"] == "running"
    assert not s.lock_path.exists(), "the lock must be released, not left for the next writer"


def test_a_lock_held_elsewhere_delays_a_write_but_never_wedges_it(tmp_path, monkeypatch):
    """A live holder is waited for; a holder that never lets go is written past."""
    monkeypatch.setattr("selftrade.session.LOCK_TIMEOUT_S", 0.2)
    s = Session(tmp_path / "s.json")
    s.lock_path.parent.mkdir(parents=True, exist_ok=True)
    s.lock_path.write_text("", encoding="utf-8")  # fresh, so not stale: honoured until the timeout
    started = time.monotonic()
    s.request("stop")
    assert time.monotonic() - started >= 0.1, "a held lock must be waited for, not run through"
    assert s.read()["pending"] == "stop", "the request must land anyway - a control never hangs"


def test_a_stop_written_right_after_a_resume_is_still_honoured(tmp_path):
    """The pause -> resume -> stop handoff, which is where the stop used to be lost.

    The resumed loop writes its status and heartbeat at that instant, and the operator
    writes ``stop`` at the same one; the two must not be allowed to interleave.
    """
    s = Session(tmp_path / "s.json", poll_interval=0.01)
    s.mark_running(generations=5)
    s.request("pause")
    results = []
    t = threading.Thread(target=lambda: results.append(s.checkpoint(0, 5)))
    t.start()
    assert wait_until(lambda: s.read()["status"] == "paused", timeout=5)
    s.request("resume")
    assert wait_until(lambda: s.read()["status"] == "running", timeout=5)
    s.request("stop")
    t.join(timeout=5)
    assert not t.is_alive()
    assert results == ["continue"]
    assert s.checkpoint(1, 5) == "stop"
    assert s.read()["status"] == "stopped"
