"""Approvals: the seven cases the brief says a human always decides.

An agent may write anything; it may only *act* when the approval manager says
so.  :meth:`ApprovalManager.needs_human` answers one question - does this have to
wait for a person? - and the answer is yes for:

1. **security questions** and anything the policy engine held for review;
2. **important criticism or controversy** (a reply to a hostile thread, an
   ``limits`` item that names a real weakness);
3. **unusual financial or performance statements** (the policy engine flags
   every performance term);
4. **changes to the autonomy limits themselves** - no agent may widen its own
   freedom, so a settings change is always a human action recorded in the audit
   log *with the old and new value*;
5. **replies that could commit the project** to something (any outbound reply,
   in every mode, until the operator turns replies on);
6. **destructive operations** (deleting history, revoking credentials);
7. **permission and credential changes**.

The pending queue is a database table, so a decision survives a restart, and the
same item cannot be silently re-decided: :meth:`decide` refuses to change a row
that is no longer pending.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import obs
from .db import SocialDB
from .models import AutonomyMode, Risk
from .policy import ACTION_PUBLISH, PolicyReport

#: Reasons that force a human decision regardless of mode or risk level.
FORCED_REASONS = {
    "security": "a security question is involved",
    "controversy": "the content answers criticism or takes a position",
    "financial": "a performance or capital statement is involved",
    "commitment": "the reply could commit the project to something",
    "permissions": "the change touches permissions or credentials",
    "destructive": "the operation destroys data or history",
    "autonomy": "the change modifies the autonomy limits",
    "reply": "outbound replies are held for a human in every mode",
}


@dataclass
class ApprovalVerdict:
    needed: bool
    reason: str = ""
    risk: str = str(Risk.LOW)
    approval_id: int | None = None
    state: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "reason": self.reason,
            "risk": self.risk,
            "approval_id": self.approval_id,
            "state": self.state,
        }


class ApprovalManager:
    def __init__(self, settings: Any, db: SocialDB, *, notify: Any = None):
        self.settings = settings
        self.db = db
        self.notify = notify

    # --------------------------------------------------------------- decisions
    def needs_human(
        self,
        item: dict[str, Any] | None,
        *,
        report: PolicyReport | None = None,
        action: str = ACTION_PUBLISH,
        kind: str = "content",
        reasons: list[str] | None = None,
    ) -> ApprovalVerdict:
        """Whether this action must wait for a person, and why."""
        reasons = list(reasons or [])
        item = item or {}
        risk = str(item.get("risk") or Risk.LOW)
        item_id = int(item.get("id")) if item.get("id") is not None else None

        for key in reasons:
            if key in FORCED_REASONS:
                return self._verdict(True, FORCED_REASONS[key], risk, item_id)

        if kind == "reply":
            if not self.settings.autonomy.allow_replies:
                return self._verdict(True, FORCED_REASONS["reply"], risk, item_id)
            reasons.append("reply")

        if action == ACTION_PUBLISH and self.settings.mode() == AutonomyMode.DRAFT:
            return self._verdict(
                True, "autonomy mode A (draft): a human publishes", risk, item_id
            )
        if action in {"schedule", "publish"} and self.settings.mode() == AutonomyMode.ADVANCED:
            # Mode C may publish ordinary content; the risk list still applies.
            if risk in {str(r) for r in self.settings.autonomy.require_approval_risks}:
                return self._verdict(
                    True, f"{risk}-risk content needs a human in every mode", risk, item_id
                )
        if risk in {str(r) for r in self.settings.autonomy.require_approval_risks}:
            return self._verdict(True, f"{risk}-risk content", risk, item_id)
        if report is not None and (report.blocked or report.needs_review):
            return self._verdict(True, "; ".join(report.reasons[:2]) or "policy held it",
                                 risk, item_id)
        if item.get("state") == "needs_review":
            return self._verdict(True, "the item was flagged for review", risk, item_id)
        return self._verdict(False, "", risk, item_id)

    def _verdict(self, needed: bool, reason: str, risk: str, item_id: int | None,
                 ) -> ApprovalVerdict:
        state = self.db.approval_state("content_item", item_id) if item_id else "none"
        return ApprovalVerdict(needed=needed, reason=reason, risk=risk, state=state)

    def ensure_request(
        self,
        item_id: int,
        *,
        risk: str | Risk,
        reason: str,
        actor: str = "agent",
    ) -> int:
        """Create the pending row once (idempotent) and tell the operator."""
        before = self.db.approval_state("content_item", item_id)
        approval_id = self.db.request_approval(
            "content_item", item_id, risk=risk, reason=reason, requested_by=actor
        )
        if before == "none" and self.notify is not None:
            try:
                self.notify.approval_requested(item_id, reason, str(risk))
            except Exception as exc:  # noqa: BLE001 - a notification must not block
                obs.warning("approval.notify_failed", detail={"error": type(exc).__name__})
        return approval_id

    def decide(
        self,
        approval_id: int,
        decision: str,
        *,
        decided_by: str = "operator",
        note: str = "",
    ) -> dict[str, Any]:
        row = self.db.get("approval", approval_id)
        if row is None:
            raise KeyError(f"approval {approval_id} not found")
        if row["state"] != "pending":
            raise ValueError(f"approval {approval_id} is already {row['state']}")
        if decision not in {"approved", "rejected", "expired"}:
            raise ValueError(f"unknown decision {decision!r}")
        updated = self.db.decide_approval(
            approval_id, decision, decided_by=decided_by, note=note
        )
        if decision == "approved" and row["subject_type"] == "content_item":
            self.db.set_item_state(int(row["subject_id"]), "approved", notes=note or None)
        elif decision == "rejected" and row["subject_type"] == "content_item":
            self.db.set_item_state(int(row["subject_id"]), "rejected", notes=note or None)
        obs.info(
            f"approval.{decision}",
            detail={"approval_id": approval_id, "by": decided_by},
        )
        return updated

    def approve(self, item_id: int, *, note: str = "", actor: str = "operator") -> dict[str, Any]:
        row = self.db.approval_for("content_item", item_id)
        if row is None:
            approval_id = self.ensure_request(
                item_id, risk=str(self.db.get("content_item", item_id) or {}).get("risk", "low"),
                reason="approved directly by the operator",
            )
            row = self.db.get("approval", approval_id)
        if row["state"] != "pending":
            self.db.set_item_state(item_id, "approved", notes=note or None)
            return row
        return self.decide(int(row["id"]), "approved", decided_by=actor, note=note)

    def reject(self, item_id: int, *, note: str = "", actor: str = "operator") -> dict[str, Any]:
        row = self.db.approval_for("content_item", item_id)
        if row is None or row["state"] != "pending":
            self.db.set_item_state(item_id, "rejected", notes=note or None)
            self.db.audit("content.rejected", actor=actor, subject=f"content_item:{item_id}",
                          detail={"note": note})
            return row or {"subject_id": item_id, "state": "rejected"}
        return self.decide(int(row["id"]), "rejected", decided_by=actor, note=note)

    # ------------------------------------------------------------- settings # 7
    def request_setting_change(self, key: str, new_value: Any, *, actor: str = "agent",
                               reason: str = "") -> int:
        """Autonomy and permission changes are *proposals*, never applied by an agent."""
        approval_id = self.db.request_approval(
            "policy_change", 0, risk=str(Risk.SENSITIVE),
            reason=reason or FORCED_REASONS["autonomy"], requested_by=actor,
        )
        self.db.set_setting(
            f"pending_setting:{approval_id}", {"key": key, "value": new_value},
            actor=actor,
        )
        return approval_id

    # ------------------------------------------------------------------- views
    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.approvals(state="pending", limit=limit)

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return [row for row in self.db.approvals(limit=limit) if row["state"] != "pending"]

    def counts(self) -> dict[str, int]:
        rows = self.db.all("SELECT state, COUNT(*) AS n FROM approval GROUP BY state")
        out = {"pending": 0, "approved": 0, "rejected": 0, "expired": 0}
        for row in rows:
            out[str(row["state"])] = int(row["n"])
        return out
