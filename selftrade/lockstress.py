"""Session-lock soak: two writers, one operator, and the Windows race, injected.

``tests/test_session.py`` states the lock's rules one at a time, deterministically.  This
module is the instrument beside them: it runs the real
:class:`~selftrade.session.Session` the way the defect happened --- several writers, an
operator writing ``stop``, and the *lost create race* that Windows reports as ``EACCES``
injected at a chosen rate --- and counts what it saw.

Nobody should have to take "the lock holds" on trust.  :func:`run_soak` reports the counters
in full (requests written, requests a writer overwrote, writes performed without the lock,
writes that overlapped on the same temporary file, races injected, attempts that found the
file already created, renames retried), and the very same run can be repeated against
:class:`LegacyLockSession` --- the pre-fix reading this tool exists to detect.  So a run that
reports *zero* losses is printed beside a counter that *moved* without the fix; that is what
``--compare`` does, and a run whose injection never bites is reported as inconclusive rather
than green.

Why the race must be injected.  A lost create race is a few microseconds wide, so it only
arrives when several writers reach for the same new name at the same instant: a soak that
waited for one would either wait a long time or report a clean bill of health without ever
exercising the path it is there to exercise.  The injection is faithful rather than
arbitrary:

* it fires **only while another writer really holds the lock** --- the one situation in which
  the create can be lost, and one in which the holder will release it, so a writer that waits
  is never left waiting for a lock nobody owns;
* it raises exactly what Windows raises for a lost race: ``PermissionError`` with ``errno`` 13
  and ``winerror`` 5 (``ERROR_ACCESS_DENIED``), so the code under test cannot tell the
  injected event from the real one;
* the rename side is injected the same way with ``winerror`` 32
  (``ERROR_SHARING_VIOLATION``), which is the error a ``Path.read_text`` reader on the
  destination causes on Windows.

The injection does not stand in for the real thing, it is *counted beside* it: every create
the operating system denies by itself is reported as ``lock_denied_by_os``, so the natural
rate is visible and is never confused with an inject.

The lock is the *existence* of the lock file, created with ``O_EXCL`` against a shared path,
so a thread-only harness exercises the same protocol two processes do: the operating system
serializes the create whichever way the two callers started.

Nothing here touches the operator's session file.  The soak works in a private temporary
directory and removes it when it is done.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .session import LOCK_POLL_S, LOCK_TIMEOUT_S, Session

#: How long the operator waits for a writer to land another heartbeat after its ``stop``.
#: A check that times out is still read (an erasure has already happened or it has not), but
#: it is counted: a silent timeout would turn "the writers never touched the file" into a
#: clean bill of health.
WRITE_WAIT_S = 2.0

#: How often a writer settles.  The work itself is file I/O, which is what gives the writers a
#: window to overlap; the pause only keeps the soak from pinning a core.
WRITER_SLEEP_S = 0.0005

#: The operator's reading of the result is taken the way the *dashboard* takes it: from
#: outside the lock.  On Windows that read meets the writer's rename, and ``Path.read_text``
#: then fails instead of reading the file - with errno 13, and (measured) often **without** a
#: ``winerror`` at all, so the retry cannot be narrowed by error code.  It is bounded, so a
#: file this process genuinely cannot read is still reported rather than waited on forever,
#: and an unreadable file is never mistaken for a request that was overwritten.
READ_RETRIES = 40
READ_BACKOFF_S = 0.005

#: Windows: ERROR_ACCESS_DENIED, what a *lost create race* is reported as.
WINERROR_ACCESS_DENIED = 5
#: Windows: ERROR_SHARING_VIOLATION, what a reader on the rename destination causes.
WINERROR_SHARING_VIOLATION = 32

#: Every counter the soak keeps.  Named in the report, zeroed before each run.
FIELDS = (
    "lock_creates",
    "lock_races_injected",
    "lock_held_by_other",
    "lock_denied_by_os",
    "lock_acquisitions",
    "writes",
    "unlocked_writes",
    "temp_overlaps",
    "renames",
    "rename_violations_injected",
    "stop_requests",
    "stop_checks",
    "stop_wait_timeouts",
    "stops_lost",
    "read_failures",
    "write_errors",
)


# --------------------------------------------------------------------------
# what the run is counted with
# --------------------------------------------------------------------------

class Counters:
    """Thread-safe tallies.  A soak is several threads writing to one report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = {name: 0 for name in FIELDS}

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._data[name] += n

    def get(self, name: str) -> int:
        with self._lock:
            return self._data[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._data)


def _access_denied() -> PermissionError:
    """What Windows raises when a create race is lost (EACCES / ERROR_ACCESS_DENIED)."""
    err = PermissionError(
        13, "The process cannot access the file because it is being used by another process"
    )
    err.winerror = WINERROR_ACCESS_DENIED  # type: ignore[attr-defined]
    return err


def _sharing_violation() -> PermissionError:
    """What Windows raises when a reader holds the rename destination open."""
    err = PermissionError(13, "The process cannot access the file")
    err.winerror = WINERROR_SHARING_VIOLATION  # type: ignore[attr-defined]
    return err


class LockInstrument:
    """Inject the Windows errors while a run is in progress, and count the run.

    The patch is installed on the ``session`` module during the soak and removed afterwards,
    in a ``finally``.  Only the lock create (``O_EXCL`` on a ``*.lock`` path) and the rename
    are intercepted; every other ``os.open`` / ``os.replace`` is passed straight through.

    ``held`` maps a thread to whether it currently holds the lock, and it is what makes the
    injection faithful: the race fires only when another writer holds the lock, which is the
    only moment a create can be lost --- and the moment the holder is about to release it.
    That is also how the report can say a write happened *without* the lock: a write counted
    while its own thread is not the holder is one that ran beside the lock, which is the
    defect, not a diagnostic quirk.
    """

    def __init__(
        self,
        *,
        race_share: float = 0.2,
        replace_share: float = 0.1,
        seed: int = 0,
        counters: Counters | None = None,
    ) -> None:
        self.counters = counters if counters is not None else Counters()
        self.race_share = float(race_share)
        self.replace_share = float(replace_share)
        self._rng = random.Random(int(seed))
        self._rng_lock = threading.Lock()
        self._held_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._held: dict[int, bool] = {}
        self._in_write: dict[int, bool] = {}
        self._saved: dict[str, Any] = {}
        self.installed = False

    # ------------------------------------------------------------------ patch
    def install(self) -> None:
        import selftrade.session as sess

        self._saved = {
            "open": sess.os.open,
            "replace": sess.os.replace,
            "write": sess.Session._write_unlocked,
            "drop": sess.Session._drop_lock,
        }
        instrument = self

        # Plain functions, not bound methods: these are assigned onto a *class*, and a bound
        # method there would swallow the receiver the way a descriptor would not.
        def traced_write(inner: Session, state: dict[str, Any]) -> dict[str, Any]:
            return instrument._write_unlocked(inner, state)

        def traced_drop(inner: Session) -> None:
            return instrument._drop_lock(inner)

        sess.os.open = self._open
        sess.os.replace = self._replace
        sess.Session._write_unlocked = traced_write
        sess.Session._drop_lock = traced_drop
        self.installed = True

    def remove(self) -> None:
        if not self.installed:
            return
        import selftrade.session as sess

        sess.os.open = self._saved["open"]
        sess.os.replace = self._saved["replace"]
        sess.Session._write_unlocked = self._saved["write"]
        sess.Session._drop_lock = self._saved["drop"]
        self.installed = False

    # --------------------------------------------------------------- state
    def holds(self, tid: int) -> bool:
        with self._held_lock:
            return bool(self._held.get(tid))

    def someone_else_holds(self, tid: int) -> bool:
        with self._held_lock:
            return any(v for k, v in self._held.items() if k != tid)

    def set_held(self, tid: int, value: bool) -> None:
        with self._held_lock:
            self._held[tid] = bool(value)

    def _draw(self) -> float:
        with self._rng_lock:
            return self._rng.random()

    # ------------------------------------------------------------ the create
    def _open(self, path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        real = self._saved["open"]
        if not (flags & os.O_EXCL) or not str(path).endswith(".lock"):
            return real(path, flags, *args, **kwargs)
        tid = threading.get_ident()
        self.counters.bump("lock_creates")
        if self.race_share > 0 and self.someone_else_holds(tid) and self._draw() < self.race_share:
            # A lost race: the other writer took the name first and this is what Windows
            # reports.  Only raised while that writer really holds it, so the retry finds the
            # file released instead of waiting out the deadline.
            self.counters.bump("lock_races_injected")
            raise _access_denied()
        try:
            fd = real(path, flags, *args, **kwargs)
        except FileExistsError:
            self.counters.bump("lock_held_by_other")
            raise
        except PermissionError:
            # Not injected: the operating system denied this create itself.  It happens when
            # another thread is unlinking the lock at that instant --- which is the very race
            # the fix is about, so the soak counts it instead of mistaking it for an inject.
            self.counters.bump("lock_denied_by_os")
            raise
        self.set_held(tid, True)
        self.counters.bump("lock_acquisitions")
        return fd

    def _drop_lock(self, inner: Session) -> None:
        try:
            self._saved["drop"](inner)
        finally:
            self.set_held(threading.get_ident(), False)

    # ------------------------------------------------------------- the write
    def _write_unlocked(self, inner: Session, state: dict[str, Any]) -> dict[str, Any]:
        tid = threading.get_ident()
        self.counters.bump("writes")
        if not self.holds(tid):
            self.counters.bump("unlocked_writes")
        with self._write_lock:
            overlapped = any(v for k, v in self._in_write.items() if k != tid and v)
            self._in_write[tid] = True
        if overlapped:
            self.counters.bump("temp_overlaps")
        try:
            return self._saved["write"](inner, state)
        except Exception:
            self.counters.bump("write_errors")
            raise
        finally:
            with self._write_lock:
                self._in_write[tid] = False

    # ------------------------------------------------------------ the rename
    def _replace(self, src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        self.counters.bump("renames")
        if self.replace_share > 0 and self._draw() < self.replace_share:
            self.counters.bump("rename_violations_injected")
            raise _sharing_violation()
        return self._saved["replace"](src, dst, *args, **kwargs)


class LegacyLockSession(Session):
    """The lock as it was read *before* the fix, kept so the soak can prove its own teeth.

    A create that failed with ``EACCES`` was read as "I cannot create files here", the writer
    carried on **without** the lock, and two unlocked read-modify-writes is how an operator's
    ``stop`` was overwritten.  Nothing in the application uses this class: it exists so that
    ``--compare`` can print the same counters for the broken reading, which is what makes a
    clean report meaningful rather than a claim.
    """

    @contextmanager
    def _locked(self) -> Iterator[None]:
        acquired = False
        broke_stale = False
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while not acquired:
            try:
                os.close(os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                acquired = True
            except FileExistsError:
                if not broke_stale and self._lock_is_stale():
                    broke_stale = True
                    self._drop_lock()
                    continue
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_S)
            except OSError:
                # The reading under test: "this state directory is not writable", so proceed
                # unlocked instead of reading the error as a lock somebody else holds.
                break
        try:
            yield
        finally:
            if acquired:
                self._drop_lock()


# --------------------------------------------------------------------------
# the soak
# --------------------------------------------------------------------------

def _read_pending(path: Path) -> tuple[str | None, bool]:
    """``(pending, readable)`` from outside the lock, tolerating a rename in progress.

    ``Session.read()`` degrades a transient error to defaults - the right call for a poll that
    will be repeated, and the wrong one for a detector: a request that could not be *read* is
    not a request that was lost.  So the file is retried here, and a persistent failure is
    returned as such instead of as an empty state.
    """
    for attempt in range(READ_RETRIES):
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, True  # gone means gone: a writer never removes the file
        except PermissionError:  # Windows: a rename has the name mid-swap
            # Any PermissionError, not only a coded sharing violation: the reader's denial
            # carries no WinError code on this platform, and it is the same window either way.
            if attempt + 1 >= READ_RETRIES:
                return None, False
            time.sleep(READ_BACKOFF_S)
            continue
        except OSError:
            return None, False
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None, False  # a corrupt file is a result, not a read to retry
        return (data.get("pending") if isinstance(data, dict) else None), True
    return None, False


def _reset(path: Path) -> None:
    for suffix in (".lock", ".tmp", ""):
        candidate = path.with_suffix(path.suffix + suffix) if suffix else path
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - a lock held open elsewhere
            pass


def _wait_for(pred: Any, timeout: float) -> bool:
    end = time.monotonic() + float(timeout)
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.001)
    return bool(pred())


def _writer(
    cls: type[Session],
    path: Path,
    stop: threading.Event,
    counters: Counters,
    errors: list[str],
    sleep_s: float,
) -> None:
    """A loop's heartbeat: read-modify-write, repeatedly, until the round ends."""
    try:
        session = cls(path, poll_interval=0.005)
        while not stop.is_set():
            session.update(lambda st: st.update({"heartbeat": time.time()}) or st)
            if sleep_s:
                time.sleep(sleep_s)
    except Exception as exc:  # a writer that cannot write is a result, not a crash
        if len(errors) < 20:
            errors.append(f"writer: {type(exc).__name__}: {exc}")


def run_soak(
    *,
    rounds: int = 15,
    writers: int = 3,
    cycles: int = 3,
    race_share: float = 0.15,
    replace_share: float = 0.05,
    seed: int = 7,
    legacy: bool = False,
    budget_s: float = 30.0,
    writer_sleep_s: float = WRITER_SLEEP_S,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the two writers against the operator, with the race injected, and report.

    Each round: several writers heartbeat the session file while an operator writes ``stop``
    and, after the next writer lands, reads the file back.  Under a correct lock no writer can
    overwrite a request it did not see, so ``stops_lost`` and ``unlocked_writes`` must both be
    zero.  ``legacy=True`` swaps in the pre-fix reading, where the same injection makes both
    counts move --- that is the soak showing its own teeth.

    The run stops after ``rounds`` or ``budget_s``, whichever comes first, and reports whether
    it stopped early instead of hiding it.
    """
    own_dir = workdir is None
    root = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="selftrade-lockstress-"))
    path = root / "session.json"
    cls = LegacyLockSession if legacy else Session
    counters = Counters()
    errors: list[str] = []
    instrument = LockInstrument(
        race_share=race_share, replace_share=replace_share, seed=seed, counters=counters
    )
    started = time.monotonic()
    deadline = started + max(0.0, float(budget_s))
    completed = 0
    try:
        root.mkdir(parents=True, exist_ok=True)
        instrument.install()
        try:
            for _ in range(int(rounds)):
                if time.monotonic() >= deadline:
                    break
                _reset(path)
                stop = threading.Event()
                threads = [
                    threading.Thread(
                        target=_writer,
                        args=(cls, path, stop, counters, errors, float(writer_sleep_s)),
                        name=f"lockstress-writer-{i}",
                        daemon=True,
                    )
                    for i in range(max(1, int(writers)))
                ]
                for thread in threads:
                    thread.start()
                try:
                    operator = cls(path, poll_interval=0.01)
                    for _ in range(max(1, int(cycles))):
                        try:
                            operator.request("stop")
                            counters.bump("stop_requests")
                            target = counters.get("writes") + 2
                            if not _wait_for(lambda: counters.get("writes") >= target, WRITE_WAIT_S):
                                counters.bump("stop_wait_timeouts")
                            pending, readable = _read_pending(path)
                            if not readable:
                                counters.bump("read_failures")
                                continue
                            counters.bump("stop_checks")
                            if pending != "stop":
                                counters.bump("stops_lost")
                                break
                            # Consume the request the way a loop would, so the next cycle starts
                            # from a clean slate and is an independent chance to lose a stop.
                            operator.update(lambda st: st.update({"pending": None}) or st)
                        except Exception as exc:
                            # The pre-fix reading lets two writers share one temporary file, so
                            # a write can meet a handle that is still open or a source already
                            # renamed away.  That is a result, not a crash: the instrument has
                            # already counted the failed write, and the round ends here.
                            if len(errors) < 20:
                                errors.append(f"operator: {type(exc).__name__}: {exc}")
                            break
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join(timeout=5)
                completed += 1
        finally:
            instrument.remove()
        seconds = time.monotonic() - started
        return _report(
            legacy=legacy,
            snapshot=counters.snapshot(),
            errors=errors,
            completed=completed,
            rounds=int(rounds),
            writers=int(writers),
            cycles=int(cycles),
            race_share=float(race_share),
            replace_share=float(replace_share),
            seed=int(seed),
            budget_s=float(budget_s),
            seconds=seconds,
        )
    finally:
        if own_dir:
            shutil.rmtree(root, ignore_errors=True)


def _verdict(snapshot: dict[str, int], errors: list[str]) -> str:
    """``clean`` when the lock held; otherwise the way it did not.

    A failed write counts: under the lock no two writers share the temporary file, so a
    rename that cannot find its source is the unlocked path showing itself, not a hiccup.
    Unreadable reads do **not** count - the reader is deliberately outside the lock, so it
    can meet a rename, and a request that could not be read is not a request that was lost.
    """
    if snapshot["stops_lost"] or snapshot["unlocked_writes"] or snapshot["write_errors"]:
        return "defect"
    if errors:
        return "error"
    return "clean"


def _report(
    *,
    legacy: bool,
    snapshot: dict[str, int],
    errors: list[str],
    completed: int,
    rounds: int,
    writers: int,
    cycles: int,
    race_share: float,
    replace_share: float,
    seed: int,
    budget_s: float,
    seconds: float,
) -> dict[str, Any]:
    verdict = _verdict(snapshot, errors)
    return {
        "legacy": bool(legacy),
        "verdict": verdict,
        "clean": verdict == "clean",
        "rounds": int(completed),
        "rounds_planned": int(rounds),
        "stopped_early": int(completed) < int(rounds),
        "writers": int(writers),
        "cycles": int(cycles),
        "race_share": float(race_share),
        "replace_share": float(replace_share),
        "seed": int(seed),
        "budget_s": float(budget_s),
        "seconds": round(float(seconds), 3),
        "counters": snapshot,
        "errors": list(errors),
    }


def compare_verdict(fixed: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
    """What a pair of runs is allowed to claim.  Kept pure so the rule is testable on its own.

    **validated** needs both halves: the shipped lock clean *and* the pre-fix reading broken.
    A pair where the pre-fix reading also lost nothing is **inconclusive** --- the conditions
    never arose, so the clean run proved nothing --- and a pair where the shipped lock broke
    is a **regression**.  Reporting the first as a pass is the kind of green light this tool
    exists to refuse.
    """
    validated = bool(fixed["verdict"] == "clean" and legacy["verdict"] == "defect")
    return {
        "validated": validated,
        "verdict": (
            "validated" if validated else ("regression" if fixed["verdict"] != "clean" else "inconclusive")
        ),
    }


def compare(**kw: Any) -> dict[str, Any]:
    """Run both readings and say whether the instrument actually bites."""
    before = run_soak(legacy=True, **kw)
    after = run_soak(legacy=False, **kw)
    return {"fixed": after, "legacy": before, **compare_verdict(after, before)}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _reading(legacy: bool) -> str:
    return "before the fix" if legacy else "the lock as shipped"


def summary_line(report: dict[str, Any], label: str | None = None) -> str:
    snap = report["counters"]
    name = _reading(report["legacy"]) if label is None else label
    return (
        f"Lock soak ({name}): {report['rounds']} rounds, "
        f"{report['writers']} writers x {report['cycles']} stop-cycles "
        f"({snap['stop_checks']} checks, {snap['writes']} writes, "
        f"{snap['lock_races_injected']} create races injected, "
        f"{snap['rename_violations_injected']} renames violated) in "
        f"{report['seconds']:.2f} s -> {report['verdict']}"
        + (" (stopped early on the time budget)" if report["stopped_early"] else "")
    )


def findings(report: dict[str, Any]) -> list[str]:
    snap = report["counters"]
    out = [
        f"requests: {snap['stop_requests']} stops written by the operator, "
        f"{snap['stops_lost']} overwritten by a writer, {snap['stop_checks']} read back"
        + (
            f" ({snap['stop_wait_timeouts']} of them without a writer landing a write)"
            if snap["stop_wait_timeouts"]
            else ""
        ),
        f"writes: {snap['writes']} total, {snap['unlocked_writes']} performed without the lock, "
        f"{snap['temp_overlaps']} overlapping another writer on the same temporary file, "
        f"{snap['write_errors']} failing",
        f"reads: {snap['stop_checks']} taken from outside the lock, {snap['read_failures']} "
        "that could not be read at all (a rename in progress is met and retried, and never "
        "mistaken for a lost request)",
        f"lock: {snap['lock_creates']} creates, {snap['lock_acquisitions']} acquisitions, "
        f"{snap['lock_held_by_other']} found the file already created, "
        f"{snap['lock_races_injected']} lost races injected and "
        f"{snap['lock_denied_by_os']} denied by the operating system itself",
        f"rename: {snap['renames']} renames, {snap['rename_violations_injected']} sharing "
        "violations injected and retried (a reader holding the destination open)",
    ]
    if report["legacy"]:
        out.append(
            "this is the pre-fix reading: the counters above are what the soak exists to "
            "detect, not a state to ship"
        )
    elif report["verdict"] == "clean":
        out.append(
            "no request was overwritten and no write happened beside the lock, with the "
            "race injected"
        )
    return out


def console_lines(report: dict[str, Any]) -> list[str]:
    lines = [summary_line(report)]
    lines += [f"  * {line}" for line in findings(report)]
    for line in report["errors"]:
        lines.append(f"  ! {line}")
    return lines


def compare_lines(result: dict[str, Any]) -> list[str]:
    after = result["fixed"]
    before = result["legacy"]
    lines = [
        f"Lock soak comparison: {result['verdict']}",
        "  " + summary_line(after, label=f"{_reading(False):<16}"),
        "  " + summary_line(before, label=f"{_reading(True):<16}"),
    ]
    a = after["counters"]
    b = before["counters"]
    lines.append(
        f"  the same injection, two readings -> stops lost {a['stops_lost']} vs "
        f"{b['stops_lost']}, writes without the lock {a['unlocked_writes']} vs "
        f"{b['unlocked_writes']}"
    )
    if result["validated"]:
        lines.append(
            "  validated: the instrument lost nothing on the shipped lock and did lose "
            "requests with the pre-fix reading, so the clean run above means something"
        )
    elif result["verdict"] == "regression":
        lines.append(
            "  REGRESSION: the shipped lock lost a request or wrote beside the lock under "
            "the same conditions the pre-fix reading fails"
        )
    else:
        lines.append(
            "  inconclusive: the pre-fix reading lost nothing either, so the injection never "
            "fired and the clean run proves nothing - raise --race-share or --rounds"
        )
    return lines


def render_compare_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# Lock soak comparison: {result['verdict']}",
        "",
    ] + compare_lines(result) + [
        "",
        "---",
        "",
        render_markdown(result["fixed"]),
        "---",
        "",
        render_markdown(result["legacy"]),
    ]
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    snap = report["counters"]
    lines = [
        f"## Lock soak ({_reading(report['legacy'])})",
        "",
        summary_line(report),
        "",
        f"- rounds: {report['rounds']} of {report['rounds_planned']}"
        + (" (stopped early on the time budget)" if report["stopped_early"] else ""),
        f"- writers per round: {report['writers']}, stop-cycles per round: {report['cycles']}",
        f"- injected: {report['race_share'] * 100:.0f}% of the lock creates (only while another "
        f"writer really holds the lock) and {report['replace_share'] * 100:.0f}% of the renames",
        f"- seed {report['seed']}, {report['seconds']:.2f} s",
        "",
        "| counter | value |",
        "| --- | ---: |",
    ]
    lines += [f"| {name} | {snap[name]} |" for name in FIELDS]
    lines += ["", "### Findings", ""]
    lines += [f"* {line}" for line in findings(report)]
    if report["errors"]:
        lines += ["", "### Errors", ""]
        lines += [f"* `{line}`" for line in report["errors"]]
    lines += [
        "",
        "Exit is **0** only when nothing was lost and nothing was written beside the lock; "
        "the pre-fix reading is expected to fail, and a run of it that loses nothing is "
        "reported as inconclusive instead of passing.",
        "",
    ]
    return "\n".join(lines)
