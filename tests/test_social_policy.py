"""The policy engine: what may pass, what is held, what is refused."""

from datetime import timedelta

import pytest

from socialmanager import config
from socialmanager.db import to_ts, utcnow
from socialmanager.policy import (
    ACTION_DRAFT,
    ACTION_PUBLISH,
    BLOCK,
    PASS,
    REVIEW,
    PolicyEngine,
    kill_switch_engaged,
    set_kill_switch,
)
# ``db`` and ``settings`` are fixtures; importing them registers them here.
from tests.fake_social import (  # noqa: F401
    db,
    make_account,
    make_settings,
    seed_item_with_variant,
    settings,
)


@pytest.fixture()
def engine(settings, db):
    return PolicyEngine(settings, db)


def check(engine, db, variant_id, action=ACTION_PUBLISH):
    return engine.check(db.variant(variant_id), action=action)


def test_drafting_is_allowed_and_publishing_is_not_in_mode_a(engine, db):
    _, variant_id = seed_item_with_variant(db)
    drafting = check(engine, db, variant_id, ACTION_DRAFT)
    assert drafting.outcome == PASS
    publishing = check(engine, db, variant_id, ACTION_PUBLISH)
    assert publishing.outcome == BLOCK
    assert any("mode A" in reason for reason in publishing.reasons)


def test_kill_switch_blocks_everything_but_drafting(engine, db):
    _, variant_id = seed_item_with_variant(db)
    set_kill_switch(db, True, actor="test")
    assert kill_switch_engaged(db, engine.settings) is True
    assert check(engine, db, variant_id, ACTION_PUBLISH).outcome == BLOCK
    assert check(engine, db, variant_id, ACTION_DRAFT).outcome == BLOCK
    set_kill_switch(db, False, actor="test")
    assert check(engine, db, variant_id, ACTION_DRAFT).outcome == PASS


def test_database_kill_switch_wins_over_the_config_file(settings, db):
    settings.autonomy.kill_switch = False
    set_kill_switch(db, True)
    assert kill_switch_engaged(db, settings) is True
    set_kill_switch(db, False)
    assert kill_switch_engaged(db, settings) is False


def test_platform_must_be_allowed(settings, db):
    settings.autonomy.allowed_platforms = ["github"]
    _, variant_id = seed_item_with_variant(db, platform="x")
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    assert "allowed_platforms" in " ".join(report.reasons)


def test_drafting_needs_no_credential(settings, db):
    _, variant_id = seed_item_with_variant(db)
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == PASS
    assert any("no credential is needed" in c.detail for c in report.checks)


def test_account_must_exist_and_be_ready_to_publish(settings, db):
    # Mode B is where the account checks matter: mode A refuses publishing first.
    settings.autonomy.mode = "controlled"
    settings.editorial.min_gap_minutes = 0
    _, variant_id = seed_item_with_variant(db)
    engine = PolicyEngine(settings, db)

    report = check(engine, db, variant_id, ACTION_PUBLISH)
    assert report.outcome == BLOCK
    assert "no social_account row" in " ".join(report.reasons)

    make_account(db, "x", status="draft_only")
    report = check(engine, db, variant_id, ACTION_PUBLISH)
    assert report.outcome == BLOCK
    assert "draft_only" in " ".join(report.reasons)

    make_account(db, "x", status="ready", scopes=("tweet.read",))
    report = check(engine, db, variant_id, ACTION_PUBLISH)
    assert report.outcome == BLOCK
    assert "writing scope" in " ".join(report.reasons)

    make_account(db, "x", status="ready", scopes=("tweet.write",))
    report = check(engine, db, variant_id, ACTION_PUBLISH)
    assert report.outcome == PASS, report.reasons


def test_facts_must_be_sourced(settings, db):
    # No facts at all: held for a human, which is the conservative default.
    _, variant_id = seed_item_with_variant(db, facts=[])
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "no verified facts" in " ".join(report.reasons)

    # The same case with the review rule switched off is a refusal, not a pass.
    settings.policy.review_without_sources = False
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    settings.policy.review_without_sources = True

    _, unsourced = seed_item_with_variant(db, facts=[
        {"claim": "a claim", "source_url": "", "kind": "repo", "verified": True}])
    report = check(PolicyEngine(settings, db), db, unsourced, ACTION_DRAFT)
    assert report.outcome == BLOCK
    assert "no source URL" in " ".join(report.reasons)

    _, unverified = seed_item_with_variant(db, facts=[
        {"claim": "maybe", "source_url": "https://github.com/x/y", "verified": False}])
    report = check(PolicyEngine(settings, db), db, unverified, ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "unverified" in " ".join(report.reasons)


def test_source_host_outside_the_allowlist_needs_a_human(settings, db):
    _, variant_id = seed_item_with_variant(db, facts=[
        {"claim": "from a blog", "source_url": "https://example.com/post", "verified": True}])
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "source_hosts_allowed" in " ".join(report.reasons)


def test_format_limits(settings, db):
    _, empty = seed_item_with_variant(db, body="   ")
    assert check(PolicyEngine(settings, db), db, empty, ACTION_DRAFT).outcome == BLOCK

    _, too_long = seed_item_with_variant(db, body="x" * 400)
    assert check(PolicyEngine(settings, db), db, too_long, ACTION_DRAFT).outcome == BLOCK

    _, too_short = seed_item_with_variant(db, body="ok")
    report = check(PolicyEngine(settings, db), db, too_short, ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "floor" in " ".join(report.reasons)

    _, links = seed_item_with_variant(
        db, body="Due link: https://github.com/a/b e https://github.com/c/d e nient'altro qui.")
    assert check(PolicyEngine(settings, db), db, links, ACTION_DRAFT).outcome == BLOCK

    _, tags = seed_item_with_variant(db, body="Testo valido con #uno #due #tre #quattro qui.")
    assert check(PolicyEngine(settings, db), db, tags, ACTION_DRAFT).outcome == BLOCK


def test_issue_numbers_are_not_counted_as_hashtags(settings, db):
    body = ("Limiti aperti: #21 l'installazione su Windows, #22 richiesta di suddivisione, "
            "il resto e' documentato nel repository.")
    _, variant_id = seed_item_with_variant(db, body=body)
    assert check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT).outcome == PASS


def test_zero_width_characters_are_refused(settings, db):
    _, variant_id = seed_item_with_variant(db, body="Testo normale\u200bconguaglione qui dentro.")
    assert check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT).outcome == BLOCK


def test_secrets_and_personal_data_are_refused(settings, db):
    emails = "Scrivimi a mario.rossi@example.com per il deploy e ti rispondo subito."
    _, variant_id = seed_item_with_variant(db, body=emails)
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    assert "personal data" in " ".join(report.reasons)

    token = "Il token e' ghp_" + "a" * 30 + " e non dovrebbe stare qui."
    _, variant_id = seed_item_with_variant(db, body=token)
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    assert "credential pattern" in " ".join(report.reasons)


def test_env_secret_never_appears_in_a_body(settings, db, monkeypatch):
    monkeypatch.setenv("SOCIAL_TEST_SECRET", "super-secret-value-123")
    settings.x.access_token_env = "SOCIAL_TEST_SECRET"
    _, variant_id = seed_item_with_variant(
        db, body="Controlla con super-secret-value-123 se funziona, grazie mille davvero.")
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    # Either branch proves the point: the value is recognised as a credential
    # (the generic pattern) or as the value of a named variable; never published.
    assert any("credential" in reason or "SOCIAL_TEST_SECRET" in reason
               for reason in report.reasons)


def test_banned_performance_claims_are_refused(settings, db):
    _, variant_id = seed_item_with_variant(
        db, body="Strategia con profitto garantito e senza rischio: seguila adesso.")
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    assert "banned performance claim" in " ".join(report.reasons)


def test_performance_terms_need_a_human_when_publishing(settings, db):
    body = ("Il rendimento misurato sui dati storici e' documentato nel repository, "
            "senza promesse sul futuro.")
    _, variant_id = seed_item_with_variant(db, body=body)
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_PUBLISH)
    assert report.outcome == BLOCK           # mode A refuses the publish outright
    assert any("performance" in reason for reason in report.reasons)


def test_engagement_farming_is_refused(settings, db):
    _, variant_id = seed_item_with_variant(
        db, body="Seguimi e ti seguo, follow back garantito per tutti quelli del settore.")
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK

    mentions = "Grazie a @uno @due @tre @quattro @cinque per i commenti sul progetto."
    _, variant_id = seed_item_with_variant(db, body=mentions)
    assert check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT).outcome == BLOCK


def test_open_graph_comparison_mention_is_not_mass_engagement(settings, db):
    body = ("Rilasciata la v0.2.0 di SelfTrade con il registro del capitale; i dettagli "
            "sono nel repository.")
    _, variant_id = seed_item_with_variant(db, body=body)
    assert check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT).outcome == PASS


def test_instagram_publishing_needs_media(settings, db):
    _, variant_id = seed_item_with_variant(db, platform="instagram", body="Un carosello nuovo.")
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_PUBLISH)
    assert report.outcome == BLOCK   # mode A first, but the media reason is recorded too
    assert any("media" in reason for reason in report.reasons)


def test_duplicate_against_published_text_is_refused(settings, db):
    _, variant_id = seed_item_with_variant(db)
    variant = db.variant(variant_id)
    db.insert("published_post", {
        "platform": "x", "content_hash": variant["content_hash"], "state": "published",
        "dedupe_key": "live:x:0:h", "published_at": to_ts(utcnow()),
        "external_id": "1", "url": "https://x.com/i/status/1",
    })
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == BLOCK
    assert "already published" in " ".join(report.reasons)


def test_a_draft_is_not_its_own_duplicate(settings, db):
    _, variant_id = seed_item_with_variant(db)
    assert check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT).outcome == PASS


def test_identical_text_in_two_drafts_needs_a_human(settings, db):
    _, first = seed_item_with_variant(db, body="Lo stesso testo esatto, senza modifiche qui.")
    _, second = seed_item_with_variant(db, body="Lo stesso testo esatto, senza modifiche qui.")
    report = check(PolicyEngine(settings, db), db, second, ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "same text already exists" in " ".join(report.reasons)


def test_frequency_caps_and_the_gap(settings, db):
    settings.editorial.max_posts_per_day = {"x": 1}
    settings.editorial.min_gap_minutes = 90
    _, variant_id = seed_item_with_variant(db)
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_PUBLISH)
    assert any("mode A" in reason for reason in report.reasons)

    db.insert("published_post", {
        "platform": "x", "content_hash": "other", "state": "published",
        "dedupe_key": "live:x:0:other", "published_at": to_ts(utcnow()),
    })
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_PUBLISH)
    joined = " ".join(report.reasons)
    assert "already published today" in joined or "cap" in joined
    assert engine_next_gap_safe(settings, db)


def engine_next_gap_safe(settings, db) -> bool:
    engine = PolicyEngine(settings, db)
    later = engine.next_allowed_time("x")
    return later > utcnow() + timedelta(minutes=89)


def test_publishing_is_disabled_when_the_daily_cap_is_zero(settings, db):
    settings.editorial.max_posts_per_day = {"x": 0}
    _, variant_id = seed_item_with_variant(db)
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_PUBLISH)
    assert any("max_posts_per_day for x is 0" in reason for reason in report.reasons)


def test_unknown_language_is_held(settings, db):
    settings.editorial.languages = ["it", "fr"]
    _, variant_id = seed_item_with_variant(db, lang="fr")
    assert check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT).outcome == PASS
    settings.editorial.languages = ["it", "en"]
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "not in editorial.languages" in " ".join(report.reasons)


def test_approval_states(settings, db):
    settings.autonomy.mode = "controlled"
    settings.editorial.max_posts_per_day = {"x": 5}
    settings.editorial.min_gap_minutes = 0
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    item_id, variant_id = seed_item_with_variant(db, risk="high")
    engine = PolicyEngine(settings, db)

    report = check(engine, db, variant_id, ACTION_PUBLISH)
    assert report.outcome == REVIEW
    assert any("approval" in reason for reason in report.reasons)

    db.request_approval("content_item", item_id, risk="high", reason="risky")
    assert check(engine, db, variant_id, ACTION_PUBLISH).outcome == REVIEW

    approval_id = db.approvals(state="pending")[0]["id"]
    db.decide_approval(approval_id, "rejected", decided_by="operator")
    report = check(engine, db, variant_id, ACTION_PUBLISH)
    assert report.outcome == BLOCK
    assert any("rejected" in reason for reason in report.reasons)


def test_high_risk_needs_a_human_even_in_advanced_mode(settings, db):
    settings.autonomy.mode = "advanced"
    make_account(db, "x", status="ready", scopes=("tweet.write",))
    item_id, variant_id = seed_item_with_variant(db, risk="sensitive")
    report = check(PolicyEngine(settings, db), db, variant_id, ACTION_PUBLISH)
    assert report.outcome == REVIEW
    assert any("risk" in reason for reason in report.reasons)


def test_a_failing_check_never_becomes_a_pass(settings, db, monkeypatch):
    _, variant_id = seed_item_with_variant(db)
    engine = PolicyEngine(settings, db)

    def explode(context):
        raise RuntimeError("the check itself is broken")

    monkeypatch.setattr(engine, "_checks", lambda: [("broken", explode)])
    report = engine.check(db.variant(variant_id), action=ACTION_DRAFT)
    assert report.outcome == REVIEW
    assert "check failed to run" in " ".join(report.reasons)


def test_evaluate_and_store_writes_the_report(tmp_path, db):
    _, variant_id = seed_item_with_variant(db)
    engine = PolicyEngine(make_settings(tmp_path), db)
    report = engine.evaluate_and_store(variant_id, action=ACTION_DRAFT)
    stored = db.variant(variant_id)
    assert stored["policy_state"] == report.outcome
    assert stored["policy_report"]["checks"]


def test_report_is_json_serialisable(engine, db):
    import json

    _, variant_id = seed_item_with_variant(db)
    payload = check(engine, db, variant_id, ACTION_DRAFT).to_dict()
    json.dumps(payload)
    assert payload["outcome"] in {str(PASS), str(REVIEW), str(BLOCK)}
    assert payload["evaluated_at"].endswith("Z")
