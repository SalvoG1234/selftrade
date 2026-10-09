import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from selftrade.llm import OllamaClient, cosine_similarity

STUB = {
    "chat_content": json.dumps({"family": "rsi_reversion", "rationale": "stub"}),
    "chat_code": 200,
    "embed": [0.1, 0.2, 0.3],
    "fail_embed": False,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "stub-model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/api/chat":
            if STUB["chat_code"] != 200:
                self._send(STUB["chat_code"], {"error": "boom"})
                return
            self._send(200, {"message": {"content": STUB["chat_content"]}})
        elif self.path == "/api/embed":
            if STUB["fail_embed"]:
                self._send(500, {"error": "embed down"})
                return
            self._send(200, {"embeddings": [STUB["embed"]]})
        else:
            self._send(404, {"error": "not found"})


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def reset_stub():
    STUB["chat_content"] = json.dumps({"family": "rsi_reversion", "rationale": "stub"})
    STUB["chat_code"] = 200
    STUB["fail_embed"] = False
    STUB["embed"] = [0.1, 0.2, 0.3]


def client(server):
    return OllamaClient(server, timeout=5.0)


def test_available_and_models(server):
    c = client(server)
    assert c.available() is True
    assert c.models() == ["stub-model"]


def test_chat_json_parses_object(server):
    c = client(server)
    out = c.chat_json("system", "user")
    assert out == {"family": "rsi_reversion", "rationale": "stub"}


def test_chat_json_handles_bad_json(server):
    STUB["chat_content"] = "not json at all"
    c = client(server)
    assert c.chat_json("s", "u") is None


def test_chat_json_handles_http_error(server):
    STUB["chat_code"] = 500
    c = client(server)
    assert c.chat_json("s", "u") is None


def test_chat_json_handles_empty_content(server):
    STUB["chat_content"] = "   "
    c = client(server)
    assert c.chat_json("s", "u") is None


def test_chat_text(server):
    STUB["chat_content"] = "just some text"
    c = client(server)
    assert c.chat_text("s", "u") == "just some text"


def test_embed(server):
    c = client(server)
    assert c.embed("hello") == [0.1, 0.2, 0.3]


def test_embed_failure_returns_none(server):
    STUB["fail_embed"] = True
    c = client(server)
    assert c.embed("hello") is None


def test_unreachable_server_is_graceful():
    c = OllamaClient("http://127.0.0.1:1", timeout=0.5)
    assert c.available() is False
    assert c.chat_json("s", "u") is None
    assert c.chat_text("s", "u") is None
    assert c.embed("x") is None
    assert c.models() == []


def test_cosine_similarity():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([], [1]) == 0.0
    assert cosine_similarity([0, 0], [1, 1]) == 0.0
