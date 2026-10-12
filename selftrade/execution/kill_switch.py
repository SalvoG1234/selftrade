"""The kill switch: one switch that stops new orders, and stays stopped.

The brief is explicit about the sequence when a limit breaks: block new orders,
record why, raise an alert, and follow the configured stop/reduce procedure.  This
module owns the first two and the operator's way back.

Three deliberate choices:

* the state is **persisted**, because a switch that resets on the next process
  start is not a switch;
* an unreadable or corrupt state file is treated as **blocked**, not as "no
  problem found".  Failing open on a risk control is how accounts die;
* when the switch is armed by the *program* (``by="risk_gate"``, which is what the
  paper rail passes) it also keeps the **numbers of that moment** (``detail``): the
  margin the gate saw is a property of the instant it refused the order, and it is the
  first thing anybody asks afterwards.  Only scalars and one small list travel - the
  file stays a state file, not a report.
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


#: Longest string kept in ``detail``, and how many rows its lists may hold.
DETAIL_STRING_CHARS = 200
DETAIL_LIST_ROWS = 8


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _trim(value: Any) -> Any:
    return value[:DETAIL_STRING_CHARS] if isinstance(value, str) else value


def clean_detail(detail: Any) -> dict[str, Any]:
    """Keep the gate's numbers and nothing else: small, JSON-safe and bounded.

    The switch is read by the gate on every order, so it stays a small file: scalars, lists
    of scalars, and lists of flat dicts, with anything else dropped rather than serialised
    blindly.  A list keeps its key even when it ends up empty: the shape of ``detail`` is
    part of the file's contract, and a reader should not have to guess whether a key exists.
    """
    if not isinstance(detail, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if _scalar(value):
            out[str(key)] = _trim(value)
        elif isinstance(value, list):
            values = value[:DETAIL_LIST_ROWS]
            if all(_scalar(item) for item in values):
                out[str(key)] = [_trim(item) for item in values]
                continue
            out[str(key)] = [
                {str(k): _trim(v) for k, v in row.items() if _scalar(v)}
                for row in values
                if isinstance(row, dict)
            ]
    return out


def arming_detail(
    account: Any, checks: list[dict[str, Any]] | None = None, *, alerts: list[str] | None = None
) -> dict[str, Any]:
    """What the gate saw when it armed the switch: the account and the halting limits.

    Built *at the refusal*, because that is the only moment the margin exists: by the time
    a notification goes out, the account may have moved, and "how much room was left?" is
    the question the reader of a HALT will have.  ``account`` is anything with the
    snapshot's attributes (``equity``, ``drawdown``, ``day_pnl_pct``, ``ts``) and ``checks``
    are the gate's own check dicts - only the ones that can halt are kept.
    """
    halting: list[dict[str, Any]] = []
    for check in checks or []:
        if not check.get("arms"):
            continue
        row = {key: check.get(key) for key in ("name", "value", "limit", "headroom", "headroom_frac", "unit")}
        row["ok"] = bool(check.get("ok"))
        halting.append(row)
    return clean_detail(
        {
            "alerts": [str(a) for a in (alerts or [])],
            "equity": getattr(account, "equity", None),
            "drawdown": getattr(account, "drawdown", None),
            "day_pnl_pct": getattr(account, "day_pnl_pct", None),
            "as_of": str(getattr(account, "ts", None) or ""),
            "checks": halting,
        }
    )


class KillSwitch:
    """File-backed, fail-safe blocked/unblocked switch."""

    def __init__(self, path: str | Path = DEFAULT_PATH, *, by: str = "operator"):
        self.path = Path(path)
        self._by = by
        self._cache: dict[str, Any] | None = None

    # ----------------------------------------------------------------- state
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _blocked_state(self, reason: str, triggered_by: str, detail: Any = None) -> dict[str, Any]:
        return {
            "blocked": True,
            "reason": reason,
            "triggered_by": triggered_by,
            "triggered_at": self._now(),
            "detail": clean_detail(detail),
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
                "detail": {},
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
        # A file written before the switch kept its numbers (or by hand) simply has none.
        raw.setdefault("detail", {})
        self._cache = raw
        return dict(raw)

    def is_blocked(self) -> bool:
        return bool(self.state().get("blocked"))

    # --------------------------------------------------------------- actions
    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        self._cache = state

    def trigger(
        self, reason: str, *, by: str = "risk_gate", detail: Any = None
    ) -> dict[str, Any]:
        """Block new orders.  Open positions are *not* touched: that is a documented
        operator procedure, not something this module can do silently.

        ``detail`` is for the program's own arming: the margin the gate saw at the refusal
        (see :func:`arming_detail`).  An operator has no such numbers to pass, and a switch
        armed before this existed simply has none.
        """
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
        new_state = self._blocked_state(reason, by, detail)
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
            "detail": {},
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
            "detail": dict(state.get("detail") or {}),
            "path": str(self.path),
            "events": len(state.get("history") or []),
        }
