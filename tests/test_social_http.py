"""The shared HTTP client, and the one model call that carries a credential.

Every connector test drives a *fake* transport, which is the right choice - it is what
keeps this repository from reaching a real API - but it also meant ``socialmanager/http.py``
had no test of its own.  The behaviour that lives there is the behaviour that matters when
something goes wrong, so it is exercised directly here: the retry rules, the recorded
``api_call`` row, the redaction of a failure message, and the three refusals that stop a
credential leaving the machine (no redirect chased, no header with a control character, no
clear-text endpoint).

The LLM providers talk to an endpoint from configuration rather than through this client, so
the same two rules are asserted at that call site too: the remote provider is the only place
in the project that sends a remote API key, and a local Ollama with no credential is allowed
to stay on plain ``http``.
"""

from __future__ import annotations

import json

import pytest

from socialmanager.http import (
    HttpClient,
    RequestsTransport,
    header_value,
    require_safe_url,
)
from socialmanager.llm import OllamaProvider, OpenAiCompatibleProvider

# ``db`` is a fixture; importing it by name registers it in this module.  ``FakeTransport``
# records every call (method, url, kwargs), which is how the transport-level rules are read
# back without a socket.
from tests.fake_social import FakeResponse, FakeTransport, db  # noqa: F401

KEY_ENV = "SOCIAL_TEST_LLM_KEY"


def client(transport=None, **kwargs):
    """A client whose sleeps are free, so a retry test is not a slow test."""
    kwargs.setdefault("connector", "test")
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HttpClient(transport=transport or FakeTransport(), **kwargs)


# ------------------------------------------------------------------- redirects
def test_the_default_transport_disables_redirects_and_verifies_tls(monkeypatch):
    """The secure defaults are stated in the transport, not only in the client."""
    import requests

    seen: dict = {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, {})

    monkeypatch.setattr(requests, "request", fake_request)
    RequestsTransport(timeout=5).request("GET", "https://api.example.test/x")
    assert seen["allow_redirects"] is False
    assert seen["verify"] is True
    assert seen["timeout"] == 5


def test_every_request_tells_the_transport_not_to_follow_redirects():
    transport = FakeTransport().add("api.example.test", FakeResponse(200, {"ok": True}))
    client(transport).get("https://api.example.test/v2/me")
    assert transport.calls[0][2]["allow_redirects"] is False


def test_a_redirect_is_an_answer_not_a_new_destination_for_the_token():
    """Two things at once: the 3xx comes back, and only one request was ever made."""
    transport = FakeTransport().add(
        "api.example.test",
        FakeResponse(301, None, {"Location": "https://evil.test/collect"}),
    )
    response = client(transport).get(
        "https://api.example.test/v2/me", headers={"Authorization": "Bearer tok-1234567890"}
    )
    assert response.status == 301 and not response.ok
    assert len(transport.calls) == 1
    assert not any("evil.test" in url for url in transport.urls())


# ------------------------------------------------------------------- the url rule
def test_https_is_required_off_the_machine():
    transport = FakeTransport().add("api.example.test", FakeResponse(200, {}))
    with pytest.raises(ValueError) as exc:
        client(transport).get("http://api.example.test/v2/me")
    assert "only https" in str(exc.value)
    assert transport.calls == [], "the request must be refused before it is sent"


def test_loopback_may_stay_on_http():
    """A local proxy or a development server is legitimate; nothing leaves the machine."""
    transport = FakeTransport().add("127.0.0.1", FakeResponse(200, {"ok": True}))
    assert client(transport).get("http://127.0.0.1:8080/health").status == 200
    assert require_safe_url("http://localhost:3000/x") == "http://localhost:3000/x"
    assert require_safe_url("https://api.x.com/2/tweets") == "https://api.x.com/2/tweets"


# ------------------------------------------------------------------- the header rule
def test_a_header_with_a_newline_is_refused_before_anything_is_sent():
    transport = FakeTransport().add("api.example.test", FakeResponse(200, {}))
    with pytest.raises(ValueError) as exc:
        client(transport).get(
            "https://api.example.test/v2/me",
            headers={"Authorization": "Bearer abc\r\nX-Injected: 1"},
        )
    assert "control character" in str(exc.value)
    assert transport.calls == []


def test_header_value_keeps_ordinary_values_and_refuses_control_characters():
    assert header_value("Authorization", "Bearer abc.def-ghi_123") == "Bearer abc.def-ghi_123"
    for bad in ("a\nb", "a\rb", "a\r\nb", "a\x00b"):
        with pytest.raises(ValueError):
            header_value("Authorization", bad)
    with pytest.raises(ValueError):
        header_value("X-Bad\nHeader", "value")


# ------------------------------------------------------------------- retries
def test_a_429_is_retried_after_the_delay_the_server_asked_for():
    sleeps: list[float] = []
    calls = {"n": 0}

    def route(method, url, kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, {"message": "slow down"}, {"Retry-After": "2"})
        return FakeResponse(200, {"ok": True})

    transport = FakeTransport().add("api.example.test", route)
    response = client(transport, max_retries=2, sleep=sleeps.append).get(
        "https://api.example.test/x"
    )
    assert response.status == 200
    assert calls["n"] == 2
    assert sleeps == [2.0], "Retry-After wins over the exponential default"


def test_a_401_is_not_retried_because_retrying_cannot_fix_it():
    transport = FakeTransport().add("api.example.test", FakeResponse(401, {"message": "bad"}))
    response = client(transport, max_retries=3).get("https://api.example.test/x")
    assert response.status == 401
    assert len(transport.calls) == 1


# ------------------------------------------------------------------- the record
def test_the_record_keeps_the_path_and_never_the_query_string(db):  # noqa: F811
    """A query string can carry a token; the api_call row must not become a copy of it."""
    transport = FakeTransport().add("api.example.test", FakeResponse(200, {"ok": True}))
    client(transport, db=db).get("https://api.example.test/v2/me?access_token=TOPSECRET")
    rows = db.api_calls(limit=5)
    assert rows[0]["endpoint"] == "/v2/me"
    assert "TOPSECRET" not in json.dumps(rows, default=str)


def test_the_configured_timeout_reaches_the_transport():
    transport = FakeTransport().add("api.example.test", FakeResponse(200, {}))
    client(transport, timeout=7.5).get("https://api.example.test/x")
    assert transport.calls[0][2]["timeout"] == 7.5


def test_a_failure_message_cannot_carry_the_token_into_the_record(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SOCIAL_TEST_TOKEN", "tok-abcdef123456")
    secret = "tok-abcdef123456"

    def explode(method, url, options):      # FakeTransport calls the route with three args
        raise RuntimeError(f"connection refused for Bearer {secret}")

    transport = FakeTransport().add("api.example.test", explode)
    client(transport, db=db, secret_names=("SOCIAL_TEST_TOKEN",), max_retries=0).get(
        "https://api.example.test/v2/me"
    )
    row = db.api_calls(limit=5)[0]
    assert row["ok"] == 0
    assert secret not in row["error"]
    assert "redacted" in row["error"]


def test_bearer_is_empty_when_the_variable_is_not_set(monkeypatch):
    monkeypatch.delenv("SOCIAL_TEST_TOKEN", raising=False)
    assert client().bearer("SOCIAL_TEST_TOKEN") == {}
    monkeypatch.setenv("SOCIAL_TEST_TOKEN", "a-value")
    assert client().bearer("SOCIAL_TEST_TOKEN") == {"Authorization": "Bearer a-value"}


# ------------------------------------------------- the model call that carries a key
class _JsonResponse:
    """The subset of ``requests.Response`` the providers read."""

    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_the_remote_model_refuses_a_clear_text_endpoint(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "k" * 24)
    calls: list = []
    provider = OpenAiCompatibleProvider(
        "http://api.example.test/v1", "some-model", api_key_env=KEY_ENV,
        post=lambda *a, **k: calls.append((a, k)),
    )
    result = provider.chat_json("system", "user")
    assert result.ok is False
    assert "only https" in result.error
    assert calls == [], "the key must not be offered to a plain-http endpoint"


def test_the_remote_model_sends_the_key_without_following_redirects(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "k" * 24)
    seen: dict = {}

    def post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _JsonResponse({"choices": [{"message": {"content": "{}"}}]})

    provider = OpenAiCompatibleProvider(
        "https://api.example.test/v1", "some-model", api_key_env=KEY_ENV, post=post
    )
    result = provider.chat_json("system", "user")
    assert result.ok is True
    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    assert seen["allow_redirects"] is False
    assert seen["verify"] is True
    assert seen["headers"]["Authorization"] == "Bearer " + "k" * 24


def test_a_key_with_a_line_break_is_never_put_on_the_wire(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "abc\ndef")
    calls: list = []
    provider = OpenAiCompatibleProvider(
        "https://api.example.test/v1", "some-model", api_key_env=KEY_ENV,
        post=lambda *a, **k: calls.append((a, k)),
    )
    result = provider.chat_json("system", "user")
    assert result.ok is False
    assert "control character" in result.error
    assert calls == []


def test_the_local_model_keeps_plain_http_because_it_has_no_credential():
    """The https rule is about a credential, not about the transport: a LAN Ollama stays legal."""
    seen: dict = {}

    def get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _JsonResponse({"models": []})

    provider = OllamaProvider("http://192.168.1.5:11434", "some-model", get=get)
    assert provider.available() is True
    assert seen["url"] == "http://192.168.1.5:11434/api/tags"
    assert seen["allow_redirects"] is False
