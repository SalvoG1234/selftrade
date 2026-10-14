"""The orchestrator: one place that wires the components and runs a named command.

It is deliberately *not* a swarm.  A command is a sequence of calls, each of
which is testable on its own, and each of which records an ``agent_run`` row so
the Agent Activity page shows what actually ran - when it started, what it
produced, what failed.

The connectors are built here and handed down.  That is also where the wiring
boundary lives: the content engine receives a *provider*, the publisher receives
*connectors*, and the community agent receives read-only clients.  An LLM never
receives a connector, so "the model cannot post" is a property of the object
graph and not of a prompt instruction.
"""

from __future__ import annotations

import platform
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import analytics as analytics_mod
from . import community as community_mod
from . import config as config_mod
from . import content as content_mod
from . import github as github_mod
from . import instagram as instagram_mod
from . import llm as llm_mod
from . import notify as notify_mod
from . import obs, publisher as publisher_mod, secrets, visuals as visuals_mod
from . import x as x_mod
from .approvals import ApprovalManager
from .db import SocialDB, to_ts, utcnow
from .models import AccountStatus, Platform
from .planner import EditorialPlanner
from .policy import ACTION_DRAFT, PolicyEngine, kill_switch_engaged

MODE_LABELS = {
    "draft": "A - Draft: the AI writes, a human publishes",
    "controlled": "B - Controlled autonomy: ordinary content may publish itself",
    "advanced": "C - Advanced autonomy: plans, publishes, measures, adapts",
}


@dataclass
class CommandResult:
    command: str
    run_id: str
    ok: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "run_id": self.run_id, "ok": self.ok,
                "detail": self.detail, "messages": list(self.messages)}


class Orchestrator:
    def __init__(
        self,
        settings: Any | None = None,
        db: SocialDB | None = None,
        *,
        transport: Any = None,
        llm_post: Callable[..., Any] | None = None,
        llm_get: Callable[..., Any] | None = None,
        notifier: Any = None,
        db_path: str | None = None,
    ):
        self.settings = settings or config_mod.load()
        self.settings = self.settings
        if db is not None:
            self.db = db
        else:
            path = db_path or self.settings.database.url
            self.db = SocialDB.open(path, timeout=self.settings.database.timeout)
        self.transport = transport
        self.llm_post = llm_post
        self.llm_get = llm_get
        self.notifier = notifier or notify_mod.NotificationService(self.settings, self.db)
        obs.configure(log_file=self.settings.state_dir() / "social.log")
        self._github: github_mod.GitHubConnector | None = None
        self._x: x_mod.XConnector | None = None
        self._instagram: instagram_mod.InstagramConnector | None = None
        self._provider: llm_mod.LlmProvider | None = None

    # ------------------------------------------------------------- components
    @property
    def github(self) -> github_mod.GitHubConnector:
        if self._github is None:
            self._github = github_mod.GitHubConnector(
                self.settings, self.db, transport=self.transport, run_id=obs.current_run()
            )
        return self._github

    @property
    def x(self) -> x_mod.XConnector:
        if self._x is None:
            self._x = x_mod.XConnector(self.settings, self.db, transport=self.transport,
                                       run_id=obs.current_run())
        return self._x

    @property
    def instagram(self) -> instagram_mod.InstagramConnector:
        if self._instagram is None:
            self._instagram = instagram_mod.InstagramConnector(
                self.settings, self.db, transport=self.transport, run_id=obs.current_run()
            )
        return self._instagram

    @property
    def provider(self) -> llm_mod.LlmProvider:
        if self._provider is None:
            self._provider = llm_mod.build_provider(
                self.settings, post=self.llm_post, get=self.llm_get
            )
        return self._provider

    def connectors(self) -> dict[str, Any]:
        return {
            str(Platform.GITHUB): self.github,
            str(Platform.X): self.x,
            str(Platform.INSTAGRAM): self.instagram,
        }

    def publisher(self) -> publisher_mod.Publisher:
        return publisher_mod.Publisher(
            self.settings, self.db, self.connectors(), notify=self.notifier
        )

    def planner(self) -> EditorialPlanner:
        return EditorialPlanner(self.settings, self.db)

    def approvals(self) -> ApprovalManager:
        return ApprovalManager(self.settings, self.db, notify=self.notifier)

    def policy(self) -> PolicyEngine:
        return PolicyEngine(self.settings, self.db)

    # ------------------------------------------------------------------- runs
    def _start(self, agent: str, command: str) -> str:
        run_id = f"{command}-{utcnow().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.db.start_run(run_id, agent, command)
        obs.bind_run(run_id)
        obs.bind_secret_names(self.settings.secret_names())
        return run_id

    def _finish(self, run_id: str, *, state: str, summary: str,
                detail: dict[str, Any], items: int = 0) -> None:
        self.db.finish_run(run_id, state=state, summary=summary, detail=detail, items=items)

    # --------------------------------------------------------------- commands
    def status(self) -> dict[str, Any]:
        stats = self.db.stats()
        approvals = self.approvals().counts()
        return {
            "project": self.settings.project.repo,
            "brand": self.settings.project.brand,
            "mode": str(self.settings.mode()),
            "mode_label": MODE_LABELS.get(str(self.settings.mode()), "unknown"),
            "kill_switch": kill_switch_engaged(self.db, self.settings),
            "platforms_allowed": [str(p) for p in self.settings.platforms()],
            "languages": list(self.settings.editorial.languages),
            "stats": stats,
            "approvals": approvals,
            "connectors": {name: self.connector_state(name) for name in
                           (str(Platform.GITHUB), str(Platform.X), str(Platform.INSTAGRAM))},
            "llm": self.provider.describe(),
            "notifications": self.notifier.readiness(),
            "generated_at": to_ts(),
            "database": self.db.health(),
        }

    def connector_state(self, platform: str) -> dict[str, Any]:
        mapping = {
            str(Platform.GITHUB): self.github,
            str(Platform.X): self.x,
            str(Platform.INSTAGRAM): self.instagram,
        }
        connector = mapping.get(str(platform))
        if connector is None:
            return {"platform": str(platform), "ready": False, "reason": "unknown platform"}
        return connector.readiness()

    def refresh_accounts(self) -> list[dict[str, Any]]:
        """Record each account's *status and credential name* - never the value."""
        rows: list[dict[str, Any]] = []
        for connector, platform, handle in (
            (self.github, str(Platform.GITHUB), self.settings.project.repo),
            (self.x, str(Platform.X), self.settings.x.account_handle or "unset"),
            (self.instagram, str(Platform.INSTAGRAM),
             self.settings.instagram.account_handle or "unset"),
        ):
            state = connector.readiness()
            if platform == str(Platform.GITHUB):
                status = AccountStatus.READY if state.get("enabled") else AccountStatus.UNCONFIGURED
                role = "source"
            else:
                status = AccountStatus.READY if state.get("ready") else AccountStatus.DRAFT_ONLY
                role = "managed"
            scopes = list(state.get("scopes") or [])
            account_id = self.db.upsert_account(
                platform, handle, role=role, status=str(status), scopes=scopes,
                token_env=state.get("token_env"),
                token_fingerprint=state.get("token_fingerprint"),
                notes="; ".join(str(x) for x in (state.get("missing") or []))[:400],
            )
            rows.append(self.db.get("social_account", account_id) or {})
        self.db.audit("accounts.refreshed", actor="orchestrator", subject="social_accounts",
                      detail={"platforms": [r.get("platform") for r in rows]})
        return rows

    def sync(self, *, since_days: int | None = None, kinds: tuple[str, ...] | None = None
             ) -> CommandResult:
        run_id = self._start("github-connector", "sync")
        report = self.github.sync(
            since_days=since_days,
            kinds=kinds or ("readme", "release", "commit", "issue", "discussion"),
        )
        docs = content_mod.index_local_docs(self.db, Path.cwd())
        community = community_mod.CommunityAgent(
            self.settings, self.db, github=self.github, notify=self.notifier, run_id=run_id
        ).ingest_github(since_days=since_days or self.settings.github.since_days)
        detail = {"sync": report.to_dict(), "docs_indexed": docs, "community": community}
        state = "ok" if report.ok else "partial"
        self._finish(run_id, state=state,
                     summary=f"{len(report.new)} new sources, {len(report.errors)} errors",
                     detail=detail, items=len(report.new))
        messages = []
        if report.errors:
            messages.append("errors: " + "; ".join(report.errors[:3]))
        if not report.authenticated:
            messages.append(
                f"{self.settings.github.token_env} is not set: reading anonymously at 60 "
                "requests/hour and no discussions"
            )
        return CommandResult("sync", run_id, ok=report.ok, detail=detail, messages=messages)

    def produce(self, *, limit: int = 6, evaluate: bool = True) -> CommandResult:
        """Generate drafts from real events, then judge each one."""
        run_id = self._start("content-agent", "produce")
        engine = content_mod.ContentEngine(
            self.settings, self.db, self.provider,
            visuals=visuals_mod.VisualGenerator(self.settings, self.db).generate,
            run_id=run_id,
        )
        report = engine.run(limit=limit)
        judged: dict[str, int] = {}
        requests: list[int] = []
        if evaluate:
            for item_id in report.items:
                item = self.db.item(item_id) or {}
                for variant in item.get("variants", []):
                    outcome = self.policy().evaluate_and_store(
                        int(variant["id"]), action=ACTION_DRAFT
                    )
                    judged[str(outcome.outcome)] = judged.get(str(outcome.outcome), 0) + 1
                verdict = self.approvals().needs_human(item, kind="content")
                if verdict.needed:
                    self.approvals().ensure_request(
                        item_id,
                        risk=str(item.get("risk", "low")),
                        reason=verdict.reason or "mode A: a human publishes",
                    )
                    requests.append(item_id)
        detail = {**report.to_dict(), "policy": judged, "approval_requests": requests}
        self._finish(run_id, state="ok" if not report.errors else "partial",
                     summary=f"{len(report.items)} items, {report.variants} variants",
                     detail=detail, items=len(report.items))
        messages = []
        if not self.provider.available():
            messages.append(
                "no LLM available: drafts were written from the built-in bilingual templates "
                f"({self.provider.describe().get('reason') or 'provider unavailable'})"
            )
        if report.errors:
            messages.append("errors: " + "; ".join(report.errors[:3]))
        return CommandResult("produce", run_id, ok=not report.errors, detail=detail,
                             messages=messages)

    def plan(self, *, schedule: bool = False, week_start: str | None = None,
             limit: int | None = None) -> CommandResult:
        run_id = self._start("editorial-planner", "plan")
        plan = self.planner().plan(week_start=week_start, schedule=schedule, limit=limit)
        detail = plan.to_dict()
        self._finish(run_id, state="ok", summary=f"{len(plan.scheduled)} slots scheduled",
                     detail=detail, items=len(plan.scheduled))
        messages = []
        if not plan.timezone_available:
            messages.append("the IANA timezone database is unavailable: slots were computed in UTC")
        if not plan.slots:
            messages.extend(plan.notes[:2])
        if schedule:
            messages.append(f"{len(plan.scheduled)} posts queued (state: pending)")
        return CommandResult("plan", run_id, ok=True, detail=detail, messages=messages)

    def tick(self, *, simulate: bool | None = None, limit: int = 5,
             worker: str = "local", collect_metrics: bool = True) -> CommandResult:
        run_id = self._start("publisher", "tick")
        publisher = self.publisher()
        results = publisher.run_due(limit=limit, worker=worker, simulate=simulate)
        collected = None
        if collect_metrics and any(r.state == "published" for r in results):
            collected = analytics_mod.AnalyticsAgent(
                self.settings, self.db, x=self.x, instagram=self.instagram, run_id=run_id
            ).collect().to_dict()
        detail = {"results": [r.to_dict() for r in results], "metrics": collected}
        self._finish(run_id, state="ok",
                     summary=f"{len(results)} jobs handled", detail=detail,
                     items=len(results))
        messages = []
        for result in results:
            if not result.ok:
                messages.append(f"{result.platform} #{result.variant_id}: {result.state} - "
                                f"{result.reason[:120]}")
        if not results:
            messages.append("nothing was due")
        return CommandResult("tick", run_id, ok=True, detail=detail, messages=messages)

    def decide(self, item_id: int, decision: str, *, note: str = "",
               actor: str = "operator") -> dict[str, Any]:
        manager = self.approvals()
        if decision == "approved":
            return manager.approve(item_id, note=note, actor=actor)
        return manager.reject(item_id, note=note, actor=actor)

    def feedback(self, *, ingest: bool = True) -> CommandResult:
        run_id = self._start("community-agent", "feedback")
        agent = community_mod.CommunityAgent(
            self.settings, self.db, github=self.github, x=self.x, instagram=self.instagram,
            notify=self.notifier, run_id=run_id,
        )
        detail: dict[str, Any] = {}
        if ingest:
            detail["github"] = agent.ingest_github()
        detail["report"] = agent.report(days=7)
        self._finish(run_id, state="ok", summary=f"{detail['report']['total']} feedback items",
                     detail=detail, items=int(detail["report"]["total"]))
        return CommandResult("feedback", run_id, ok=True, detail=detail)

    def analytics(self, *, days: int = 30, collect: bool = True) -> CommandResult:
        run_id = self._start("analytics-agent", "analytics")
        agent = analytics_mod.AnalyticsAgent(self.settings, self.db, x=self.x,
                                             instagram=self.instagram, run_id=run_id)
        collected = agent.collect(days=days).to_dict() if collect else None
        report = agent.report(days=days)
        detail = {"collected": collected, "report": report}
        self._finish(run_id, state="ok", summary=f"{report['totals']['published']} published posts",
                     detail=detail, items=int(report["totals"]["published"]))
        return CommandResult("analytics", run_id, ok=True, detail=detail)

    def doctor(self) -> dict[str, Any]:
        """Everything that must be true before this thing is allowed to run."""
        problems = config_mod.validate(self.settings)
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str, hint: str = "") -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail, "hint": hint})

        check("python", sys.version_info >= (3, 10),
              f"{platform.python_version()} on {platform.system()}",
              "3.10 or newer is required for the type syntax used here")
        check("database", self.db.health()["schema_version"] > 0,
              f"schema v{self.db.health()['schema_version']} at {self.settings.database.url}")
        audit = self.db.verify_audit()
        check("audit_chain", bool(audit["ok"]),
              f"{audit['checked']} events verified" if audit["ok"]
              else f"broken at #{audit['at']}: {audit['reason']}",
              "the audit log is append-only; a broken chain means the file was edited")
        check("configuration", not problems, "no problems" if not problems
              else "; ".join(problems[:4]),
              "run `config validate` for the full list" if problems else "")
        for name in (str(Platform.GITHUB), str(Platform.X), str(Platform.INSTAGRAM)):
            state = self.connector_state(name)
            if name == str(Platform.GITHUB):
                check(f"connector.{name}", bool(state.get("enabled")),
                      f"{state.get('mode')} mode, token_env={state.get('token_env')}",
                      "public reads work without a token; discussions need one")
                continue
            check(f"connector.{name}", bool(state.get("ready")),
                  "ready" if state.get("ready") else "; ".join(state.get("missing") or ["not ready"])[:220],
                  "this is expected until you complete the platform onboarding")
        llm = self.provider.describe()
        check("llm", bool(llm.get("available")),
              f"{llm.get('provider')} {llm.get('model')}" if llm.get("available")
              else f"unavailable: {llm.get('reason') or 'not configured'}",
              "drafts still work: the deterministic templates are used instead")
        check("notifications", not self.notifier.readiness()["would_send"] or True,
              "configured" if self.notifier.readiness()["would_send"] else "disabled (fine)")
        check("kill_switch", not kill_switch_engaged(self.db, self.settings),
              "engaged: publishing is stopped" if kill_switch_engaged(self.db, self.settings)
              else "released (publishing follows the autonomy mode)",
              "release it from the dashboard or `killswitch release`")
        check("autonomy_mode", True, MODE_LABELS.get(str(self.settings.mode()), "unknown"))
        return {
            "ok": all(c["ok"] for c in checks if c["check"] not in {
                "connector.x", "connector.instagram", "llm", "notifications",
            }),
            "checks": checks,
            "problems": problems,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "cwd": str(Path.cwd()),
                "config_path": self.settings.path or "(defaults)",
                "config_existed": self.settings.existed,
                "database": self.settings.database.url,
                "overrides": list(self.settings.overrides),
            },
            "generated_at": to_ts(),
        }

    # ------------------------------------------------------------- operator ops
    def set_kill_switch(self, engaged: bool, *, actor: str = "operator") -> dict[str, Any]:
        from .policy import set_kill_switch

        set_kill_switch(self.db, engaged, actor=actor)
        try:
            self.notifier.kill_switch(engaged, actor)
        except Exception:  # noqa: BLE001
            pass
        return {"kill_switch": bool(engaged), "actor": actor, "at": to_ts()}

    def set_setting(self, key: str, value: Any, *, actor: str = "operator",
                    require_approval: bool = False) -> dict[str, Any]:
        """Changing limits is an operator action, recorded with the old value."""
        before = self.db.setting(key, None)
        if require_approval:
            approval_id = self.approvals().request_setting_change(
                key, value, actor=actor, reason="a settings change always needs a human"
            )
            return {"pending_approval": approval_id, "key": key, "before": before,
                    "proposed": value}
        self.db.set_setting(key, value, actor=actor)
        self.db.audit("settings.changed", actor=actor, subject=key,
                      detail={"before": before, "after": value})
        return {"key": key, "before": before, "after": value}

    def audit_verify(self) -> dict[str, Any]:
        return self.db.verify_audit()

    def backup(self, directory: str | None = None) -> str:
        return str(self.db.backup(directory or self.settings.database.backup_dir))

    def close(self) -> None:
        self.db.close()

    # The CLI uses ``with Orchestrator(...)`` so a command always releases the
    # database connection, including when it raises.
    def __enter__(self) -> Orchestrator:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
