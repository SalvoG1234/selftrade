"""Fakes for the Social Manager tests: nothing here can reach the network.

Same spirit as ``tests/fake_mt5.py`` for the trading side.  Every connector test
drives a :class:`FakeTransport`, so a test that accidentally tries a real HTTP
call fails with a clear error instead of posting something.

This module is not called ``test_*``, so pytest does not collect it.  The
fixtures (``db``, ``settings``) are shared by importing them by name, which is
what registers a fixture in a test module::

    from tests.fake_social import db, settings, make_account
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from socialmanager import config
from socialmanager.db import SocialDB, to_ts, utcnow
from socialmanager.publisher import ExternalPost


class FakeResponse:
    """The subset of ``requests.Response`` the connectors read."""

    def __init__(self, status: int = 200, payload: Any = None,
                 headers: dict[str, str] | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def text(self) -> str:
        return self._text or json.dumps(self._payload if self._payload is not None else {})

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeTransport:
    """Routes calls by URL substring; records every call for assertions."""

    def __init__(self, routes: list[tuple[str, Any]] | None = None):
        self.routes: list[tuple[str, Any]] = list(routes or [])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.default = FakeResponse(404, {"message": "not found"})

    def add(self, needle: str, response: Any) -> FakeTransport:
        self.routes.append((needle, response))
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method.upper(), url, kwargs))
        for needle, response in self.routes:
            if needle in url:
                if callable(response):
                    return response(method, url, kwargs)
                return response
        return self.default

    # ------------------------------------------------------------- assertions
    @property
    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]

    def urls(self) -> list[str]:
        return [url for _, url, _ in self.calls]

    def body_of(self, needle: str) -> dict[str, Any]:
        for method, url, kwargs in self.calls:
            if needle in url:
                return dict(kwargs.get("json") or {})
        return {}

    def count(self, needle: str, method: str | None = None) -> int:
        return sum(
            1 for m, url, _ in self.calls
            if needle in url and (method is None or m == method)
        )


def make_settings(tmp_path, **overrides: Any) -> config.Settings:
    """A fully-defaulted configuration pointed at a temporary database."""
    settings = config.load(str(tmp_path / "missing.yaml"))
    settings.database.url = str(tmp_path / "social.db")
    settings.instagram.media_dir = str(tmp_path / "media")
    settings.dashboard.password_file = str(tmp_path / "dashboard_auth.json")
    settings.notify.enabled = False
    for key, value in overrides.items():
        target: Any = settings
        parts = key.split("__")
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], value)
    return settings


@pytest.fixture()
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture()
def db(tmp_path):
    database = SocialDB.open(tmp_path / "social.db")
    yield database
    database.close()


def seed_release(db: SocialDB, *, tag: str = "v0.2.0", url: str | None = None) -> int:
    """One stored release, enough for the content engine to build a brief."""
    db.upsert_source(
        "release", f"release:{tag}",
        url=url or f"https://github.com/SalvoG1234/selftrade/releases/tag/{tag}",
        title=f"{tag} - ledger and risk gate",
        body="- capital ledger with audit\n- risk gate refuses orders past the cap",
        published_at=to_ts(utcnow()),
        meta={"tag": tag},
    )
    return db.count("content_source")


def seed_item_with_variant(
    db: SocialDB,
    *,
    platform: str = "x",
    lang: str = "en",
    body: str = "A draft with a source: https://github.com/SalvoG1234/selftrade",
    risk: str = "low",
    facts: list[dict[str, Any]] | None = None,
    state: str = "draft",
) -> tuple[int, int]:
    """A content item plus one variant, returning ``(item_id, variant_id)``."""
    item_id = db.create_item(
        "Test item", "dev_update", risk=risk, state=state,
        facts=facts if facts is not None else [
            {"claim": "the repository is public",
             "source_url": "https://github.com/SalvoG1234/selftrade",
             "kind": "repo", "verified": True},
        ],
        source_refs=["release:v0.2.0"],
    )
    variant_id = db.upsert_variant(item_id, platform, lang, body, generator="template")
    return item_id, variant_id


def make_account(db: SocialDB, platform: str = "x", *, status: str = "ready",
                 scopes: tuple[str, ...] = ("tweet.write", "tweet.read")) -> int:
    return db.upsert_account(
        platform, "selftrade_dev", status=status, scopes=scopes,
        token_env="SOCIAL_X_ACCESS_TOKEN",
    )


class FakeConnector:
    """A publisher-facing connector: records calls, returns an external id."""

    def __init__(self, *, ready: bool = True, dry_run: bool = False, fails: bool = False,
                 external_id: str = "999", platform: str = "x"):
        self.ready = ready
        self.dry_run = dry_run
        self.fails = fails
        self.external_id = external_id
        self.platform = platform
        self.calls: list[dict[str, Any]] = []

    def readiness(self) -> dict[str, Any]:
        return {
            "platform": self.platform, "ready": self.ready, "dry_run": self.dry_run,
            "enabled": True, "missing": [] if self.ready else ["no credentials configured"],
            "token_env": "SOCIAL_FAKE_TOKEN", "token_present": self.ready,
        }

    def publish(self, variant: dict[str, Any], account: dict[str, Any] | None = None):
        self.calls.append({"variant_id": variant.get("id"), "body": variant.get("body")})
        if self.fails:
            raise RuntimeError("platform said no")
        return ExternalPost(external_id=self.external_id,
                            url=f"https://example.test/{self.external_id}",
                            meta={"fake": True})


class FakeProvider:
    """An LLM that answers instantly with canned JSON, or refuses."""

    name = "fake"
    model = "fake-model"

    def __init__(self, *, available: bool = True, body: str | None = None,
                 hashtags: str = "#one #two #three #four", fail: bool = False):
        self._available = available
        self.body = body or ("Rilasciata la v0.2.0 di SelfTrade: il registro del capitale e il "
                            "gate di rischio sono nel repository. Metodo e limiti sono "
                            "documentati nel repo.")
        self.hashtags = hashtags
        self.fail = fail
        self.prompts: list[str] = []

    def available(self) -> bool:
        return self._available

    def chat_json(self, system: str, user: str, schema=None, **kwargs: Any):
        from socialmanager.llm import ChatResult

        self.prompts.append(user)
        if self.fail:
            return ChatResult(ok=False, model=self.model, error="boom")
        return ChatResult(ok=True, model=self.model, data={
            "body": self.body, "hashtags": self.hashtags, "alt_text": "card",
            "risk": "low", "unsupported": [],
        })

    def chat_text(self, system: str, user: str, **kwargs: Any):
        from socialmanager.llm import ChatResult

        return ChatResult(ok=True, model=self.model, text=self.body)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "available": self._available}


def github_routes() -> FakeTransport:
    """The canned GitHub API a connector test drives."""
    import base64

    return (
        FakeTransport()
        .add("/readme", FakeResponse(200, {
            "encoding": "base64",
            "content": base64.b64encode(b"# selftrade\n\nA research loop.").decode(),
            "path": "README.md", "sha": "abc",
            "html_url": "https://github.com/SalvoG1234/selftrade#readme",
        }, {"X-RateLimit-Remaining": "55"}))
        .add("/releases", FakeResponse(200, [{
            "tag_name": "v0.2.0", "name": "v0.2.0 - ledger", "draft": False, "prerelease": False,
            "html_url": "https://github.com/SalvoG1234/selftrade/releases/tag/v0.2.0",
            "author": {"login": "SalvoG1234"}, "body": "- ledger\n- risk gate",
            "published_at": "2026-10-12T18:00:00Z",
        }], {"X-RateLimit-Remaining": "54"}))
        .add("/commits", FakeResponse(200, [
            {"sha": "c" * 40, "commit": {"message": "Add the ledger and its tests",
                                          "author": {"date": "2026-10-12T09:00:00Z"}},
             "author": {"login": "SalvoG1234"},
             "html_url": "https://github.com/SalvoG1234/selftrade/commit/" + "c" * 40},
            {"sha": "b" * 40, "commit": {"message": "bump deps",
                                          "author": {"date": "2026-10-12T08:00:00Z"}},
             "html_url": "https://github.com/SalvoG1234/selftrade/commit/" + "b" * 40},
        ], {"X-RateLimit-Remaining": "53"}))
        .add("/issues", FakeResponse(200, [
            {"number": 21, "title": "pip install fails on Windows 11",
             "body": "ModuleNotFoundError on Windows", "state": "open", "comments": 3,
             "labels": [{"name": "bug"}], "user": {"login": "mrossi"},
             "created_at": "2026-10-11T10:00:00Z", "updated_at": "2026-10-12T10:00:00Z",
             "html_url": "https://github.com/SalvoG1234/selftrade/issues/21"},
        ], {"X-RateLimit-Remaining": "52"}))
        .add("/repos/SalvoG1234/selftrade", FakeResponse(200, {
            "full_name": "SalvoG1234/selftrade", "private": False, "default_branch": "master",
            "stargazers_count": 9, "forks_count": 2, "open_issues_count": 4,
            "subscribers_count": 3,
        }, {"X-RateLimit-Remaining": "51"}))
    )
