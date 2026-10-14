"""The publisher: the only component allowed to send anything to a platform.

Everything else in this project proposes.  This module executes, and it does so
under three rules that are worth more than the feature list:

**1. Four gates, in order.**  For a live send: the policy engine must return
``pass``, the approval manager must not require a human (or an approval must
exist), the connector must be *ready* (credentials present, write scope known),
and the text must not already be claimed.  Any gate failing ends the attempt with
a recorded reason - it never "tries anyway".

**2. At-most-once, by claim.**  Before the API call the row is written as
``publishing`` and the ``dedupe_key`` is unique.  A second worker, a retry after
a crash, or a doubled cron will hit the unique index and stop.  A crash between
the claim and the call leaves a ``publishing`` row: the dashboard shows it as
*unknown - needs inspection*, which is the honest state.  Guessing would risk
posting twice, and this project would rather be asked than be wrong.

**3. Simulation is a first-class outcome.**  With ``simulate=True`` (the default
until a connector is configured, and always in Mode A) the run still executes
every static check, still records what a real publish *would* have decided, and
writes the row with ``state='dry_run'``.  The dashboard counts those separately
from published posts, so "nothing has ever been posted" is always visible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import obs
from .approvals import ApprovalManager
from .db import SocialDB, to_ts, utcnow
from .models import PolicyOutcome, Platform
from .policy import ACTION_DRAFT, ACTION_PUBLISH, PolicyEngine, kill_switch_engaged


@dataclass
class ExternalPost:
    """What a platform gave back.  An empty ``external_id`` means: not posted."""

    external_id: str = ""
    url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    ok: bool
    platform: str
    variant_id: int
    state: str                     # published | dry_run | blocked | needs_review | failed | unconfigured
    external_id: str = ""
    url: str = ""
    reason: str = ""
    idempotent: bool = False
    policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "platform": self.platform,
            "variant_id": self.variant_id,
            "state": self.state,
            "external_id": self.external_id,
            "url": self.url,
            "reason": self.reason,
            "idempotent": self.idempotent,
            "policy_outcome": self.policy.get("outcome"),
        }


class Publisher:
    def __init__(
        self,
        settings: Any,
        db: SocialDB,
        connectors: dict[str, Any] | None = None,
        *,
        notify: Any = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.db = db
        self.connectors = {str(k): v for k, v in (connectors or {}).items()}
        self.policy = PolicyEngine(settings, db, now=now)
        self.approvals = ApprovalManager(settings, db, notify=notify)
        self.notify = notify
        self._now = now or utcnow

    # ------------------------------------------------------------------ status
    def kill_switch(self) -> bool:
        return kill_switch_engaged(self.db, self.settings)

    def connector_state(self, platform: str) -> dict[str, Any]:
        connector = self.connectors.get(str(platform))
        if connector is None:
            return {"platform": str(platform), "configured": False, "ready": False,
                    "reason": "no connector wired for this platform"}
        if hasattr(connector, "readiness"):
            return {"configured": True, **connector.readiness()}
        return {"platform": str(platform), "configured": True, "ready": False,
                "reason": "connector has no readiness()"}

    def ready_platforms(self, *, live: bool) -> list[str]:
        out: list[str] = []
        for platform in self.connectors:
            state = self.connector_state(platform)
            ready = bool(state.get("ready")) and not bool(state.get("dry_run", True))
            if ready or not live:
                out.append(platform)
        return sorted(out)

    # --------------------------------------------------------------- publishing
    def publish_variant(
        self,
        variant_id: int,
        *,
        simulate: bool | None = None,
        actor: str = "publisher",
        scheduled_id: int | None = None,
    ) -> PublishResult:
        variant = self.db.variant(variant_id)
        if variant is None:
            raise KeyError(f"variant {variant_id} not found")
        platform = str(variant["platform"])
        item = variant.get("item")
        account = self.db.account_for(platform)
        simulate = self._default_simulate(platform) if simulate is None else bool(simulate)

        # Gate 0: is this a retry?  The claim is checked before anything else, so a
        # worker that restarts mid-job reports "already published" instead of a
        # policy refusal - and, more importantly, cannot post twice.
        if not simulate:
            account_id = int((account or {}).get("id") or 0)
            existing = self.db.one(
                "SELECT id, state, url FROM published_post WHERE dedupe_key = ?",
                [f"live:{platform}:{account_id}:{variant['content_hash']}"],
            )
            if existing and existing["state"] in {"published", "publishing"}:
                obs.info("publish.retry_noop", platform=platform,
                         detail={"variant_id": variant_id, "state": existing["state"]})
                self._finish_schedule(scheduled_id, "published", "already claimed")
                return PublishResult(
                    ok=True, platform=platform, variant_id=variant_id,
                    state="published", idempotent=True, url=str(existing.get("url") or ""),
                    reason=f"this exact text was already claimed (post {existing['id']}, "
                           f"state {existing['state']}): nothing was sent again",
                )

        # Gate 1: policy.  A simulation is judged as a draft (there is nothing to
        # authorise), but the *live* verdict is recorded too, so the operator can
        # see exactly what would stop a real post.
        live_report = self.policy.check(
            variant, action=ACTION_PUBLISH, account=account, item=item, now=self._now()
        )
        if simulate:
            draft_report = self.policy.check(
                variant, action=ACTION_DRAFT, account=account, item=item, now=self._now()
            )
            static_ok = draft_report.outcome == str(PolicyOutcome.PASS)
            if not static_ok:
                self._audit_refusal(variant_id, "simulation_refused", draft_report.reasons)
                self._finish_schedule(scheduled_id, "blocked", "; ".join(draft_report.reasons))
                return PublishResult(
                    ok=False, platform=platform, variant_id=variant_id, state="blocked",
                    reason="; ".join(draft_report.reasons[:3]), policy=live_report.to_dict(),
                )
            result = self._simulate(variant, account, live_report, scheduled_id)
            self._finish_schedule(scheduled_id, "published" if result.ok else "failed",
                                  result.reason or "simulated")
            return result

        if live_report.outcome != str(PolicyOutcome.PASS):
            state = "blocked" if live_report.outcome == str(PolicyOutcome.BLOCK) else "needs_review"
            if state == "needs_review":
                self.approvals.ensure_request(
                    int(item["id"]) if item else 0,
                    risk=str((item or {}).get("risk", "medium")),
                    reason="; ".join(live_report.reasons[:2]),
                )
            self._audit_refusal(variant_id, f"publish_{state}", live_report.reasons)
            self._finish_schedule(scheduled_id, state, "; ".join(live_report.reasons[:3]))
            if self.notify is not None and state == "blocked":
                try:
                    self.notify.policy_blocked(variant_id, live_report.reasons)
                except Exception:  # noqa: BLE001
                    pass
            return PublishResult(
                ok=False, platform=platform, variant_id=variant_id, state=state,
                reason="; ".join(live_report.reasons[:3]), policy=live_report.to_dict(),
            )

        # Gate 2: connector readiness.
        state = self.connector_state(platform)
        if not bool(state.get("ready")):
            reason = _why_not_ready(state)
            self._audit_refusal(variant_id, "publish_unconfigured", [reason])
            self._finish_schedule(scheduled_id, "blocked", reason)
            return PublishResult(
                ok=False, platform=platform, variant_id=variant_id, state="unconfigured",
                reason=reason, policy=live_report.to_dict(),
            )

        # Gate 3: claim the idempotency key *before* the network call.
        digest = str(variant["content_hash"])
        account_id = int((account or {}).get("id") or 0)
        dedupe = f"live:{platform}:{account_id}:{digest}"
        claim = self._claim(dedupe, variant, account_id, scheduled_id, state="publishing")
        if claim is None:
            return PublishResult(
                ok=True, platform=platform, variant_id=variant_id, state="published",
                idempotent=True, reason="this exact text is already claimed or published",
                policy=live_report.to_dict(),
            )

        # Gate 4: the actual call.
        connector = self.connectors[platform]
        try:
            posted = connector.publish(variant, account) or ExternalPost()
        except Exception as exc:  # noqa: BLE001 - a connector failure is a result, not a crash
            message = f"{type(exc).__name__}: {exc}"
            self.db.update("published_post", claim, {
                "state": "failed", "meta": _json({"error": message[:300]}),
            })
            self._audit_refusal(variant_id, "publish_failed", [message])
            self._finish_schedule(scheduled_id, "failed", message)
            obs.error("publish.failed", platform=platform,
                      detail={"variant_id": variant_id, "error": message[:200]})
            return PublishResult(
                ok=False, platform=platform, variant_id=variant_id, state="failed",
                reason=message[:300], policy=live_report.to_dict(),
            )

        self.db.update("published_post", claim, {
            "state": "published",
            "external_id": posted.external_id or None,
            "url": posted.url or "",
            "published_at": to_ts(self._now()),
            "meta": _json(posted.meta or {}),
        })
        self.db.audit(
            "publish.published", actor=actor, subject=f"content_variant:{variant_id}",
            detail={"platform": platform, "external_id": posted.external_id,
                    "url": posted.url, "char_count": len(variant.get("body") or "")},
        )
        obs.info("publish.published", platform=platform,
                 detail={"variant_id": variant_id, "external_id": posted.external_id})
        self._finish_schedule(scheduled_id, "published", "")
        self._touch_item(variant)
        if self.notify is not None:
            try:
                self.notify.published(platform, posted.url or posted.external_id)
            except Exception:  # noqa: BLE001
                pass
        return PublishResult(
            ok=True, platform=platform, variant_id=variant_id, state="published",
            external_id=posted.external_id, url=posted.url, policy=live_report.to_dict(),
        )

    def publish_item(
        self,
        item_id: int,
        *,
        platforms: list[str] | None = None,
        simulate: bool | None = None,
        actor: str = "publisher",
    ) -> list[PublishResult]:
        item = self.db.item(item_id)
        if item is None:
            raise KeyError(f"content_item {item_id} not found")
        wanted = {str(p) for p in platforms} if platforms else None
        results: list[PublishResult] = []
        for variant in item.get("variants", []):
            platform = str(variant["platform"])
            if wanted and platform not in wanted:
                continue
            results.append(self.publish_variant(
                int(variant["id"]), simulate=simulate, actor=actor
            ))
        return results

    def run_due(
        self,
        *,
        limit: int = 5,
        worker: str = "worker",
        simulate: bool | None = None,
        now: datetime | None = None,
    ) -> list[PublishResult]:
        """The cloud worker's tick: lease due jobs and publish (or simulate) them."""
        moment = now or self._now()
        if self.kill_switch():
            obs.warning("publish.halted", detail={"reason": "kill switch engaged"})
            return []
        self.db.release_expired_leases(to_ts(moment))
        claimed = self.db.claim_due_posts(
            to_ts(moment), limit=limit, worker=worker, lease_seconds=300
        )
        results: list[PublishResult] = []
        for job in claimed:
            results.append(self.publish_variant(
                int(job["variant_id"]),
                simulate=simulate,
                actor=f"worker:{worker}",
                scheduled_id=int(job["id"]),
            ))
        return results

    # ------------------------------------------------------------------ details
    def _default_simulate(self, platform: str) -> bool:
        state = self.connector_state(platform)
        if not bool(state.get("ready")):
            return True
        if not bool(state.get("enabled", True)):
            return True
        return bool(state.get("dry_run", True))

    def _claim(self, dedupe: str, variant: dict[str, Any], account_id: int,
               scheduled_id: int | None, *, state: str) -> int | None:
        row = {
            "scheduled_id": scheduled_id,
            "variant_id": int(variant["id"]),
            "platform": str(variant["platform"]),
            "account_id": account_id or None,
            "content_hash": str(variant["content_hash"]),
            "state": state,
            "dedupe_key": dedupe,
            "published_at": to_ts(self._now()),
            "meta": _json({"lang": variant.get("lang"), "generator": variant.get("generator")}),
        }
        try:
            with self.db.transaction():
                return self.db.insert("published_post", row)
        except sqlite3.IntegrityError:
            existing = self.db.one(
                "SELECT id, state, url, external_id FROM published_post WHERE dedupe_key = ?",
                [dedupe],
            )
            obs.info("publish.duplicate_claim",
                     detail={"dedupe_key": dedupe, "existing_state": (existing or {}).get("state")})
            return None

    def _simulate(self, variant: dict[str, Any], account: dict[str, Any] | None,
                  live_report: Any, scheduled_id: int | None) -> PublishResult:
        digest = str(variant["content_hash"])
        platform = str(variant["platform"])
        account_id = int((account or {}).get("id") or 0)
        dedupe = f"dry:{platform}:{account_id}:{digest}"
        claim = self._claim(dedupe, variant, account_id, scheduled_id, state="dry_run")
        would_be = live_report.outcome
        meta = {
            "lang": variant.get("lang"),
            "generator": variant.get("generator"),
            "live_policy_outcome": would_be,
            "live_policy_reasons": live_report.reasons[:5],
            "simulated": True,
        }
        if claim is None:
            return PublishResult(
                ok=True, platform=platform, variant_id=int(variant["id"]),
                state="dry_run", idempotent=True,
                reason="simulation already recorded for this text",
                policy=live_report.to_dict(),
            )
        self.db.update("published_post", claim, {"meta": _json(meta)})
        reason = (
            "simulated: a real publish would be "
            + ("allowed" if would_be == str(PolicyOutcome.PASS)
               else ("held for review: " + "; ".join(live_report.reasons[:2])
                     if would_be == str(PolicyOutcome.NEEDS_REVIEW)
                     else "refused: " + "; ".join(live_report.reasons[:2])))
        )
        self.db.audit(
            "publish.simulated", actor="publisher",
            subject=f"content_variant:{variant['id']}",
            detail={"platform": platform, "would_be": would_be,
                    "reasons": live_report.reasons[:5]},
        )
        obs.info("publish.simulated", platform=platform,
                 detail={"variant_id": variant["id"], "would_be": would_be})
        return PublishResult(
            ok=True, platform=platform, variant_id=int(variant["id"]), state="dry_run",
            reason=reason, policy=live_report.to_dict(),
        )

    def _finish_schedule(self, scheduled_id: int | None, state: str, reason: str) -> None:
        if scheduled_id is None:
            return
        mapped = "published" if state in {"published", "dry_run"} else state
        self.db.finish_post(scheduled_id, state=mapped, error=reason)

    def _touch_item(self, variant: dict[str, Any]) -> None:
        item_id = variant.get("item_id")
        if item_id:
            self.db.set_item_state(int(item_id), "published")

    def _audit_refusal(self, variant_id: int, action: str, reasons: list[str]) -> None:
        self.db.audit(
            action, actor="publisher", subject=f"content_variant:{variant_id}",
            detail={"reasons": reasons[:6]},
        )
        obs.warning(action, detail={"variant_id": variant_id, "reasons": reasons[:3]})

    # ------------------------------------------------------- operator recovery
    def unknown_claims(self) -> list[dict[str, Any]]:
        """Rows claimed but never confirmed: a human must decide what happened."""
        return self.db.published_posts(limit=200, state="publishing")

    def resolve_claim(self, post_id: int, outcome: str, *, external_id: str = "",
                      url: str = "", actor: str = "operator") -> dict[str, Any]:
        """Close a ``publishing`` row by hand after checking the platform."""
        if outcome not in {"published", "failed"}:
            raise ValueError("outcome must be 'published' or 'failed'")
        row = self.db.get("published_post", post_id)
        if row is None:
            raise KeyError(f"published_post {post_id} not found")
        self.db.update("published_post", post_id, {
            "state": outcome,
            "external_id": external_id or None,
            "url": url or row.get("url") or "",
            "meta": _json({"resolved_by": actor, "previous_state": row["state"]}),
        })
        self.db.audit("publish.claim_resolved", actor=actor, subject=f"published_post:{post_id}",
                      detail={"outcome": outcome, "external_id": external_id})
        return self.db.get("published_post", post_id) or {}


def _why_not_ready(state: dict[str, Any]) -> str:
    """The most useful sentence available about why a connector cannot post.

    A missing credential should be named (its *variable*, never its value), not
    hidden behind a generic "not ready".
    """
    reason = str(state.get("reason") or "").strip()
    if reason:
        return reason
    missing = state.get("missing") or []
    if missing:
        return "; ".join(str(item) for item in missing[:3])
    return "connector is not ready"


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def platform_list() -> list[str]:
    return [str(p) for p in Platform]
