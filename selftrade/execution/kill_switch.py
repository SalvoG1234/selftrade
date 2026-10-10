"""The kill switch: one switch that stops new orders, and stays stopped.

The brief is explicit about the sequence when a limit breaks: block new orders,
record why, raise an alert, and follow the configured stop/reduce procedure.  This
module owns the first two and the operator's way back.

Two deliberate choices:

* the state is **persisted**, because a switch that resets on the next process
  start is not a switch;
* an unreadable or corrupt state file is treated as **blocked**, not as "no
  problem found".  Failing open on a risk control is how accounts die.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATH = "state/kill_switch.json"


class KillSwitchError(Exception):
    """Raised for illegal kill-switch operations."""


def kill_switch_path(settings: Any = None, filename: str = "kill_switch.json") -> Path:
    """Where the switch lives: next to the session file (``state/`` by default)."""
    session_file = getattr(settings, "session_file", None)
    if session_file:
        return Path(session_file).parent / filename
    return Path(DEFAULT_PATH)


class KillSwitch:
    """File-backed, fail-safe blocked/unblocked switch."""

    def __init__(self, path: str | Path = DEFAULT_PATH, *, by: str = "operator"):
        self.path = Path(path)
        self._by = by
        self._cache: dict[str, Any] | None = None

    # ----------------------------------------------------------------- state
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _blocked_state(self, reason: str, triggered_by: str) -> dict[str, Any]:
        return {
            "blocked": True,
            "reason": reason,
            "triggered_by": triggered_by,
            "triggered_at": self._now(),
            "history": [],
        }

    def state(self) -> dict[str, Any]:
        """Read the switch.  A corrupt file counts as blocked (fail-safe)."""
        if self._cache is not None:
            return dict(self._cache)
        if not self.path.exists():
            self._cache = {
                "blocked": False,
                "reason": "",
                "triggered_by": "",
                "triggered_at": None,
                "history": [],
            }
            return dict(self._cache)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or "blocked" not in raw:
                raise ValueError("missing 'blocked' key")
        except (OSError, ValueError) as exc:
            self._cache = {
                "blocked": True,
                "reason": f"kill switch state unreadable ({exc}): blocked as a precaution",
                "triggered_by": "fail-safe",
                "triggered_at": self._now(),
                "history": [],
            }
            return dict(self._cache)
        raw.setdefault("history", [])
        self._cache = raw
        return dict(raw)

    def is_blocked(self) -> bool:
        return bool(self.state().get("blocked"))

    # --------------------------------------------------------------- actions
    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        self._cache = state

    def trigger(self, reason: str, *, by: str = "risk_gate") -> dict[str, Any]:
        """Block new orders.  Open positions are *not* touched: that is a documented
        operator procedure, not something this module can do silently."""
        if not reason:
            raise KillSwitchError("a kill switch trigger needs a reason")
        state = self.state()
        history = list(state.get("history") or [])
        if state.get("blocked"):
            # Already engaged: keep the *first* cause, which is the one that matters,
            # and note the repeat instead of burying it under a nested reason.
            history.append({"at": self._now(), "event": "trigger_again", "reason": reason, "by": by})
            state = dict(state, history=history[-20:])
            self._write(state)
            return dict(state)
        history.append({"at": self._now(), "event": "trigger", "reason": reason, "by": by})
        new_state = self._blocked_state(reason, by)
        new_state["history"] = history[-20:]
        self._write(new_state)
        return dict(new_state)

    def reset(self, reason: str = "", *, by: str = "operator") -> dict[str, Any]:
        """Unblock.  Always explicit, always with an audit trail."""
        state = self.state()
        history = list(state.get("history") or [])
        history.append(
            {
                "at": self._now(),
                "event": "reset",
                "reason": reason or "operator reset",
                "by": by,
            }
        )
        new_state = {
            "blocked": False,
            "reason": "",
            "triggered_by": "",
            "triggered_at": None,
            "history": history[-20:],
        }
        self._write(new_state)
        return dict(new_state)

    def describe(self) -> dict[str, Any]:
        state = self.state()
        return {
            "blocked": bool(state.get("blocked")),
            "reason": state.get("reason", ""),
            "triggered_by": state.get("triggered_by", ""),
            "triggered_at": state.get("triggered_at"),
            "path": str(self.path),
            "events": len(state.get("history") or []),
        }
