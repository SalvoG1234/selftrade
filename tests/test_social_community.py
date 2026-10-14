"""The community agent: classify, cluster, and never answer anyone twice."""

import pytest

from socialmanager.community import CommunityAgent, classify, cluster_key
from socialmanager.github import GitHubConnector

# ``db``/``settings`` are fixtures; importing them registers them here.
from tests.fake_social import db, github_routes, settings  # noqa: F401


@pytest.fixture()
def agent(settings, db):
    return CommunityAgent(settings, db)


def test_classification_in_both_languages():
    cases = [
        ("pip install fails with ModuleNotFoundError on Windows", "install_issue"),
        ("The backtest crashes with a traceback when I run it", "bug_report"),
        ("I think the results are overfitting: the folds are static", "methodology_critique"),
        ("would be nice to have a walk-forward helper", "feature_request"),
        ("how do I run the paper trading mode?", "question"),
        ("great work, thanks for sharing this", "praise"),
        ("crypto giveaway, dm me to invest now", "spam"),
        ("non si installa su Windows, errore di installazione", "install_issue"),
        ("la strategia non funziona, errore nel backtest", "bug_report"),
        ("secondo me c'e' overfitting nella metodologia", "methodology_critique"),
        ("grazie, ottimo lavoro", "praise"),
    ]
    for text, expected in cases:
        assert classify(text).category == expected, text


def test_a_security_report_is_critical():
    result = classify("There is a vulnerability: the token is logged in plain text")
    assert result.category == "security"
    assert result.priority == "critical"
    assert result.sentiment == "negative"


def test_priority_rises_with_urgency_words_and_comments():
    assert classify("cannot start: this is a blocker for me").priority == "critical"
    assert classify("the import fails", comments=4).priority == "critical"


def test_similar_reports_share_a_cluster_key():
    first = cluster_key("pip install fails on Windows with ModuleNotFoundError")
    second = cluster_key("pip install fails on Windows, dependencies missing")
    other = cluster_key("the risk gate refuses my order")
    assert first == second
    assert first != other


def test_ingest_stores_one_row_per_issue_and_classifies_it(settings, db):
    GitHubConnector(settings, db, transport=github_routes()).sync()
    agent = CommunityAgent(settings, db)
    report = agent.ingest_github()
    assert report["new"] == 1
    rows = db.feedback()
    assert len(rows) == 1
    assert rows[0]["category"] == "install_issue"
    assert rows[0]["state"] == "triaged"
    assert rows[0]["summary"]

    again = agent.ingest_github()
    assert again["new"] == 0, "ingesting twice must not duplicate feedback"
    assert db.count("feedback_item") == 1


def test_ingesting_without_a_sync_is_reported_not_crashed(agent):
    report = agent.ingest_github()
    assert report["errors"] and "run `sync` first" in report["errors"][0]


def test_classify_all_handles_rows_stored_before_the_rules(db, agent):
    db.upsert_feedback("github", "issue:1", "pip install fails on Windows")
    db.upsert_feedback("github", "issue:2", "secondo me c'e' overfitting")
    assert db.count("feedback_item", "state = 'new'") == 2
    assert agent.classify_all() == 2
    assert db.count("feedback_item", "state = 'triaged'") == 2


def test_spam_is_never_answered(agent, db):
    feedback_id, _ = db.upsert_feedback("x", "1", "crypto giveaway, dm me to invest now")
    agent.classify_all()
    assert agent.reply_draft(feedback_id)["ok"] is False
    assert "spam" in agent.reply_draft(feedback_id)["reason"]


def test_a_reply_is_a_proposal_that_needs_a_human(agent, db):
    feedback_id, _ = db.upsert_feedback("github", "issue:9", "pip install fails on Windows")
    agent.classify_all()
    result = agent.reply_draft(feedback_id, lang="en")
    assert result["ok"] is True
    draft = result["draft"]
    assert draft["requires_approval"] is True
    assert draft["platform"] == "github"
    assert "run the tests" not in draft["body"]
    assert db.count("published_post") == 0, "drafting a reply must not send anything"


def test_the_same_author_is_not_answered_twice(agent, db):
    first, _ = db.upsert_feedback("github", "issue:9", "pip install fails on Windows",
                                  author="mrossi")
    db.upsert_feedback("github", "issue:10", "another question about the install",
                       author="mrossi")
    agent.classify_all()
    assert agent.reply_draft(first)["ok"] is True
    agent.record_reply("mrossi", "github", "reply-1", url="https://example.test/1")
    again = agent.reply_draft(first)
    assert again["ok"] is False
    assert "already received a reply" in again["reason"]


def test_issue_proposals_are_off_by_default(agent, db):
    for index in range(3):
        db.upsert_feedback("github", f"issue:{index}", "pip install fails on Windows")
    agent.classify_all()
    result = agent.propose_issues(min_size=3)
    assert result["ok"] is False
    assert "allow_issue_proposals" in result["reason"]
    assert result["proposals"] == []
    assert db.count("published_post") == 0


def test_issue_proposals_are_drafts_not_github_calls(settings, db):
    settings.autonomy.allow_issue_proposals = True
    agent = CommunityAgent(settings, db)
    for index in range(3):
        db.upsert_feedback("github", f"issue:{index}", "pip install fails on Windows")
    agent.classify_all()
    result = agent.propose_issues(min_size=3)
    assert result["ok"] is True
    assert result["proposals"]
    proposal = result["proposals"][0]
    assert proposal["requires_approval"] is True
    assert proposal["duplicate_check"] == "already open"
    assert "no user was contacted automatically" in proposal["body"]


def test_report_counts_only_what_was_received(agent, db):
    db.upsert_feedback("github", "issue:1", "pip install fails on Windows")
    agent.classify_all()
    report = agent.report(days=7)
    assert report["total"] == 1
    assert report["by_platform"] == {"github": 1}
    assert report["by_category"]["install_issue"]
    assert "nothing here is estimated" in report["note"]


def test_x_mentions_and_instagram_comments_are_opt_in(agent):
    assert agent.ingest_x_mentions(user_id="1")["errors"] == ["x connector not configured"]
    assert agent.ingest_instagram_comments(media_id="1")["errors"] == \
        ["instagram connector not configured"]


def test_groups_join_recurring_feedback(agent, db):
    for index in range(3):
        db.upsert_feedback("github", f"issue:{index}",
                           "pip install fails on Windows with ModuleNotFoundError")
    agent.classify_all()
    groups = agent.groups(min_size=2)
    assert groups and groups[0]["count"] == 3
    assert groups[0]["suggested_action"]
    assert len(groups[0]["items"]) == 3
