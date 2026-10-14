"""The editorial planner: which draft, on which platform, in which slot, and why.

The calendar is not decorative.  A plan is a list of *decisions*: each entry
records the slot it took, the rule it satisfied, and - when it was dropped - the
limit it hit.  Three constraints are enforced here rather than hoped for:

* **frequency** - ``editorial.max_posts_per_day`` per platform, the weekly
  targets, and a minimum gap between two posts on the same platform;
* **variety** - a single category may not take more than
  ``editorial.max_category_share`` of the week, so a week of commits does not
  become a week of the same post;
* **nothing to say** - if no item has news behind it (no facts, all sources
  already covered), the plan is short and says why.  Generating content to fill
  a slot is exactly what the brief forbids.

Slots are written in the project's timezone and stored in UTC.  If the platform
has no IANA timezone database (Windows without ``tzdata``), the planner falls
back to UTC and *says so in the plan* instead of pretending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import obs
from .db import SocialDB, parse_ts, to_ts, utcnow
from .models import Category, EditorialState, Platform, Risk

try:  # pragma: no cover - depends on the machine, not on the logic
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


def local_tz(name: str) -> tuple[Any, bool]:
    """The project timezone, or UTC with ``False`` when the data is missing."""
    if ZoneInfo is None or not name:
        return timezone.utc, False
    try:
        return ZoneInfo(str(name)), True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc, False


@dataclass
class PlanSlot:
    item_id: int
    variant_id: int
    platform: str
    run_at: str
    category: str
    risk: str
    title: str
    decision: str = "scheduled"          # scheduled | skipped | held
    reason: str = ""
    scheduled_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "variant_id": self.variant_id,
            "platform": self.platform,
            "run_at": self.run_at,
            "category": self.category,
            "risk": self.risk,
            "title": self.title,
            "decision": self.decision,
            "reason": self.reason,
            "scheduled_id": self.scheduled_id,
        }


@dataclass
class Plan:
    week_start: str
    timezone: str
    timezone_available: bool = True
    slots: list[PlanSlot] = field(default_factory=list)
    skipped: list[PlanSlot] = field(default_factory=list)
    targets: dict[str, int] = field(default_factory=dict)
    filled: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def scheduled(self) -> list[PlanSlot]:
        return [s for s in self.slots if s.decision == "scheduled"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start,
            "timezone": self.timezone,
            "timezone_available": self.timezone_available,
            "slots": [s.to_dict() for s in self.slots],
            "skipped": [s.to_dict() for s in self.skipped],
            "targets": dict(self.targets),
            "filled": dict(self.filled),
            "notes": list(self.notes),
        }


class EditorialPlanner:
    def __init__(self, settings: Any, db: SocialDB, *, now=None):
        self.settings = settings
        self.db = db
        self._now = now or utcnow

    # ------------------------------------------------------------------ slots
    def _slots_for(self, day: datetime, platform: str) -> list[datetime]:
        times = list(self.settings.editorial.default_slots.get(platform, []) or [])
        out: list[datetime] = []
        for text in times:
            try:
                hour, _, minute = str(text).partition(":")
                out.append(day.replace(hour=int(hour), minute=int(minute or 0),
                                       second=0, microsecond=0))
            except (ValueError, TypeError):
                continue
        return out

    def _in_quiet_hours(self, moment: datetime) -> bool:
        quiet = list(self.settings.editorial.quiet_hours or [])
        if not quiet:
            return False
        start, end = int(quiet[0]), int(quiet[-1])
        hour = moment.hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # ------------------------------------------------------------------- plan
    def plan(
        self,
        *,
        week_start: str | None = None,
        schedule: bool = True,
        limit: int | None = None,
    ) -> Plan:
        tz, tz_ok = local_tz(self.settings.project.timezone)
        now_local = self._now().astimezone(tz)
        start_local = (
            parse_ts(week_start).astimezone(tz) if week_start
            else now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        plan = Plan(
            week_start=start_local.date().isoformat(),
            timezone=str(self.settings.project.timezone),
            timezone_available=tz_ok,
            targets=dict(self.settings.editorial.weekly_targets),
        )
        if not tz_ok:
            plan.notes.append(
                "IANA timezone data is unavailable on this machine: slots are in UTC"
            )

        candidates = self._candidates()
        if not candidates:
            plan.notes.append(
                "no item has news behind it: nothing is scheduled rather than filling slots"
            )
            return plan

        max_share = float(self.settings.editorial.max_category_share)
        planned: list[tuple[int, int, str, datetime, str, str]] = []
        category_counts: dict[str, int] = {}
        per_day: dict[tuple[str, str], int] = {}
        day_count = max(1, int(self.settings.editorial.planning_days))
        capacity = 0
        for platform, target in self.settings.editorial.weekly_targets.items():
            capacity += int(target)
        category_cap = max(1, int(capacity * max_share)) if capacity else 1

        for item in candidates:
            for variant in item["variants"]:
                platform = str(variant["platform"])
                category = str(item["category"])
                weekly_target = int(self.settings.editorial.weekly_targets.get(platform, 0) or 0)
                if weekly_target <= 0:
                    plan.skipped.append(self._slot(item, variant, "", "skipped",
                                                    f"weekly target for {platform} is 0"))
                    continue
                if plan.filled.get(platform, 0) >= weekly_target:
                    plan.skipped.append(self._slot(
                        item, variant, "", "skipped",
                        f"weekly target for {platform} already reached ({weekly_target})"))
                    continue
                if category_counts.get(category, 0) >= category_cap:
                    plan.skipped.append(self._slot(item, variant, "", "skipped",
                                                    f"category {category} reached its share"))
                    continue
                slot = self._next_slot(start_local, platform, planned, per_day, day_count)
                if slot is None:
                    plan.skipped.append(self._slot(item, variant, "", "skipped",
                                                    "no free slot inside the week"))
                    continue
                if str(variant.get("policy_state")) == str("block"):
                    plan.skipped.append(self._slot(item, variant, to_ts(slot), "skipped",
                                                    "policy blocked this variant"))
                    continue
                entry = self._slot(item, variant, to_ts(slot), "scheduled", "")
                if item["state"] not in {str(EditorialState.APPROVED), str(EditorialState.SCHEDULED)}:
                    if str(item["risk"]) in {str(r) for r in self.settings.autonomy.require_approval_risks}:
                        entry.decision = "held"
                        entry.reason = "needs approval before it can be scheduled"
                planned.append((int(item["id"]), int(variant["id"]), platform, slot,
                                category, entry.decision))
                per_day[(slot.date().isoformat(), platform)] = \
                    per_day.get((slot.date().isoformat(), platform), 0) + 1
                if entry.decision == "scheduled":
                    category_counts[category] = category_counts.get(category, 0) + 1
                    plan.filled[platform] = plan.filled.get(platform, 0) + 1
                plan.slots.append(entry)
                if limit and len(plan.slots) >= limit:
                    break
            if limit and len(plan.slots) >= limit:
                break

        if schedule:
            self._persist(plan)
        obs.info("planner.plan", detail={
            "scheduled": len(plan.scheduled), "slots": len(plan.slots),
            "skipped": len(plan.skipped),
        })
        return plan

    def _candidates(self) -> list[dict[str, Any]]:
        items = self.db.items(
            state=f"{EditorialState.DRAFT},{EditorialState.APPROVED},{EditorialState.SCHEDULED}",
            limit=60,
        )
        out = []
        for item in items:
            if not item.get("facts"):
                continue
            variants = self.db.variants_for(int(item["id"]))
            if not variants:
                continue
            item = dict(item)
            item["variants"] = variants
            out.append(item)
        out.sort(key=lambda i: (str(i.get("planned_for") or "9999"), -int(i["id"])))
        return out

    def _next_slot(self, start_local: datetime, platform: str,
                   planned: list[tuple[Any, ...]],
                   per_day: dict[tuple[str, str], int],
                   day_count: int) -> datetime | None:
        max_day = int(self.settings.editorial.max_posts_per_day.get(platform, 0) or 0)
        gap = int(self.settings.editorial.min_gap_minutes or 0)
        taken = [p[3] for p in planned if p[2] == platform]
        for offset in range(0, day_count + 1):
            day = start_local + timedelta(days=offset)
            for slot in self._slots_for(day, platform):
                if slot < self._now().astimezone(slot.tzinfo):
                    continue
                if self._in_quiet_hours(slot):
                    continue
                if max_day and per_day.get((day.date().isoformat(), platform), 0) >= max_day:
                    continue
                if gap and any(abs((slot - other).total_seconds()) < gap * 60 for other in taken):
                    continue
                return slot
        return None

    def _slot(self, item: dict[str, Any], variant: dict[str, Any], run_at: str,
              decision: str, reason: str) -> PlanSlot:
        return PlanSlot(
            item_id=int(item["id"]),
            variant_id=int(variant["id"]),
            platform=str(variant["platform"]),
            run_at=run_at,
            category=str(item["category"]),
            risk=str(item["risk"]),
            title=str(item["title"]),
            decision=decision,
            reason=reason,
        )

    def _persist(self, plan: Plan) -> None:
        today = self._now().date()
        pending_first = to_ts(datetime.combine(today, datetime.min.time(),
                                               tzinfo=timezone.utc))
        for slot in plan.slots:
            if slot.decision != "scheduled":
                continue
            run_at = slot.run_at
            if parse_ts(run_at) and parse_ts(run_at) < self._now():
                # A slot in the past (the day already started) is scheduled now.
                run_at = to_ts(max(self._now(), parse_ts(pending_first) or self._now()))
            account = self.db.account_for(slot.platform)
            approval = self.db.approval_for("content_item", slot.item_id)
            try:
                slot.scheduled_id = self.db.schedule_post(
                    slot.variant_id,
                    run_at,
                    platform=slot.platform,
                    account_id=int(account["id"]) if account else None,
                    approval_id=int(approval["id"]) if approval else None,
                )
            except KeyError as exc:  # pragma: no cover - defensive
                slot.decision = "skipped"
                slot.reason = str(exc)
                continue
            self.db.set_item_state(slot.item_id, EditorialState.SCHEDULED)
        self.db.audit(
            "planner.plan_created",
            actor="editorial-planner",
            subject=plan.week_start,
            detail={"scheduled": len(plan.scheduled), "held": len(plan.slots) - len(plan.scheduled),
                    "targets": plan.targets},
        )

    # ------------------------------------------------------------------ views
    def calendar(self, start: str | None = None, days: int = 7) -> dict[str, Any]:
        """The week as the dashboard shows it: per day, per platform, with gaps."""
        tz, tz_ok = local_tz(self.settings.project.timezone)
        start_local = (parse_ts(start).astimezone(tz) if start
                       else self._now().astimezone(tz))
        window_start = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=max(1, int(days)))
        rows = self.db.all(
            """SELECT s.id, s.platform, s.run_at, s.state, s.attempts, s.last_error,
                      v.id AS variant_id, v.lang, v.body, v.policy_state,
                      i.id AS item_id, i.title, i.category, i.risk, i.state AS item_state
               FROM scheduled_post s
               JOIN content_variant v ON v.id = s.variant_id
               JOIN content_item i ON i.id = v.item_id
               WHERE s.run_at >= ? AND s.run_at < ?
               ORDER BY s.run_at ASC""",
            [to_ts(window_start), to_ts(window_end)],
        )
        days_out: dict[str, dict[str, Any]] = {}
        for offset in range(max(1, int(days))):
            day = (window_start + timedelta(days=offset)).date().isoformat()
            days_out[day] = {"date": day, "slots": []}
        for row in rows:
            moment = parse_ts(row["run_at"])
            key = (moment.astimezone(tz).date().isoformat() if moment else "")
            if key not in days_out:
                continue
            days_out[key]["slots"].append({
                **row,
                "local_time": moment.astimezone(tz).strftime("%H:%M") if moment else "",
            })
        targets = dict(self.settings.editorial.weekly_targets)
        filled: dict[str, int] = {}
        for row in rows:
            if str(row["state"]) in {"pending", "leased", "published"}:
                filled[str(row["platform"])] = filled.get(str(row["platform"]), 0) + 1
        return {
            "timezone": str(self.settings.project.timezone),
            "timezone_available": tz_ok,
            "start": window_start.date().isoformat(),
            "days": [days_out[k] for k in sorted(days_out)],
            "targets": targets,
            "filled": filled,
            "unscheduled_drafts": self.db.count(
                "content_item", "state IN ('draft','approved','needs_review')"
            ),
        }

    def budget(self, start: str | None = None) -> dict[str, Any]:
        """How the week sits against the targets - including 'behind, on purpose'."""
        cal = self.calendar(start, days=int(self.settings.editorial.planning_days))
        rows = []
        for platform, target in cal["targets"].items():
            filled = int(cal["filled"].get(platform, 0))
            rows.append({
                "platform": platform,
                "target": int(target),
                "planned": filled,
                "remaining": max(0, int(target) - filled),
                "state": ("empty" if filled == 0 else
                          "full" if filled >= int(target) else "partial"),
            })
        return {
            "window_start": cal["start"],
            "period_days": int(self.settings.editorial.planning_days),
            "platforms": rows,
            "unscheduled_drafts": cal["unscheduled_drafts"],
            "max_category_share": float(self.settings.editorial.max_category_share),
        }

    def categories_by_week(self, start: str | None = None) -> dict[str, int]:
        cal = self.calendar(start, days=int(self.settings.editorial.planning_days))
        counts: dict[str, int] = {}
        for day in cal["days"]:
            for slot in day["slots"]:
                counts[str(slot["category"])] = counts.get(str(slot["category"]), 0) + 1
        return counts

    def initial_calendar(self, *, days: int = 14) -> list[dict[str, Any]]:
        """A starting calendar to hand the operator before anything is scheduled.

        It proposes real categories for real slots, and it marks every entry as a
        proposal - nothing here is scheduled, and no content is generated.
        """
        tz, _ = local_tz(self.settings.project.timezone)
        start = self._now().astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        plan: list[dict[str, Any]] = []
        for offset in range(int(days)):
            day = start + timedelta(days=offset)
            weekday = day.weekday()
            for platform, slots in self.settings.editorial.default_slots.items():
                for text in slots or []:
                    if weekday >= 5 and platform == str(Platform.INSTAGRAM):
                        continue
                    hour, _, minute = str(text).partition(":")
                    moment = day.replace(hour=int(hour), minute=int(minute or 0))
                    category = self._suggest_category(weekday, platform)
                    plan.append({
                        "date": day.date().isoformat(),
                        "time": moment.strftime("%H:%M"),
                        "platform": platform,
                        "category": str(category),
                        "label": CATEGORY_LABELS.get(str(category), str(category)),
                        "status": "proposal",
                        "note": "proposal only: filled when the repository has real news",
                    })
        return plan

    @staticmethod
    def _suggest_category(weekday: int, platform: str) -> Category:
        if platform == str(Platform.GITHUB):
            return Category.TECH_EXPLAINER if weekday % 3 else Category.CONTRIBUTE
        if platform == str(Platform.INSTAGRAM):
            return Category.DASHBOARD_DEMO if weekday % 2 else Category.BUILD_IN_PUBLIC
        return (Category.DEV_UPDATE, Category.TECH_EXPLAINER, Category.BUILD_IN_PUBLIC,
                Category.EXPERIMENT, Category.FEEDBACK_REQUEST,
                Category.LIMITS, Category.BUILD_IN_PUBLIC)[weekday % 7]


CATEGORY_LABELS = {
    str(Category.BUILD_IN_PUBLIC): "Build in public",
    str(Category.DEV_UPDATE): "Aggiornamento di sviluppo",
    str(Category.RELEASE): "Release e changelog",
    str(Category.TECH_EXPLAINER): "Spiegazione tecnica",
    str(Category.EXPERIMENT): "Esperimento e risultato reale",
    str(Category.LIMITS): "Limiti e problemi scoperti",
    str(Category.DASHBOARD_DEMO): "Demo della dashboard",
    str(Category.FEEDBACK_REQUEST): "Richiesta di feedback",
    str(Category.TUTORIAL): "Tutorial e guida",
    str(Category.CONTRIBUTE): "Invito a contribuire",
}
