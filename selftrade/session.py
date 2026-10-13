"""Session control via a small JSON file.

The evolution loop calls :meth:`Session.checkpoint` at every generation
boundary.  A ``pause`` request really freezes progress (the checkpoint blocks
until a ``resume`` or ``stop`` arrives); ``stop`` closes the loop, which still
writes the report.

A corrupt control file never kills a run: :meth:`Session.read` degrades to
defaults instead of raising.

**Two writers, one file.**  The loop records its heartbeat at every boundary and the
operator writes a request from another process (the dashboard's buttons, ``session stop``).
Both are read-modify-write, so without coordination the loop can write back the snapshot it
read *before* a ``stop`` arrived - the click then does nothing, which is the one failure a
control panel must not have.  Every write therefore happens under a cooperative lock file
(see :meth:`Session._locked`): no dependency, no server, and never a way to wedge a run.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

#: How long a read-modify-write waits for the lock before giving up and writing anyway.
#: The holder is a *state write* (microseconds), so this is a safety valve, never a wait.
LOCK_TIMEOUT_S = 2.0
LOCK_POLL_S = 0.01
#: A lock file older than this is a process that died holding it: break it and carry on.
LOCK_STALE_S = 30.0
#: Windows refuses a rename while a reader holds the destination open (no share-delete),
#: which is what ``Path.read_text`` takes, and the readers here are in other processes.
#: The window is a few instructions wide, so the rename is retried briefly - and only for
#: that error: a real permission problem must still surface.
REPLACE_RETRIES = 40
REPLACE_BACKOFF_S = 0.005
#: Windows: ERROR_ACCESS_DENIED (5) and ERROR_SHARING_VIOLATION (32).
SHARING_WINERRORS = (5, 32)

DEFAULT_STATE: dict[str, Any] = {
    "status": "idle",
    "pid": None,
    "generation": 0,
    "generations_done": 0,
    "generations_total": 0,
    "pending": None,
    "started_at": None,
    "updated_at": None,
    "heartbeat": None,
    "last_request": None,
}


def replace_file(tmp: Path, dest: Path) -> None:
    """Rename ``tmp`` over ``dest``, tolerating a reader that holds it open.

    ``os.replace`` is atomic, but on Windows it *fails* - instead of succeeding the way
    POSIX does - when another handle has the destination open without share-delete, which
    is exactly the handle ``Path.read_text`` takes.  That reader is real here: the loop
    writes this file while the dashboard process polls it, so a sharing violation would
    abort a state write that has already been prepared, on the platform this project
    targets.  The retry is bounded (a few tens of milliseconds) and covers only that
    error, so a genuine permission problem is reported at once.
    """
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in SHARING_WINERRORS:
                raise
            if attempt + 1 >= REPLACE_RETRIES:
                raise
            time.sleep(REPLACE_BACKOFF_S)


class Session:
    def __init__(self, path: str | Path = "state/session.json", poll_interval: float = 0.2):
        self.path = Path(path)
        self.poll_interval = poll_interval

    # -------------------------------------------------- io
    def read(self) -> dict[str, Any]:
        """Read the control file; a corrupt/missing file yields defaults."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            state = dict(DEFAULT_STATE)
            return state
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            state = dict(DEFAULT_STATE)
            state["corrupt"] = True
            return state
        if not isinstance(data, dict):
            state = dict(DEFAULT_STATE)
            state["corrupt"] = True
            return state
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        """Replace the file atomically, under the lock."""
        with self._locked():
            return self._write_unlocked(state)

    def _write_unlocked(self, state: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["updated_at"] = time.time()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        replace_file(tmp, self.path)
        return state

    def update(self, apply: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """Read-modify-write as one indivisible step, so no request can be lost.

        The read happens *inside* the lock: a ``stop`` the operator wrote can only land
        before this step (and is then read, and preserved) or after it (and is then on
        disk).  It can no longer be overwritten by a snapshot taken before it arrived.
        """
        with self._locked():
            return self._write_unlocked(apply(dict(self.read())))

    # -------------------------------------------------- lock
    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _lock_is_stale(self) -> bool:
        try:
            return (time.time() - self.lock_path.stat().st_mtime) > LOCK_STALE_S
        except OSError:
            return False

    def _drop_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:  # pragma: no cover - already gone, or held open elsewhere
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the cooperative lock for the duration of a read-modify-write.

        The lock is the *existence* of ``<session file>.lock``, created with ``O_EXCL``:
        the first process through wins and the others retry, which works across processes
        with no dependency and no server.  Nothing here may wedge a run - a lock held by a
        process that died is broken after :data:`LOCK_STALE_S`, and a holder that outlives
        :data:`LOCK_TIMEOUT_S` is written past rather than waited on (the file itself is
        still replaced atomically, so a reader never sees half a state).

        The existence of the file, not the errno of the create, is what decides whether the
        lock is held: on Windows a lost create race is reported as ``EACCES``.
        """
        acquired = False
        broke_stale = False
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while not acquired:
            try:
                os.close(os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                acquired = True
            except (FileExistsError, PermissionError):
                # Windows answers a *lost create race* with EACCES (``PermissionError``)
                # instead of ``EEXIST``: two writers reach for the same new lock file at the
                # same instant and the loser is told the name is not available.  Read as
                # "this directory is not writable" it sent the writer on *without* the lock,
                # and two unlocked read-modify-writes is precisely how an operator's ``stop``
                # gets erased (measured: 6 rounds in 40, plus writers colliding on the same
                # temporary file).  Even checking whether the lock file exists before
                # deciding is not safe - the holder can drop it between the failed create and
                # the check, and that hole still swallowed stops.  So the error is always
                # read as "held": a persistent EACCES from a state directory we cannot write
                # to now waits out the deadline instead of failing instantly, and then fails
                # in the temporary write anyway, which is where that error belongs.
                # Once only: a lock we cannot remove is one we must stop retrying, or the
                # loop spins here forever instead of trading.
                if not broke_stale and self._lock_is_stale():
                    broke_stale = True
                    self._drop_lock()
                    continue
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_S)
            except OSError:  # pragma: no cover - anything else is not a lock we can take
                break
        try:
            yield
        finally:
            if acquired:
                self._drop_lock()

    # -------------------------------------------------- requests
    def request(self, action: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state["pending"] = action
            state["last_request"] = {"action": action, "ts": time.time()}
            return state

        return self.update(apply)

    def request_start(self, generations: int, pid: int | None = None) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state.update(
                {
                    "status": "running",
                    "pid": pid,
                    "generation": 0,
                    "generations_done": 0,
                    "generations_total": int(generations),
                    "pending": "start",
                    "started_at": time.time(),
                }
            )
            return state

        return self.update(apply)

    def mark_running(self, generations: int, pid: int | None = None) -> dict[str, Any]:
        """Record that a loop is running now (``start`` is the request this answers).

        Only a ``start`` request is cleared here: a ``pause``/``stop`` written between the
        spawn and this line is the operator's, and silently dropping it is how a clicked
        button does nothing.
        """

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            pending = state.get("pending")
            state.update(
                {
                    "status": "running",
                    "pid": os.getpid() if pid is None else pid,
                    "generation": 0,
                    "generations_done": 0,
                    "generations_total": int(generations),
                    "pending": pending if pending in ("pause", "resume", "stop") else None,
                    "started_at": time.time(),
                }
            )
            return state

        return self.update(apply)

    def mark_finished(self, status: str = "done") -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state["status"] = status
            state["pending"] = None
            return state

        return self.update(apply)

    def status(self) -> dict[str, Any]:
        return self.read()

    # -------------------------------------------------- checkpoint
    def checkpoint(self, generation: int, total: int) -> str:
        """Called at a generation boundary.  Returns ``continue`` or ``stop``.

        Honours ``pause`` by blocking until ``resume`` or ``stop``.  Every state change is
        a locked read-modify-write, so a request that arrives while this generation is
        running is never overwritten by the snapshot this call started from.
        """
        verdict = self._take_pending()
        if verdict == "stop":
            return "stop"
        if verdict == "pause" and self._frozen_until_resumed():
            return "stop"

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state.update(
                {
                    "status": "running",
                    "generation": int(generation),
                    "generations_done": int(generation) + 1,
                    "generations_total": int(total),
                    "heartbeat": time.time(),
                }
            )
            return state

        self.update(apply)
        return "continue"

    def _take_pending(self) -> str | None:
        """Consume a ``stop``/``pause`` request and record the status it implies."""
        with self._locked():
            state = self.read()
            pending = state.get("pending")
            if pending not in ("stop", "pause"):
                return None
            state.update({"status": "stopped" if pending == "stop" else "paused", "pending": None})
            self._write_unlocked(state)
        return str(pending)

    def _frozen_until_resumed(self) -> bool:
        """Block while paused; True when a ``stop`` arrived instead of a ``resume``."""
        while True:
            time.sleep(self.poll_interval)
            with self._locked():
                state = self.read()
                pending = state.get("pending")
                if pending in ("resume", "stop"):
                    state.update(
                        {
                            "status": "running" if pending == "resume" else "stopped",
                            "pending": None,
                        }
                    )
                    self._write_unlocked(state)
                    return pending == "stop"
