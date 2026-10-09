"""Session control via a small JSON file.

The evolution loop calls :meth:`Session.checkpoint` at every generation
boundary.  A ``pause`` request really freezes progress (the checkpoint blocks
until a ``resume`` or ``stop`` arrives); ``stop`` closes the loop, which still
writes the report.

A corrupt control file never kills a run: :meth:`Session.read` degrades to
defaults instead of raising.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

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

    def write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["updated_at"] = time.time()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    # -------------------------------------------------- requests
    def request(self, action: str) -> dict[str, Any]:
        state = self.read()
        state["pending"] = action
        state["last_request"] = {"action": action, "ts": time.time()}
        self.write(state)
        return state

    def request_start(self, generations: int, pid: int | None = None) -> dict[str, Any]:
        state = self.read()
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
        self.write(state)
        return state

    def mark_running(self, generations: int, pid: int | None = None) -> dict[str, Any]:
        state = self.read()
        state.update(
            {
                "status": "running",
                "pid": os.getpid() if pid is None else pid,
                "generation": 0,
                "generations_done": 0,
                "generations_total": int(generations),
                "pending": None,
                "started_at": time.time(),
            }
        )
        self.write(state)
        return state

    def mark_finished(self, status: str = "done") -> dict[str, Any]:
        state = self.read()
        state["status"] = status
        state["pending"] = None
        self.write(state)
        return state

    def status(self) -> dict[str, Any]:
        return self.read()

    # -------------------------------------------------- checkpoint
    def checkpoint(self, generation: int, total: int) -> str:
        """Called at a generation boundary.  Returns ``continue`` or ``stop``.

        Honours ``pause`` by blocking until ``resume`` or ``stop``.
        """
        state = self.read()
        pending = state.get("pending")

        if pending == "stop":
            state.update({"status": "stopped", "pending": None})
            self.write(state)
            return "stop"
        if pending == "pause":
            state.update({"status": "paused", "pending": None})
            self.write(state)
            # Block here: this is a real freeze, not just a flag.
            while True:
                time.sleep(self.poll_interval)
                cur = self.read()
                if cur.get("pending") == "resume":
                    cur.update({"status": "running", "pending": None})
                    self.write(cur)
                    break
                if cur.get("pending") == "stop":
                    cur.update({"status": "stopped", "pending": None})
                    self.write(cur)
                    return "stop"

        state = self.read()
        state.update(
            {
                "status": "running",
                "generation": int(generation),
                "generations_done": int(generation) + 1,
                "generations_total": int(total),
                "heartbeat": time.time(),
            }
        )
        self.write(state)
        return "continue"
