"""Persistence: the schema, the append-only audit log and the publish queue."""

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from socialmanager.db import (
    SCHEMA_VERSION,
    SocialDB,
    day_start_utc,
    private_mask,
    to_ts,
    utcnow,
)

# ``db`` is a fixture; importing it by name registers it in this module.
from tests.fake_social import db, seed_item_with_variant  # noqa: F401


def test_schema_is_created_and_idempotent(tmp_path):
    path = tmp_path / "social.db"
    first = SocialDB.open(path)
    assert first.health()["schema_version"] == SCHEMA_VERSION
    assert first.health()["tables"] >= 13
    first.close()
    second = SocialDB.open(path)  # reopening must not wipe anything
    assert second.count("content_item") == 0
    second.close()


def test_every_entity_from_the_brief_exists(db):
    names = {
        row["name"] for row in db.all("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table in ("social_account", "content_item", "content_variant", "content_source",
                  "scheduled_post", "published_post", "approval", "feedback_item",
                  "agent_run", "api_call", "analytics_snapshot", "audit_event",
                  "system_setting"):
        assert table in names


def test_audit_chain_verifies_and_detects_tampering(db):
    db.audit("one", actor="a", subject="x", detail={"n": 1})
    db.audit("two", actor="b", subject="y", detail={"n": 2})
    assert db.verify_audit()["ok"] is True
    assert db.verify_audit()["checked"] == 2

    # The triggers refuse an edit and a delete outright.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE audit_event SET action = 'rewritten' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM audit_event WHERE id = 1")

    # Even with the triggers dropped, the chain catches a rewrite.
    db.execute("DROP TRIGGER audit_event_no_update")
    db.execute("UPDATE audit_event SET action = 'rewritten' WHERE id = 1")
    verdict = db.verify_audit()
    assert verdict["ok"] is False
    assert verdict["at"] == 1
    assert "hash mismatch" in verdict["reason"]


def test_audit_never_stores_a_secret_value(db):
    db.audit("login", actor="operator", subject="host", detail={"token": "[redacted:sha256:x]"})
    rows = db.audit_tail(limit=5)
    assert rows[0]["action"] == "login"
    assert rows[0]["detail"]["token"].startswith("[redacted")


def test_source_upsert_reports_changes(db):
    _, created = db.upsert_source("release", "release:v1", body="a", url="u")
    _, again = db.upsert_source("release", "release:v1", body="a", url="u")
    _, changed = db.upsert_source("release", "release:v1", body="b", url="u")
    assert (created, again, changed) == (True, False, True)
    assert db.count("content_source") == 1


def test_variant_unique_per_item_platform_language(db):
    item_id, variant_id = seed_item_with_variant(db)
    again = db.upsert_variant(item_id, "x", "en", "different text")
    assert again == variant_id
    assert db.count("content_variant") == 1
    assert db.variant(variant_id)["body"] == "different text"


def test_scheduled_post_dedupe_key(db):
    _, variant_id = seed_item_with_variant(db)
    first = db.schedule_post(variant_id, "2026-10-14T09:00:00Z")
    second = db.schedule_post(variant_id, "2026-10-14T09:00:00Z")
    other = db.schedule_post(variant_id, "2026-10-15T09:00:00Z")
    assert first == second
    assert other != first
    assert db.count("scheduled_post") == 2


def test_claim_leases_and_releases_expired(db):
    _, variant_id = seed_item_with_variant(db)
    job_id = db.schedule_post(variant_id, to_ts(utcnow()))
    claimed = db.claim_due_posts(to_ts(utcnow()), worker="w1", limit=5)
    assert [row["id"] for row in claimed] == [job_id]
    assert db.get("scheduled_post", job_id)["attempts"] == 1
    # A second worker gets nothing: the row is leased.
    assert db.claim_due_posts(to_ts(utcnow()), worker="w2") == []

    released = db.release_expired_leases(to_ts(utcnow()), lease_seconds=-1)
    assert released == 1
    assert db.get("scheduled_post", job_id)["state"] == "pending"
    assert db.claim_due_posts(to_ts(utcnow()), worker="w2")


def test_finish_post_retries_then_gives_up(db):
    _, variant_id = seed_item_with_variant(db)
    job_id = db.schedule_post(variant_id, to_ts(utcnow()), max_attempts=2)
    db.claim_due_posts(to_ts(utcnow()), worker="w")
    db.finish_post(job_id, state="failed", error="first")
    assert db.get("scheduled_post", job_id)["state"] == "pending"
    db.claim_due_posts(to_ts(utcnow()), worker="w")
    db.finish_post(job_id, state="failed", error="second")
    assert db.get("scheduled_post", job_id)["state"] == "failed"


def test_published_dedupe_key_is_unique(db):
    import sqlite3 as sql

    row = {
        "platform": "x", "content_hash": "h", "state": "published",
        "dedupe_key": "live:x:0:h", "published_at": to_ts(utcnow()),
    }
    db.insert("published_post", row)
    with pytest.raises(sql.IntegrityError):
        db.insert("published_post", dict(row))


def test_feedback_is_unique_per_platform_and_reference(db):
    first, created = db.upsert_feedback("github", "issue:21", "text", url="u")
    second, again = db.upsert_feedback("github", "issue:21", "text changed", url="u")
    assert (first, created) == (second, True)
    assert again is False


def test_daily_and_last_published_helpers(db):
    assert db.published_today("x", day_start_utc()) == 0
    db.insert("published_post", {
        "platform": "x", "content_hash": "h1", "state": "published",
        "dedupe_key": "live:x:0:h1", "published_at": to_ts(utcnow()),
    })
    # A simulated post must not count towards the frequency limits.
    db.insert("published_post", {
        "platform": "x", "content_hash": "h2", "state": "dry_run",
        "dedupe_key": "dry:x:0:h2", "published_at": to_ts(utcnow()),
    })
    assert db.published_today("x", day_start_utc()) == 1
    assert db.last_published_at("x")


def test_settings_round_trip_and_audit(db):
    assert db.setting("mode", "unset") == "unset"
    db.set_setting("mode", "controlled", actor="operator")
    assert db.setting("mode") == "controlled"
    assert db.settings()["mode"] == "controlled"


def test_metrics_upsert_and_availability(db):
    db.record_metric("x", "123", "likes", 4.0, captured_at="2026-10-10T00:00:00Z")
    db.record_metric("x", "123", "likes", 9.0, captured_at="2026-10-10T00:00:00Z")
    db.record_metric("x", "123", "reach", 0.0, captured_at="2026-10-11T00:00:00Z",
                     source="unavailable", available=False, meta={"reason": "too old"})
    rows = db.metrics()
    assert len(rows) == 2
    assert [r["value"] for r in rows if r["metric"] == "likes"] == [9.0]
    assert [r["available"] for r in rows if r["metric"] == "reach"] == [0]


def test_prune_keeps_the_audit_log(db):
    db.record_api_call("github", "GET", "/x", status=200)
    db.start_run("r1", "agent", "cmd")
    db.audit("kept", actor="a")
    removed = db.prune({"api_call": 0, "agent_run": -1})
    assert removed == {}
    assert db.count("api_call") == 1
    assert db.count("audit_event") == 1
    removed = db.prune({"api_call": 1, "agent_run": 1})
    # Nothing is older than a day yet, so nothing is removed - and never the audit.
    assert db.count("audit_event") == 1


def test_backup_writes_a_readable_copy(tmp_path, db):
    db.audit("before backup", actor="a")
    path = db.backup(tmp_path / "backups")
    assert path.exists()
    copy = SocialDB.open(path)
    assert copy.count("audit_event") == 1
    copy.close()


def test_in_memory_database_refuses_backup():
    memory = SocialDB.open(":memory:")
    with pytest.raises(ValueError):
        memory.backup()
    memory.close()


def test_stats_never_invent_numbers(db):
    stats = db.stats()
    assert stats["items"] == 0
    assert stats["published"] == 0
    assert stats["published_by_platform"] == {}
    assert stats["dry_run_posts"] == 0


# ------------------------------------------------------------------ file permissions
# The store holds the audit log, the ingested feedback and the settings, so it is private
# data: group and other bits are removed rather than tolerated.  The mask rule is pure and is
# asserted on every platform; the parts that need real POSIX modes are marked as such.


def test_private_mask_removes_group_and_other_bits_and_only_ever_removes():
    assert private_mask(0o644) == 0o600
    assert private_mask(0o666) == 0o600
    assert private_mask(0o777) == 0o700
    assert private_mask(0o640) == 0o600
    # Already owner-only, or stricter than that: nothing to change.
    assert private_mask(0o600) is None
    assert private_mask(0o400) is None
    assert private_mask(0o500) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_a_new_store_and_its_wal_siblings_are_owner_only(tmp_path):
    store = SocialDB.open(tmp_path / "social.db")
    try:
        store.audit("written", actor="a")          # makes SQLite create -wal
        assert store.file_mode() == "0600"
        summary = store.permissions_summary()
        assert summary["enforced"] is True and summary["ok"] is True
        assert set(summary["files"]) >= {"social.db", "social.db-wal"}
        assert all(mode == "0600" for mode in summary["files"].values())
    finally:
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_opening_a_loose_store_narrows_it(tmp_path):
    path = tmp_path / "social.db"
    store = SocialDB.open(path)
    store.close()
    os.chmod(path, 0o644)                       # as a shared machine would leave it
    reopened = SocialDB.open(path)
    try:
        assert reopened.file_mode() == "0600"
        assert "social.db" in reopened.tightened
        assert reopened.permissions_summary()["ok"] is True
    finally:
        reopened.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_a_backup_carries_the_same_owner_only_mode(tmp_path, db):
    """The copy holds exactly what the store holds, so it cannot be the loose one."""
    path = db.backup(tmp_path / "backups")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_opening_a_store_does_not_append_to_its_audit_log(tmp_path):
    """Narrowing permissions must not become a side effect an operator has to explain."""
    path = tmp_path / "social.db"
    first = SocialDB.open(path)
    first.audit("one", actor="a")
    first.close()
    second = SocialDB.open(path)
    try:
        assert second.count("audit_event") == 1
        assert second.verify_audit()["checked"] == 1
    finally:
        second.close()


def test_the_posix_branch_narrows_every_loose_store_file(monkeypatch, tmp_path):
    """The loop behind the guard, exercised on any platform through its two seams.

    Windows has no mode bits and CI runs Linux, so without this the branch would only ever be
    checked where it happens to execute.  ``_mode_of`` supplies the modes and the module's
    ``os`` supplies ``chmod``, which is all the loop reads.
    """
    from socialmanager import db as db_module

    store = SocialDB.open(tmp_path / "social.db")
    try:
        names = [path.name for path in store.store_files()]
        assert names[0] == "social.db"
        monkeypatch.setattr(store, "_mode_of", lambda path: 0o644)
        chmods: list = []
        monkeypatch.setattr(db_module, "os", SimpleNamespace(
            name="posix", chmod=lambda path, mode: chmods.append((Path(path).name, mode))))

        changed = store.tighten_permissions()
        assert [Path(path).name for path in changed] == names
        assert chmods == [(name, 0o600) for name in names]
        assert store.tightened == names

        # An already owner-only store is left completely alone.
        monkeypatch.setattr(store, "_mode_of", lambda path: 0o600)
        assert store.tighten_permissions() == []
    finally:
        store.close()


def test_the_posix_summary_flags_a_loose_file(monkeypatch, tmp_path):
    from socialmanager import db as db_module

    store = SocialDB.open(tmp_path / "social.db")
    try:
        monkeypatch.setattr(store, "_mode_of", lambda path: 0o644)
        monkeypatch.setattr(db_module, "os", SimpleNamespace(name="posix"))
        summary = store.permissions_summary()
        assert summary["enforced"] is True
        assert summary["ok"] is False
        assert sorted(summary["loose"]) == sorted(p.name for p in store.store_files())

        monkeypatch.setattr(store, "_mode_of", lambda path: 0o600)
        assert store.permissions_summary()["ok"] is True
    finally:
        store.close()


def test_health_reports_the_mode_and_the_permission_summary(db):
    health = db.health()
    assert "file_mode" in health
    summary = health["permissions"]
    assert set(summary) >= {"enforced", "ok", "files", "mode"}
    if os.name != "posix":
        # No POSIX mode bits to read: the file inherits the ACL of the profile it lives in.
        assert summary["enforced"] is False and summary["ok"] is True
    else:
        assert summary["enforced"] is True and summary["ok"] is True
        assert summary["mode"] == "0600"
