"""Tests for the session-lock soak.

Two things have to be true for this tool to be worth anything, and they are the two things
tested here: the shipped lock must come out **clean** under the injected race, and the
instrument must actually *count* a write that happens beside a held lock -- otherwise a clean
report is a coincidence and not evidence.  The second one is driven deterministically (a real
holder on another thread, the race injected at 100%), so it does not depend on a schedule.
"""

import json
import tempfile
import threading
from pathlib import Path

from selftrade import lockstress as ls
from selftrade.cli import build_parser, main
from selftrade.session import Session


def _hold(session: Session, ready: threading.Event, release: threading.Event, done: threading.Event):
    """Hold the session lock on this thread until ``release`` is set."""
    try:
        with session._locked():
            ready.set()
            release.wait(5)
    finally:
        done.set()


def _hold_for(session: Session, seconds: float) -> tuple[threading.Event, threading.Event]:
    ready, release, done = threading.Event(), threading.Event(), threading.Event()
    thread = threading.Thread(
        target=_hold, args=(session, ready, release, done), name="lockstress-test-holder"
    )
    thread.start()
    assert ready.wait(5), "the holder never took the lock"
    timer = threading.Timer(seconds, release.set)
    timer.daemon = True
    timer.start()
    return done, release


# ---------------------------------------------------------------- the instrument


def test_the_instrument_counts_a_write_that_bypasses_a_held_lock(tmp_path):
    """The race injected at 100% while another writer really holds the lock.

    With the pre-fix reading the failed create means "carry on", so the write happens beside a
    lock somebody else holds -- and that is exactly what the counter must catch.
    """
    instrument = ls.LockInstrument(race_share=1.0, replace_share=0.0, seed=1)
    path = tmp_path / "session.json"
    holder = Session(path, poll_interval=0.01)
    writer = ls.LegacyLockSession(path, poll_interval=0.01)
    instrument.install()  # before the holder takes it: the instrument has to see it hold
    done, release = _hold_for(holder, 5.0)
    try:
        writer.update(lambda st: st.update({"heartbeat": 1.0}) or st)
    finally:
        release.set()
        done.wait(5)
        instrument.remove()
    snap = instrument.counters.snapshot()
    assert snap["lock_races_injected"] >= 1, "the injection never fired: nothing was tested"
    assert snap["unlocked_writes"] == 1, "a write beside a held lock must be counted as one"
    assert snap["lock_acquisitions"] == 1, "the only acquisition is the holder's"
    assert ls._verdict(snap, []) == "defect"


def test_the_shipped_lock_waits_out_the_same_race_and_writes_inside_the_lock(tmp_path):
    """The same injection, the shipped reading: it waits for the holder instead of writing.

    The holder lets go after 50 ms, so the writer's retry succeeds -- the assertion is that it
    waited (no unlocked write, one acquisition) rather than that it was fast.
    """
    instrument = ls.LockInstrument(race_share=1.0, replace_share=0.0, seed=1)
    path = tmp_path / "session.json"
    holder = Session(path, poll_interval=0.01)
    writer = Session(path, poll_interval=0.01)
    instrument.install()  # before the holder takes it: the instrument has to see it hold
    done, release = _hold_for(holder, 0.05)
    try:
        writer.update(lambda st: st.update({"heartbeat": 1.0}) or st)
    finally:
        release.set()
        done.wait(5)
        instrument.remove()
    snap = instrument.counters.snapshot()
    assert snap["lock_races_injected"] >= 1, "the injection never fired: nothing was tested"
    assert snap["unlocked_writes"] == 0, "the shipped lock must never write beside the lock"
    assert snap["lock_acquisitions"] == 2, "the holder and then the writer that waited"
    assert path.exists()
    assert ls._verdict(snap, []) == "clean"


def test_an_unreadable_state_file_is_not_a_lost_request(tmp_path, monkeypatch):
    """A reader outside the lock can meet a rename; that is not an overwritten ``stop``.

    ``Session.read()`` degrades a transient error to defaults, which is right for a poll and
    wrong for a detector: this soak first reported a "lost" stop that was only a
    ``PermissionError`` from reading the file mid-rename.  The retry is what removes that
    false positive, and the failure is still reported when it does not clear.
    """
    import selftrade.lockstress as mod

    path = tmp_path / "session.json"
    path.write_text('{"pending": "stop"}', encoding="utf-8")
    assert mod._read_pending(path) == ("stop", True)

    calls = {"n": 0}
    real = Path.read_text

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            err = PermissionError(13, "The process cannot access the file")
            err.winerror = 32
            raise err
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert mod._read_pending(path) == ("stop", True)
    assert calls["n"] == 3, "the read must be retried, not read as an empty state"

    monkeypatch.setattr(mod, "READ_RETRIES", 3)
    calls["n"] = 100  # keep failing

    def always(self, *a, **kw):
        err = PermissionError(13, "The process cannot access the file")
        err.winerror = 32
        raise err

    monkeypatch.setattr(Path, "read_text", always)
    assert mod._read_pending(path) == (None, False), "an unreadable file must say so"


# ---------------------------------------------------------------- the soak


def test_the_soak_is_clean_on_the_lock_as_shipped(tmp_path):
    """The invariant: with the race injected, no stop is overwritten and no write is unlocked."""
    report = ls.run_soak(
        rounds=3,
        writers=3,
        cycles=2,
        race_share=0.3,
        replace_share=0.1,
        seed=7,
        budget_s=30,
        workdir=tmp_path / "soak",
    )
    snap = report["counters"]
    assert snap["lock_races_injected"] > 0, "a clean run with no injection proves nothing"
    assert snap["stop_checks"] > 0
    assert snap["read_failures"] == 0
    assert snap["stops_lost"] == 0
    assert snap["unlocked_writes"] == 0
    assert snap["temp_overlaps"] == 0
    assert snap["write_errors"] == 0
    assert report["verdict"] == "clean"
    assert report["clean"] is True


def test_the_soak_holds_with_the_injection_switched_off(tmp_path):
    """No injection at all: the natural race is still met and still survives.

    This is the honest walk before the injected one.  On this platform ``os.open`` with
    ``O_EXCL`` is denied by the operating system itself now and then (another thread is
    unlinking the lock at that instant), and the count is reported rather than confused with
    an injection.
    """
    report = ls.run_soak(
        rounds=3, writers=3, cycles=2, race_share=0.0, replace_share=0.0, seed=1, budget_s=20,
        workdir=tmp_path / "soak",
    )
    assert report["counters"]["lock_races_injected"] == 0
    assert report["verdict"] == "clean"
    assert report["counters"]["stops_lost"] == 0


def test_the_comparison_rule_refuses_an_unproven_pass():
    """A clean run beside a clean pre-fix run is inconclusive, never a pass."""
    clean = {"verdict": "clean"}
    defect = {"verdict": "defect"}
    assert ls.compare_verdict(clean, defect) == {"validated": True, "verdict": "validated"}
    assert ls.compare_verdict(clean, clean) == {"validated": False, "verdict": "inconclusive"}
    assert ls.compare_verdict(defect, defect) == {"validated": False, "verdict": "regression"}
    assert ls.compare_verdict(defect, clean) == {"validated": False, "verdict": "regression"}


def test_the_budget_stops_the_run_and_says_so(tmp_path):
    report = ls.run_soak(
        rounds=10000, writers=2, cycles=1, budget_s=0.4, seed=1, workdir=tmp_path / "soak"
    )
    assert report["stopped_early"] is True
    assert report["rounds"] < 10000
    assert "stopped early" in ls.summary_line(report)


def test_the_soak_works_in_its_own_directory_and_touches_nothing_else(tmp_path, monkeypatch):
    """The operator's live session file must never be the subject of a soak."""
    monkeypatch.chdir(tmp_path)
    live = tmp_path / "state" / "session.json"
    live.parent.mkdir(parents=True)
    live.write_text('{"status": "running", "pending": "stop"}', encoding="utf-8")
    before = live.read_text(encoding="utf-8")
    temp_root = Path(tempfile.gettempdir())
    leftovers = set(temp_root.glob("selftrade-lockstress-*"))
    report = ls.run_soak(rounds=2, writers=2, cycles=1, budget_s=20)
    assert report["rounds"] == 2
    assert live.read_text(encoding="utf-8") == before
    assert set(temp_root.glob("selftrade-lockstress-*")) <= leftovers, "the soak left a directory behind"


def test_the_report_renders_and_names_the_counters(tmp_path):
    report = ls.run_soak(
        rounds=2, writers=2, cycles=1, budget_s=20, workdir=tmp_path / "soak"
    )
    lines = ls.console_lines(report)
    assert lines and "Lock soak" in lines[0]
    assert any("stops written by the operator" in line for line in lines)
    md = ls.render_markdown(report)
    assert "## Lock soak" in md
    assert "| stops_lost |" in md
    for name in ls.FIELDS:
        assert report["counters"][name] == report["counters"][name]


# ---------------------------------------------------------------- the CLI


def test_parser_exposes_lock_stress():
    parser = build_parser()
    names = set(parser._subparsers._group_actions[0].choices)
    assert "lock-stress" in names


def test_lock_stress_command_runs_and_passes(capsys):
    code = main(["lock-stress", "--rounds", "2", "--writers", "2", "--cycles", "1", "--budget", "20"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Lock soak" in out
    assert "-> clean" in out


def test_lock_stress_command_json_payload(capsys):
    code = main(
        ["lock-stress", "--rounds", "2", "--writers", "2", "--cycles", "1", "--budget", "20", "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {"verdict", "clean", "counters", "rounds", "writers", "cycles"}
    assert payload["clean"] is True
    assert set(payload["counters"]) == set(ls.FIELDS)


def test_lock_stress_command_writes_a_report(capsys, tmp_path):
    out_md = tmp_path / "soak.md"
    code = main(
        [
            "lock-stress", "--rounds", "2", "--writers", "2", "--cycles", "1",
            "--budget", "20", "--out", str(out_md),
        ]
    )
    assert code == 0
    assert out_md.exists()
    assert "## Lock soak" in out_md.read_text(encoding="utf-8")


def test_lock_stress_legacy_reading_is_reported_as_a_defect(capsys):
    """The pre-fix reading must fail the soak; that is the soak showing its teeth."""
    code = main(
        [
            "lock-stress", "--rounds", "3", "--writers", "3", "--cycles", "2",
            "--race-share", "0.8", "--budget", "20", "--legacy",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "before the fix" in out
    assert "-> defect" in out


def test_lock_stress_compare_validates_the_instrument(capsys):
    code = main(
        ["lock-stress", "--compare", "--rounds", "6", "--cycles", "2", "--race-share", "0.5",
         "--budget", "25"]
    )
    out = capsys.readouterr().out
    assert "comparison: validated" in out, out
    assert code == 0


def test_lock_stress_refuses_a_share_that_is_not_a_share(capsys):
    """`--race-share 15` would switch the injection off and report a clean run."""
    code = main(["lock-stress", "--race-share", "15", "--rounds", "1"])
    assert code == 2
    assert "between 0 and 1" in capsys.readouterr().err
    code = main(["lock-stress", "--replace-share", "-0.5", "--rounds", "1"])
    assert code == 2
