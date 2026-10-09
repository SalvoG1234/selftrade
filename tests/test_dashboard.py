import json
import threading
import urllib.error
import urllib.request

import pytest

from selftrade.config import load_settings
from selftrade.dashboard import make_server
from selftrade.memory import Memory
from selftrade.session import Session


@pytest.fixture()
def ctx(tmp_path):
    s = load_settings("config.yaml")
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.session_file = str(tmp_path / "state" / "session.json")
    mem = Memory(s.memory.db)
    mem.start_run("dash test", {})
    mem.add_lesson(1, "a lesson")
    mem.add_to_hall(1, {"family": "rsi_reversion", "params": {}, "risk": {}}, 0.42, {})
    httpd = make_server(s, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, s, mem
    httpd.shutdown()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(base, path, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_health(ctx):
    base, _, _ = ctx
    code, obj = get(base, "/api/health")
    assert code == 200
    assert obj["status"] == "ok"
    assert obj["db_ok"] is True


def test_overview(ctx):
    base, _, _ = ctx
    code, obj = get(base, "/api/overview")
    assert code == 200
    assert obj["stats"]["runs"] >= 1
    assert obj["champion"]["family"] == "rsi_reversion"


def test_all_json_endpoints_respond(ctx):
    base, _, _ = ctx
    for path in (
        "/api/overview",
        "/api/equity",
        "/api/trades",
        "/api/experiments",
        "/api/hall",
        "/api/lessons",
        "/api/history",
        "/api/session",
        "/api/paper",
    ):
        code, obj = get(base, path)
        assert code == 200, path
        assert isinstance(obj, dict), path
    _, hall = get(base, "/api/hall")
    assert len(hall["items"]) == 1
    _, lessons = get(base, "/api/lessons")
    assert lessons["items"][0]["text"] == "a lesson"
    _, history = get(base, "/api/history")
    assert history["items"][0]["note"] == "dash test"


def test_html_page_served(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("text/html")
        html = r.read().decode("utf-8")
    assert "selftrade" in html
    assert "/api/control" in html
    # no remote assets
    assert "http://" not in html.split("<script>")[0].replace("http://127.0.0.1", "")


def test_control_writes_session_request(ctx):
    base, s, _ = ctx
    code, obj = post(base, "/api/control", {"action": "pause"})
    assert code == 200
    assert obj["ok"] is True
    state = Session(s.session_file).read()
    assert state["pending"] == "pause"
    assert state["last_request"]["action"] == "pause"


def test_control_start_sets_generations(ctx):
    base, s, _ = ctx
    code, obj = post(base, "/api/control", {"action": "start", "generations": 7})
    assert code == 200
    state = Session(s.session_file).read()
    assert state["generations_total"] == 7
    assert state["status"] == "running"


def test_control_invalid_action(ctx):
    base, _, _ = ctx
    code, obj = post(base, "/api/control", {"action": "detonate"})
    assert code == 400
    assert "invalid action" in obj["error"]


def test_unknown_path_404(ctx):
    base, _, _ = ctx
    try:
        get(base, "/api/nope")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_paper_endpoints_populated(ctx):
    base, s, mem = ctx
    sid = mem.create_paper_session(
        name="p", genome={"family": "rsi_reversion", "params": {}, "risk": {}},
        window="holdout", total_bars=10, start_time="t0", end_time="t1", initial_cash=1000.0,
    )
    mem.add_paper_trades(sid, [{"direction": "long", "pnl": 3.0, "exit_reason": "signal"}])
    mem.add_paper_equity(sid, [("t0", 1000.0), ("t1", 1003.0)])
    _, paper = get(base, "/api/paper")
    assert paper["session"]["session_id"] == sid
    assert paper["session"]["n_trades"] == 1
    _, eq = get(base, "/api/equity")
    assert len(eq["points"]) == 2
    _, tr = get(base, "/api/trades")
    assert tr["items"][0]["pnl"] == 3.0
