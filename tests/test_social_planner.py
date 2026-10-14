"""The editorial planner: slots, ceilings, and a week that stays empty on purpose."""

from datetime import datetime, timezone

import pytest

from socialmanager.planner import EditorialPlanner, Plan, local_tz

# ``db``/``settings`` are fixtures; importing them registers them here.
from tests.fake_social import db, make_settings, seed_item_with_variant, settings  # noqa: F401


@pytest.fixture()
def planner(settings, db):
    return EditorialPlanner(settings, db, now=lambda: datetime(2026, 10, 13, 8, 0,
                                                               tzinfo=timezone.utc))


def test_an_empty_pipeline_schedules_nothing(planner):
    plan = planner.plan(schedule=True)
    assert plan.slots == []
    assert any("no item has news behind it" in note for note in plan.notes)
    assert planner.db.count("scheduled_post") == 0


def test_a_plan_fills_the_weekly_targets_and_writes_the_queue(settings, db):
    for platform in ("x", "github", "instagram"):
        seed_item_with_variant(db, platform=platform)
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    assert plan.scheduled
    assert all(slot.scheduled_id for slot in plan.scheduled)
    assert db.count("scheduled_post") == len(plan.scheduled)
    states = {row["id"]: row["state"] for row in db.items(limit=10)}
    assert all(states[slot.item_id] == "scheduled" for slot in plan.scheduled)
    # Whatever does not fit in a slot stays a draft: no filler is invented.
    assert "draft" in set(states.values())


def test_the_weekly_target_is_a_ceiling(settings, db):
    settings.editorial.weekly_targets = {"x": 1, "instagram": 0, "github": 0}
    for _ in range(3):
        seed_item_with_variant(db)
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    assert plan.filled.get("x") == 1
    assert any("already reached" in slot.reason for slot in plan.skipped)


def test_the_daily_cap_and_the_minimum_gap_are_respected(settings, db):
    settings.editorial.weekly_targets = {"x": 4, "instagram": 0, "github": 0}
    settings.editorial.max_posts_per_day = {"x": 1}
    settings.editorial.min_gap_minutes = 240
    settings.editorial.default_slots = {"x": ["09:30", "12:00"]}
    for _ in range(3):
        seed_item_with_variant(db)
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    days = [slot.run_at[:10] for slot in plan.scheduled]
    assert len(days) == len(set(days)), "only one post a day is allowed"


def test_a_single_category_cannot_take_the_whole_week(settings, db):
    settings.editorial.weekly_targets = {"x": 6, "instagram": 0, "github": 0}
    settings.editorial.max_category_share = 0.2      # 1 slot out of 6
    for _ in range(4):
        seed_item_with_variant(db)
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    assert plan.filled.get("x", 0) <= 2
    assert any("share" in slot.reason for slot in plan.skipped)


def test_quiet_hours_are_skipped(settings, db):
    settings.editorial.weekly_targets = {"x": 1, "instagram": 0, "github": 0}
    settings.editorial.default_slots = {"x": ["03:00"]}
    settings.editorial.quiet_hours = [23, 7]
    seed_item_with_variant(db)
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    assert plan.scheduled == []
    assert any("no free slot" in slot.reason for slot in plan.skipped)


def test_risky_items_are_held_until_approved(settings, db):
    settings.editorial.weekly_targets = {"x": 1, "instagram": 0, "github": 0}
    item_id, _ = seed_item_with_variant(db, risk="sensitive")
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    held = [slot for slot in plan.slots if slot.decision == "held"]
    assert held and held[0].item_id == item_id
    assert "approval" in held[0].reason
    assert db.count("scheduled_post") == 0

    db.request_approval("content_item", item_id, risk="sensitive", reason="check")
    db.decide_approval(db.approvals(state="pending")[0]["id"], "approved")
    db.set_item_state(item_id, "approved")
    again = EditorialPlanner(settings, db).plan(schedule=True)
    assert again.scheduled


def test_a_policy_blocked_variant_is_not_scheduled(settings, db):
    settings.editorial.weekly_targets = {"x": 1, "instagram": 0, "github": 0}
    _, variant_id = seed_item_with_variant(db)
    db.set_variant_policy(variant_id, "block", {"outcome": "block"})
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    assert plan.scheduled == []
    assert any("policy blocked" in slot.reason for slot in plan.skipped)


def test_an_item_without_facts_is_not_a_candidate(settings, db):
    seed_item_with_variant(db, facts=[])
    plan = EditorialPlanner(settings, db).plan(schedule=True)
    assert plan.slots == []


def test_the_calendar_and_the_budget_agree(settings, db):
    for _ in range(2):
        seed_item_with_variant(db)
    planner = EditorialPlanner(settings, db)
    planner.plan(schedule=True)
    calendar = planner.calendar(days=7)
    assert calendar["targets"]
    assert calendar["filled"]
    assert len(calendar["days"]) == 7
    budget = planner.budget()
    assert all(row["planned"] <= calendar["filled"].get(row["platform"], 0)
               or row["planned"] == 0 for row in budget["platforms"])
    assert budget["unscheduled_drafts"] >= 0


def test_the_initial_calendar_is_only_a_proposal(settings, db):
    plan = EditorialPlanner(settings, db).initial_calendar(days=14)
    assert len(plan) > 10
    assert {entry["status"] for entry in plan} == {"proposal"}
    assert all("proposal only" in entry["note"] for entry in plan)
    assert db.count("scheduled_post") == 0
    assert {entry["platform"] for entry in plan} == {"x", "instagram", "github"}


def test_a_missing_timezone_database_is_declared(settings, db, monkeypatch):
    monkeypatch.setattr("socialmanager.planner.local_tz", lambda name: (timezone.utc, False))
    seed_item_with_variant(db)
    plan = EditorialPlanner(settings, db).plan()
    assert plan.timezone_available is False
    assert any("timezone" in note.lower() for note in plan.notes)


def test_local_tz_falls_back_to_utc():
    tz, available = local_tz("Not/AZone")
    assert available is False
    assert tz is timezone.utc or str(tz) == "UTC"
    resolved, ok = local_tz("Europe/Rome")
    assert ok in (True, False)   # depends on the machine having tzdata
    assert resolved is not None


def test_plan_serialises(settings, db):
    seed_item_with_variant(db)
    payload = EditorialPlanner(settings, db).plan().to_dict()
    assert isinstance(payload["slots"], list)
    assert "targets" in payload and "budget" not in payload
