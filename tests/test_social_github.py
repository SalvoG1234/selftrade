"""The GitHub connector: read-only by construction, honest about what it could not read."""

import json

import pytest

from socialmanager import obs
from socialmanager.github import GitHubConnector

# ``db``/``settings`` are fixtures imported by name.
from tests.fake_social import (  # noqa: F401
    FakeResponse,
    FakeTransport,
    db,
    github_routes,
    settings,
)


@pytest.fixture()
def connector(settings, db):
    return GitHubConnector(settings, db, transport=github_routes())


def test_sync_reads_the_expected_kinds(connector, db):
    report = connector.sync()
    assert report.ok
    assert report.fetched["release"] == 1
    assert report.fetched["commit"] == 2         # both came back from the API
    assert db.count("content_source", "kind = 'commit'") == 1   # "bump deps" was skipped
    assert report.fetched["issue"] == 1
    assert report.fetched["readme"] == 1
    kinds = {row["kind"] for row in db.sources(limit=50)}
    assert kinds == {"release", "commit", "issue", "readme"}


def test_commit_messages_without_news_value_are_skipped(connector):
    report = connector.sync()
    assert any("no news value" in item for item in report.skipped)


def test_the_connector_only_issues_get(connector):
    connector.sync()
    assert set(connector.http.transport.methods) == {"GET"}  # type: ignore[attr-defined]


def test_discussions_stay_off_without_a_token(connector):
    report = connector.sync(kinds=("discussion",))
    assert any("read:discussion" in item or "disabled" in item for item in report.skipped)
    assert connector._graphql("query { viewer { login } }", {}) is None


def test_discussions_use_a_read_only_query(settings, db, monkeypatch):
    monkeypatch.setenv("SOCIAL_GITHUB_TOKEN", "github_pat_" + "a" * 30)
    settings.github.discussions = True
    transport = github_routes().add("/graphql", FakeResponse(200, {"data": {"repository": {
        "discussions": {"nodes": [{
            "number": 4, "title": "Overfitting in the folds", "url": "https://github.com/d/4",
            "body": "how are the folds built?", "createdAt": "2026-10-11T10:00:00Z",
            "updatedAt": "2026-10-12T10:00:00Z", "author": {"login": "quant"},
            "category": {"name": "Q&A"}, "comments": {"totalCount": 2},
        }]}}}}))
    connector = GitHubConnector(settings, db, transport=transport)
    report = connector.sync()
    assert report.fetched.get("discussion") == 1
    posts = [c for c in transport.calls if c[0] == "POST"]
    assert posts, "the GraphQL query is a POST"
    assert "mutation" not in posts[0][2]["json"]["query"].lower()
    assert db.count("content_source", "kind = 'discussion'") == 1

    with pytest.raises(ValueError):
        connector._graphql("mutation { deleteIssue(input: {}) { clientMutationId } }", {})


def test_a_rate_limited_read_is_retried_then_reported(settings, db):
    attempts = {"n": 0}

    def flaky(method, url, kwargs):
        if "/releases" in url:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return FakeResponse(429, {"message": "rate limited"}, {"Retry-After": "0"})
            return FakeResponse(200, [], {})
        return FakeResponse(404, {"message": "not found"})

    transport = FakeTransport([
        ("/commits", FakeResponse(200, [], {})),
        ("/issues", FakeResponse(200, [], {})),
        ("/releases", flaky),
    ])
    connector = GitHubConnector(settings, db, transport=transport, sleep=lambda _s: None)
    report = connector.sync()
    assert attempts["n"] == 2
    assert not any("releases" in error for error in report.errors)
    assert any(row["status"] == 429 for row in db.api_calls(limit=50))


def test_a_transport_failure_becomes_a_reported_error(settings, db):
    class Exploding:
        def request(self, method, url, **kwargs):
            raise TimeoutError("connection timed out")

    connector = GitHubConnector(settings, db, transport=Exploding(), sleep=lambda _s: None)
    report = connector.sync()
    assert not report.ok
    assert any("releases" in error for error in report.errors)
    calls = db.api_calls(limit=10)
    assert calls and calls[0]["status"] == 0
    assert "TimeoutError" in calls[0]["error"]


def test_the_connector_exposes_no_write_operation(connector):
    """There is no method to call: the brief forbids touching the repository."""
    for name in ("create_issue", "update_issue", "close_issue", "delete_issue", "merge",
                 "commit", "push", "edit_readme", "create_release", "post"):
        assert not hasattr(connector, name), name


def test_readiness_names_the_variable_never_the_value(settings, db, monkeypatch):
    monkeypatch.delenv("SOCIAL_GITHUB_TOKEN", raising=False)
    anonymous = GitHubConnector(settings, db, transport=github_routes()).readiness()
    assert anonymous["authenticated"] is False
    assert anonymous["mode"] == "anonymous"
    assert anonymous["token_env"] == "SOCIAL_GITHUB_TOKEN"
    assert any("not set" in note for note in anonymous["missing"])

    monkeypatch.setenv("SOCIAL_GITHUB_TOKEN", "github_pat_" + "b" * 30)
    authenticated = GitHubConnector(settings, db, transport=github_routes()).readiness()
    assert authenticated["authenticated"] is True
    assert authenticated["token_fingerprint"].startswith("sha256:")
    assert "b" * 30 not in json.dumps(authenticated)


def test_health_reports_the_repository(connector):
    health = connector.health()
    assert health["ok"] is True
    assert health["repo"] == "SalvoG1234/selftrade"
    assert health["default_branch"] == "master"
    assert health["stars"] == 9


def test_repo_numbers_are_stored_as_metrics_with_their_source(connector, db):
    connector.sync()
    metrics = {row["metric"]: row for row in db.metrics(subject_ref="repo:SalvoG1234/selftrade")}
    assert metrics["stars"]["value"] == 9.0
    assert metrics["stars"]["source"] == "api"


def test_disabled_connector_does_nothing(settings, db):
    settings.github.enabled = False
    connector = GitHubConnector(settings, db, transport=github_routes())
    report = connector.sync()
    assert report.skipped == ["connector disabled in configuration"]
    assert db.count("content_source") == 0


def test_the_token_never_reaches_the_log(settings, db, monkeypatch, capsys):
    secret = "github_pat_" + "c" * 30
    monkeypatch.setenv("SOCIAL_GITHUB_TOKEN", secret)
    obs.configure()
    obs.clear_ring()
    transport = FakeTransport([
        ("/issues", FakeResponse(500, {"message": f"oops {secret}"})),
    ])
    connector = GitHubConnector(settings, db, transport=transport, sleep=lambda _s: None)
    connector.sync(kinds=("issue",))
    logged = json.dumps(obs.tail(limit=50))
    assert secret not in logged
    assert "redacted" in logged
    assert secret not in json.dumps(db.api_calls(limit=20))


def test_sync_writes_an_audit_event(connector, db):
    connector.sync()
    actions = {row["action"] for row in db.audit_tail(limit=20)}
    assert "github.sync" in actions
