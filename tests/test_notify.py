import io
from contextlib import redirect_stdout

import pytest

from selftrade import notify
from selftrade.config import load_settings


class FakeResponse:
    def __init__(self, code=200):
        self.status_code = code


class FakeSession:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse(self.code)


def test_disabled(tmp_path):
    s = load_settings("config.yaml")
    ok, msg = notify.send(s, "hello")
    assert ok is False
    assert "disabled" in msg


def test_missing_env(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.delenv(s.notify.telegram_token_env, raising=False)
    monkeypatch.delenv(s.notify.telegram_chat_id_env, raising=False)
    ok, msg = notify.send(s, "hello")
    assert ok is False
    assert "not set" in msg


def test_send_success(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "tok123")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    sess = FakeSession(200)
    ok, msg = notify.send(s, "body", session=sess)
    assert ok is True
    assert len(sess.calls) == 1
    assert sess.calls[0]["json"]["chat_id"] == "chat9"


def test_send_http_error(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "tok123")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    ok, msg = notify.send(s, "body", session=FakeSession(500))
    assert ok is False
    assert "500" in msg


def test_token_never_printed(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "SUPERSECRETTOKEN")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok, msg = notify.send(s, "body", session=FakeSession(200))
    assert "SUPERSECRETTOKEN" not in buf.getvalue()
    assert "SUPERSECRETTOKEN" not in msg


def test_paper_summary_text():
    text = notify.paper_summary({"name": "x", "status": "done", "cursor": 10, "total_bars": 10, "n_trades": 3, "total_return": 0.05})
    assert "selftrade paper" in text and "5.00%" in text
