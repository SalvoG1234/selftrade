"""The tracked loop as a *process* - started here, so a button can mean something.

``python -m selftrade evolve --session`` is the only thing that honours the session
file: :meth:`selftrade.session.Session.checkpoint` is called at every generation
boundary, and that is where ``pause``, ``resume`` and ``stop`` take effect.  Writing a
request into ``state/session.json`` while no such process is running is how a control
panel looks responsive and does nothing, so :class:`LoopRunner` owns the child:

* ``start`` **launches the loop** and points at its pid, instead of announcing a run
  nobody is performing;
* ``pause``/``resume``/``stop`` are refused when nothing is running, because a request
  in a file no one reads is not a control;
* the child's stdout+stderr go to ``state/loop.log`` (next to the session file), so
  ``start`` can be explained afterwards by a log rather than a shrug.

Two faces, one implementation: the dashboard's buttons and ``selftrade session``.
The loop is the *research* loop - it evaluates genomes, it never places an order.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .session import Session

LOOP_LOG_NAME = "loop.log"

#: How long ``stop`` waits for a loop *this process started* before answering.  A stop is
#: delivered at the next generation boundary - the run finishes the generation it is in
#: and still writes its report - so this is a courtesy wait, never a kill.
STOP_GRACE_S = 2.0

#: ``GetExitCodeProcess`` answers this for a process that has not exited yet.
_STILL_ACTIVE = 259


def loop_log_path(settings: Any) -> Path:
    """Where the loop's output goes: next to the session file it obeys."""
    session_file = getattr(settings, "session_file", None) or "state/session.json"
    return Path(session_file).parent / LOOP_LOG_NAME


def loop_argv(generations: int, config_path: str | None = None) -> list[str]:
    """The command line that runs the tracked loop, in *this* interpreter.

    ``-u`` because the output is a log a human reads while the run is in progress, and a
    block-buffered child would show nothing until it finished.
    """
    argv = [sys.executable, "-u", "-m", "selftrade"]
    if config_path:
        argv += ["--config", str(config_path)]
    return argv + ["evolve", "--session", "--generations", str(int(generations))]


def pid_alive(pid: int | None) -> bool | None:
    """Is ``pid`` alive?  ``None`` when this platform cannot say.

    ``os.kill(pid, 0)`` is a probe on POSIX but **terminates the process** on Windows, so
    the probe there is ``OpenProcess`` + ``GetExitCodeProcess`` instead.  Answering
    ``None`` rather than guessing matters: a loop whose pid is unknowable is not reported
    as running, and a live one is never checked with something that could kill it.
    """
    if pid in (None, ""):
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it exists; it is just not ours
        return True
    except OSError:
        return None
    return True


def _windows_pid_alive(pid: int) -> bool | None:  # pragma: no cover - Windows only
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _spawn_kwargs() -> dict[str, Any]:
    """Detach the loop from this console.

    The loop is meant to outlive the click that started it (and a dashboard is restarted
    far more often than a run finishes): on Windows its own process *group*, so a Ctrl+C
    in the dashboard's console does not take a generation down mid-flight; on POSIX its
    own session, for the same reason.  Stopping it is an explicit action - the button,
    or ``session stop``.
    """
    if os.name == "nt":  # pragma: no cover - Windows only
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


class LoopRunner:
    """Starts the tracked loop, and reports honestly on the one that is running."""

    def __init__(
        self,
        settings: Any,
        config_path: str | None = None,
        argv_builder: Callable[[int], list[str]] | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.session_file = getattr(settings, "session_file", "state/session.json")
        self.config_path = config_path
        self.log_path = loop_log_path(settings)
        self._argv_builder = argv_builder
        self._popen = popen or subprocess.Popen
        self._child: Any = None
        self._log: Any = None

    # --------------------------------------------------------------- plumbing
    def argv(self, generations: int | None = None) -> list[str]:
        total = int(generations) if generations else int(self.settings.evolution.generations)
        if self._argv_builder is not None:
            return list(self._argv_builder(total))
        return loop_argv(total, config_path=self.config_path)

    def close(self) -> None:
        """Release the log handle (the loop itself keeps running)."""
        self._close_log()

    def _close_log(self) -> None:
        log, self._log = self._log, None
        if log is not None:
            try:
                log.close()
            except OSError:  # pragma: no cover - a log we already lost
                pass

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        """What the loop is doing, from two sources that can disagree.

        The session file is the *record* - any loop, started here or in a terminal - and
        the child handle is the truth about the one this process started.  A file that
        still says ``running`` while its pid is gone is a loop that died; reporting that
        as running is exactly how a panel lies, so the pid decides.
        """
        session = Session(self.session_file).read()
        child = self._child
        owned = child is not None and child.poll() is None
        if child is not None and not owned:
            self._close_log()
        pid = session.get("pid")
        alive = True if owned else pid_alive(pid)
        running = bool(owned or (session.get("status") == "running" and alive is True))
        return {
            "running": running,
            "owned": bool(owned),
            "pid": pid if running else None,
            "status": session.get("status"),
            "pending": session.get("pending"),
            "generation": session.get("generation"),
            "generations_done": session.get("generations_done"),
            "generations_total": session.get("generations_total"),
            "started_at": session.get("started_at"),
            "heartbeat": session.get("heartbeat"),
            # only the child we started has an exit code to report; a loop from a
            # terminal reports `null` rather than pretending to a verdict
            "exit_code": child.returncode if (child is not None and not owned) else None,
            "log": str(self.log_path),
            "checked_at": _now(),
        }

    # ---------------------------------------------------------------- actions
    def start(self, generations: int | None = None) -> tuple[int, dict[str, Any]]:
        """Launch the loop; refuse if one is already running."""
        state = self.status()
        if state["running"]:
            return 409, {
                "ok": False,
                "action": "start",
                "error": f"the loop is already running (pid {state['pid']})",
                "loop": state,
            }
        total = int(generations) if generations else int(self.settings.evolution.generations)
        argv = self.argv(total)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(self.log_path, "ab", buffering=0)
        except OSError as exc:
            return 500, {
                "ok": False,
                "action": "start",
                "error": f"cannot write the loop log {self.log_path}: {exc}",
                "loop": state,
            }
        try:
            log.write(f"\n=== loop started {_now()} | {total} generations | "
                      f"{' '.join(argv)} ===\n".encode("utf-8"))
        except OSError:  # pragma: no cover - the header is a convenience
            pass
        session = Session(self.session_file)
        # The file says "starting" before the child says anything: `status` between the
        # spawn and the child's own `mark_running` must not read idle.
        session.request_start(total)
        try:
            child = self._popen(
                argv,
                cwd=os.getcwd(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **_spawn_kwargs(),
            )
        except OSError as exc:
            log.close()
            session.mark_finished("error")
            return 500, {
                "ok": False,
                "action": "start",
                "error": f"could not start the loop: {exc}",
                "loop": self.status(),
            }
        self._close_log()
        self._log = log
        self._child = child
        session.mark_running(total, pid=child.pid)
        # one answer shape for every action: `ok`, what was asked, the detail that action
        # has (`command` here, `note` for the others, `error` when refused) and `loop`.
        return 200, {
            "ok": True,
            "action": "start",
            "command": " ".join(argv),
            "loop": self.status(),
        }

    def request(self, action: str) -> tuple[int, dict[str, Any]]:
        """Deliver ``pause``/``resume``/``stop`` to the running loop, or refuse.

        Refusing is the point: the request is honoured by a loop at its next generation
        boundary, so with no loop there is nothing to honour it and saying ``ok`` would
        be a lie the operator only discovers at three in the morning.
        """
        state = self.status()
        if not state["running"]:
            return 409, {
                "ok": False,
                "action": action,
                "error": f"no loop is running - there is nothing to {action}",
                "loop": state,
            }
        Session(self.session_file).request(action)
        if action == "stop":
            child = self._child if state["owned"] else None
            stopped = child is not None and self._wait_exit(child, STOP_GRACE_S)
            note = (
                "the loop stopped and wrote its report"
                if stopped
                else "stop requested: the run finishes the generation it is in, then writes its report"
            )
        elif action == "pause":
            note = "pause requested: it takes effect at the next generation boundary"
        else:
            note = "resume requested: a paused loop continues at its next boundary"
        return 200, {
            "ok": True,
            "action": action,
            "note": note,
            "loop": self.status(),
        }

    def _wait_exit(self, child: Any, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if child.poll() is not None:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
