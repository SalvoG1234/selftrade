"""The publisher: the only component that sends, and every way it refuses to."""

import pytest

from socialmanager.db import to_ts, utcnow
from socialmanager.publisher import Publisher

# ``db``/``settings`` are fixtures; importing them registers them here.
from tests.fake_social import (  # noqa: F401
    FakeConnector,
    db,
    make_account,
    seed_item_with_variant,
    settings,
)


def ready_settings(settings, *, mode="controlled"):
    settings.autonomy.mode = mode
    settings.editorial.min_gap_minutes = 0
    settings.editorial.max_posts_per_day = {"x": 5, "github": 5, "instagram": 5}
    settings.x.dry_run = False
    settings.x.enabled = True
    return settings


def test_simulation_records_a_dry_run_and_calls_nothing(settings, db):
    _, variant_id = seed_item_with_variant(db)
    connector = FakeConnector(ready=True, dry_run=True)
    publisher = Publisher(settings, db, {"x": connector})

    result = publisher.publish_variant(variant_id, simulate=True)
    assert result.ok and result.state == "dry_run"
    assert connector.calls == []
    rows = db.published_posts(state="dry_run")
    assert len(rows) == 1
    assert rows[0]["meta"]["simulated"] is True
    assert "would be" in rows[0]["meta"]["live_policy_reasons"][0] or \
        "mode A" in " ".join(rows[0]["meta"]["live_policy_reasons"])


def test_mode_a_refuses_a_live_publish(settings, db):
    _, variant_id = seed_item_with_variant(db)
    connector = FakeConnector(ready=True)
    publisher = Publisher(settings, db, {"x": connector})

    result = publisher.publish_variant(variant_id, simulate=False)
    assert result.ok is False
    assert result.state == "blocked"
    assert "mode A" in result.reason
    assert connector.calls == []
    assert db.count("published_post") == 0


def test_a_live_publish_needs_a_ready_connector(settings, db):
    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    _, variant_id = seed_item_with_variant(db)
    publisher = Publisher(settings, db, {"x": FakeConnector(ready=False)})

    result = publisher.publish_variant(variant_id, simulate=False)
    assert result.state == "unconfigured"
    assert "no credentials" in result.reason
    assert db.count("published_post") == 0


def test_a_live_publish_passes_all_four_gates(settings, db):
    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    item_id, variant_id = seed_item_with_variant(db)
    connector = FakeConnector(ready=True)
    publisher = Publisher(settings, db, {"x": connector})

    result = publisher.publish_variant(variant_id, simulate=False)
    assert result.ok, result.reason
    assert result.state == "published"
    assert result.external_id == "999"
    assert len(connector.calls) == 1

    row = db.published_posts(state="published")[0]
    assert row["url"] == "https://example.test/999"
    assert db.item(item_id)["state"] == "published"
    actions = {event["action"] for event in db.audit_tail(limit=10)}
    assert "publish.published" in actions


def test_needs_review_is_held_and_queued(settings, db):
    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    item_id, variant_id = seed_item_with_variant(db, risk="high")
    connector = FakeConnector(ready=True)
    publisher = Publisher(settings, db, {"x": connector})

    result = publisher.publish_variant(variant_id, simulate=False)
    assert result.state == "needs_review"
    assert result.ok is False
    assert connector.calls == []
    assert db.count("approval", "state = 'pending'") == 1

    # With the approval in place the same call goes through.
    approval = db.approvals(state="pending")[0]
    publisher.approvals.decide(int(approval["id"]), "approved", decided_by="operator")
    again = publisher.publish_variant(variant_id, simulate=False)
    assert again.ok, again.reason
    assert len(connector.calls) == 1


def test_a_second_publish_of_the_same_text_is_idempotent(settings, db):
    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    _, variant_id = seed_item_with_variant(db)
    connector = FakeConnector(ready=True)
    publisher = Publisher(settings, db, {"x": connector})

    first = publisher.publish_variant(variant_id, simulate=False)
    second = publisher.publish_variant(variant_id, simulate=False)
    assert first.ok and second.ok
    assert second.idempotent is True
    assert "already claimed" in second.reason
    assert len(connector.calls) == 1, "the platform must be called exactly once"
    assert db.count("published_post") == 1


def test_a_second_simulation_is_also_idempotent(settings, db):
    _, variant_id = seed_item_with_variant(db)
    publisher = Publisher(settings, db, {"x": FakeConnector(dry_run=True)})
    publisher.publish_variant(variant_id, simulate=True)
    again = publisher.publish_variant(variant_id, simulate=True)
    assert again.idempotent is True
    assert db.count("published_post", "state = 'dry_run'") == 1


def test_a_failing_connector_leaves_a_failed_row_and_an_audit_event(settings, db):
    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    _, variant_id = seed_item_with_variant(db)
    publisher = Publisher(settings, db, {"x": FakeConnector(ready=True, fails=True)})

    result = publisher.publish_variant(variant_id, simulate=False)
    assert result.ok is False
    assert result.state == "failed"
    assert "platform said no" in result.reason
    assert db.count("published_post", "state = 'failed'") == 1
    assert "publish_failed" in {e["action"] for e in db.audit_tail(limit=10)}


def test_a_claim_left_behind_waits_for_a_human(settings, db):
    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    _, variant_id = seed_item_with_variant(db)
    variant = db.variant(variant_id)
    claim = db.insert("published_post", {
        "platform": "x", "variant_id": variant_id, "content_hash": variant["content_hash"],
        "state": "publishing", "dedupe_key": "live:x:1:" + variant["content_hash"],
        "published_at": to_ts(utcnow()),
    })
    publisher = Publisher(settings, db, {"x": FakeConnector(ready=True)})
    unknown = publisher.unknown_claims()
    assert [row["id"] for row in unknown] == [claim]

    resolved = publisher.resolve_claim(claim, "published", external_id="1234",
                                       url="https://example.test/1234", actor="operator")
    assert resolved["state"] == "published"
    assert publisher.unknown_claims() == []
    assert "publish.claim_resolved" in {e["action"] for e in db.audit_tail(limit=5)}


def test_run_due_publishes_scheduled_jobs(settings, db):
    ready_settings(settings)
    account_id = make_account(db, "x", status="ready", scopes=("tweet.write",))
    item_id, variant_id = seed_item_with_variant(db)
    db.schedule_post(variant_id, to_ts(utcnow()), account_id=account_id)
    connector = FakeConnector(ready=True)
    publisher = Publisher(settings, db, {"x": connector})

    results = publisher.run_due(limit=5, worker="test", simulate=False)
    assert [r.state for r in results] == ["published"]
    job = db.scheduled()[0]
    assert job["state"] == "published"
    assert job["attempts"] == 1


def test_run_due_does_nothing_while_the_kill_switch_is_engaged(settings, db):
    from socialmanager.policy import set_kill_switch

    ready_settings(settings)
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    _, variant_id = seed_item_with_variant(db)
    db.schedule_post(variant_id, to_ts(utcnow()))
    connector = FakeConnector(ready=True)
    publisher = Publisher(settings, db, {"x": connector})
    set_kill_switch(db, True, actor="operator")

    assert publisher.run_due(worker="test", simulate=False) == []
    assert connector.calls == []
    assert db.scheduled()[0]["state"] == "pending"


def test_publish_item_can_be_limited_to_one_platform(settings, db):
    ready_settings(settings)
    settings.editorial.max_posts_per_day = {"x": 5, "github": 5}
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    item_id, _ = seed_item_with_variant(db, platform="x")
    db.upsert_variant(item_id, "github", "en", "Testo anche su GitHub, con i dettagli.")
    publisher = Publisher(settings, db, {"x": FakeConnector(ready=True)})

    results = publisher.publish_item(item_id, platforms=["x"], simulate=True)
    assert [r.platform for r in results] == ["x"]
    assert db.count("published_post") == 1


def test_the_default_is_to_simulate_when_the_connector_is_not_ready(settings, db):
    _, variant_id = seed_item_with_variant(db)
    connector = FakeConnector(ready=False)
    publisher = Publisher(settings, db, {"x": connector})
    assert publisher._default_simulate("x") is True
    assert publisher._default_simulate("github") is True   # no connector wired


def test_a_simulation_reports_what_a_real_post_would_have_done(settings, db):
    ready_settings(settings)
    _, variant_id = seed_item_with_variant(db)
    publisher = Publisher(settings, db, {"x": FakeConnector(ready=True, dry_run=True)})
    result = publisher.publish_variant(variant_id, simulate=True)
    assert result.state == "dry_run"
    assert "refused" in result.reason or "held" in result.reason or "allowed" in result.reason
    assert result.policy["outcome"] in {"pass", "block", "needs_review"}
