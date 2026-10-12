"""Tests for the dashboard: panels, login, the loop controls and the live chart.

The chart is exercised with an injected Binance client, so these tests never
touch the network; the login is exercised end to end over real HTTP with cookies.
"""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

from selftrade.config import load_settings
from selftrade.dashboard import make_server
from selftrade.memory import Memory
from selftrade.session import Session


def _settings(tmp_path):
    s = load_settings("config.yaml")
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.session_file = str(tmp_path / "state" / "session.json")
    # The login password is state too: point it at the tmp dir so no test can ever
    # write the credential into the checked-out `state/` (or read one from there).
    s.dashboard.password_file = str(tmp_path / "state" / "dashboard_auth.json")
    return s


def _serve(settings):
    httpd = make_server(settings, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture()
def ctx(tmp_path):
    s = _settings(tmp_path)
    s.dashboard.auth = False  # the panel tests are not about the login
    mem = Memory(s.memory.db)
    mem.start_run("dash test", {})
    mem.add_lesson(1, "a lesson")
    mem.add_to_hall(1, {"family": "rsi_reversion", "params": {}, "risk": {}}, 0.42, {})
    httpd, base = _serve(s)
    yield base, s, mem
    httpd.shutdown()


@pytest.fixture()
def auth_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFTD_TEST_DASH_PW", "s3cret-pw")
    s = _settings(tmp_path)
    s.dashboard.auth = True
    s.dashboard.password_env = "SELFTD_TEST_DASH_PW"
    Memory(s.memory.db)
    httpd, base = _serve(s)
    yield base, s
    httpd.shutdown()


@pytest.fixture()
def default_pw_ctx(tmp_path, monkeypatch):
    """A login with **no** environment password: the bootstrap one is what it accepts."""
    monkeypatch.delenv("SELFTD_TEST_DASH_PW", raising=False)
    s = _settings(tmp_path)
    s.dashboard.auth = True
    s.dashboard.password_env = "SELFTD_TEST_DASH_PW"
    Memory(s.memory.db)
    httpd, base = _serve(s)
    yield base, s
    httpd.shutdown()


@pytest.fixture()
def loop_ctx(tmp_path):
    """A dashboard whose loop controls run the fake loop instead of an evolution run."""
    from selftrade.loop import LoopRunner

    from tests.fake_loop import fake_argv

    s = _settings(tmp_path)
    s.dashboard.auth = False
    Memory(s.memory.db)
    httpd, base = _serve(s)
    runner = LoopRunner(s, argv_builder=lambda n: fake_argv(s.session_file, n))
    httpd.RequestHandlerClass.runner = runner
    yield base, s, runner
    child = runner._child
    if child is not None and child.poll() is None:
        Session(s.session_file).request("stop")
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - the fake always stops
            child.kill()
    runner.close()
    httpd.shutdown()


def _wait(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


def get(base, path, headers=None, cookie=None):
    req = urllib.request.Request(base + path, headers=dict(headers or {}))
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(base, path, obj, headers=None, cookie=None):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8")), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), e.headers


def get_http_error(base, path, cookie=None):
    req = urllib.request.Request(base + path)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        urllib.request.urlopen(req, timeout=10)
        return None
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------- panels


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
    # the loop controls, ready to be wired to the loop state the page reads
    for button in ('id="ctl-start"', 'id="ctl-pause"', 'id="ctl-resume"', 'id="ctl-stop"'):
        assert button in html
    assert "applyButtons" in html
    # no remote assets
    assert "http://" not in html.split("<script>")[0].replace("http://127.0.0.1", "")


def test_control_refuses_steering_when_no_loop_runs(ctx):
    base, s, _ = ctx
    for action in ("pause", "resume", "stop"):
        code, obj, _ = post(base, "/api/control", {"action": action})
        assert code == 409, action
        assert obj["ok"] is False
        assert "no loop is running" in obj["error"]
    # and nothing was written: a request no process will ever read is not a control
    assert not Path(s.session_file).exists()


def test_control_invalid_action(ctx):
    base, _, _ = ctx
    code, obj, _ = post(base, "/api/control", {"action": "detonate"})
    assert code == 400
    assert "invalid action" in obj["error"]


# ---------------------------------------------------------- the loop controls
# The buttons are only worth having if they move a *process*.  These run the fake
# loop (tests/fake_loop.py, an `evolve --session` that obeys the session file)
# instead of an evolution run: same plumbing, no minutes per generation.


def test_control_start_launches_the_loop(loop_ctx):
    base, _, runner = loop_ctx
    code, obj, _ = post(base, "/api/control", {"action": "start", "generations": 7})
    assert code == 200 and obj["ok"] is True
    loop = obj["loop"]
    assert loop["running"] is True and loop["owned"] is True
    assert loop["pid"] == runner._child.pid
    assert loop["generations_total"] == 7
    assert loop["log"].endswith("loop.log")
    assert obj["session"]["status"] == "running"
    assert obj["session"]["pid"] == runner._child.pid


def test_session_endpoint_reports_the_loop(loop_ctx):
    base, _, runner = loop_ctx
    _, before = get(base, "/api/session")
    assert before["loop"]["running"] is False
    assert post(base, "/api/control", {"action": "start", "generations": 30})[0] == 200
    _, after = get(base, "/api/session")
    assert after["status"] == "running"
    assert after["loop"]["running"] is True
    assert after["loop"]["pid"] == runner._child.pid


def test_control_start_twice_is_refused(loop_ctx):
    base, _, _ = loop_ctx
    assert post(base, "/api/control", {"action": "start", "generations": 30})[0] == 200
    code, obj, _ = post(base, "/api/control", {"action": "start", "generations": 30})
    assert code == 409 and "already running" in obj["error"]
    assert obj["loop"]["running"] is True


def test_control_steers_the_running_loop(loop_ctx):
    base, s, runner = loop_ctx
    assert post(base, "/api/control", {"action": "start", "generations": 200})[0] == 200
    session = Session(s.session_file)
    code, obj, _ = post(base, "/api/control", {"action": "pause"})
    assert code == 200 and "generation boundary" in obj["note"]
    assert _wait(lambda: session.read()["status"] == "paused")
    code, obj, _ = post(base, "/api/control", {"action": "resume"})
    assert code == 200 and _wait(lambda: session.read()["status"] == "running")
    code, obj, _ = post(base, "/api/control", {"action": "stop"})
    assert code == 200 and "stop" in obj["note"].lower()
    assert _wait(lambda: runner.status()["running"] is False, timeout=20)
    assert session.read()["status"] == "stopped"


def test_unknown_path_404(ctx):
    base, _, _ = ctx
    assert get_http_error(base, "/api/nope") == 404


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


# ---------------------------------------------------------------- login


def test_login_page_is_served_to_an_anonymous_visitor(auth_ctx):
    base, _ = auth_ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert r.status == 200
    assert "sign in" in html  # the login form is part of the page


def test_whoami_reports_anonymous_without_a_cookie(auth_ctx):
    base, _ = auth_ctx
    code, obj = get(base, "/api/whoami")
    assert code == 200
    assert obj["auth_enabled"] is True
    assert obj["authenticated"] is False


def test_api_is_protected_without_a_cookie(auth_ctx):
    base, _ = auth_ctx
    for path in ("/api/overview", "/api/market", "/api/hall"):
        assert get_http_error(base, path) == 401, path
    code, obj, _ = post(base, "/api/control", {"action": "pause"})
    assert code == 401


def test_wrong_password_is_refused(auth_ctx):
    base, _ = auth_ctx
    code, obj, _ = post(base, "/api/login", {"password": "nope"})
    assert code == 401
    assert "wrong password" in obj["error"]
    assert get_http_error(base, "/api/overview") == 401


def test_login_then_everything_works_and_logout_closes_it(auth_ctx):
    base, _ = auth_ctx
    code, obj, headers = post(base, "/api/login", {"password": "s3cret-pw"})
    assert code == 200
    set_cookie = headers.get("Set-Cookie")
    assert set_cookie and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    cookie = set_cookie.split(";", 1)[0]

    code, who = get(base, "/api/whoami", cookie=cookie)
    assert who["authenticated"] is True
    code, ov = get(base, "/api/overview", cookie=cookie)
    assert code == 200 and "stats" in ov
    # the cookie is accepted (401 would mean it was not); the refusal is the loop's,
    # not the login's
    code, obj, _ = post(base, "/api/control", {"action": "pause"}, cookie=cookie)
    assert code == 409 and "no loop is running" in obj["error"]

    _, _, logout_headers = post(base, "/api/logout", {}, cookie=cookie)
    assert "Max-Age=0" in (logout_headers.get("Set-Cookie") or "")
    # the browser no longer sends the cookie, so the next request is anonymous
    assert get_http_error(base, "/api/overview") == 401


def test_a_replayed_cookie_lives_until_its_ttl(auth_ctx):
    """The documented boundary of a stateless cookie.

    ``/api/logout`` clears the browser's cookie; there is no server-side
    revocation list, so a *copied* token stays valid until ``session_ttl_s``.
    """
    base, _ = auth_ctx
    _, _, headers = post(base, "/api/login", {"password": "s3cret-pw"})
    cookie = headers.get("Set-Cookie").split(";", 1)[0]
    post(base, "/api/logout", {}, cookie=cookie)
    code, _ = get(base, "/api/overview", cookie=cookie)
    assert code == 200


def test_a_token_expires_with_the_ttl():
    import time

    from selftrade.dashboard import Auth

    auth = Auth(True, "pw", ttl_s=5)
    assert auth.check(auth.issue()) is True
    stale = str(int(time.time()) - 600)
    assert auth.check(f"{stale}.{auth._sign(stale)}") is False
    assert auth.check("garbage") is False
    assert auth.check(None) is False


def test_a_tampered_cookie_is_refused(auth_ctx):
    base, _ = auth_ctx
    _, _, headers = post(base, "/api/login", {"password": "s3cret-pw"})
    cookie = headers.get("Set-Cookie").split(";", 1)[0]
    name, _, token = cookie.partition("=")
    forged = f"{name}={token[:-3]}xyz"
    assert get_http_error(base, "/api/overview", cookie=forged) == 401


def test_too_many_failures_are_rate_limited(auth_ctx):
    base, _ = auth_ctx
    for _ in range(8):
        post(base, "/api/login", {"password": "nope"})
    code, obj, _ = post(base, "/api/login", {"password": "s3cret-pw"})
    assert code == 429
    assert "too many" in obj["error"]


def test_cross_origin_post_is_refused(auth_ctx):
    base, _ = auth_ctx
    _, _, headers = post(base, "/api/login", {"password": "s3cret-pw"})
    cookie = headers.get("Set-Cookie").split(";", 1)[0]
    code, obj, _ = post(
        base, "/api/control", {"action": "pause"},
        headers={"Origin": "http://evil.example"}, cookie=cookie,
    )
    assert code == 403


def _sign_in(base, password):
    """Log in and return the cookie header, or None when the login was refused."""
    code, obj, headers = post(base, "/api/login", {"password": password})
    if code != 200:
        return None
    return headers.get("Set-Cookie").split(";", 1)[0]


# ------------------------------------------------- the bootstrap password + changing it


def test_the_bootstrap_password_0000_is_what_a_fresh_login_accepts(default_pw_ctx):
    """The first login, end to end: no stored credential, no environment variable."""
    base, s = default_pw_ctx
    path = Path(s.dashboard.password_file)
    assert not path.exists()

    cookie = _sign_in(base, "0000")
    assert cookie, "the bootstrap password must sign in"
    code, who = get(base, "/api/whoami", cookie=cookie)
    assert who["authenticated"] is True

    # and it is the only password that works, and signing in writes nothing
    assert _sign_in(base, "password") is None
    assert not path.exists()


def test_the_settings_card_reports_the_source_and_never_the_hash(default_pw_ctx):
    base, s = default_pw_ctx
    cookie = _sign_in(base, "0000")
    code, cfg = get(base, "/api/settings", cookie=cookie)
    assert code == 200
    pw = cfg["password"]
    assert pw["source"] == "default"
    assert pw["is_default"] is True
    assert pw["changeable"] is True
    assert pw["min_length"] == s.dashboard.password_min_length
    assert pw["file"] == s.dashboard.password_file
    assert cfg["dashboard"]["password file"] == s.dashboard.password_file

    # after a change: the source moves, the record does not leak.  The change rotates the
    # signing key, so this reads the settings with the *new* cookie from its response.
    code, obj, headers = post(base, "/api/password", {
        "current": "0000", "new": "a-real-password", "confirm": True}, cookie=cookie)
    assert code == 200
    cookie = headers.get("Set-Cookie").split(";", 1)[0]
    raw = Path(s.dashboard.password_file).read_text(encoding="utf-8")
    record = json.loads(raw)
    code, cfg = get(base, "/api/settings", cookie=cookie)
    body = json.dumps(cfg)
    assert record["hash"] not in body and record["salt"] not in body
    assert "a-real-password" not in body
    assert cfg["password"]["source"] == "stored"
    assert cfg["password"]["is_default"] is False
    assert cfg["password"]["changed_at"]
    assert cfg["password"]["changed_by"] == "dashboard"


def test_changing_the_password_needs_the_current_one_a_confirmation_and_a_usable_new_one(default_pw_ctx):
    base, s = default_pw_ctx
    path = Path(s.dashboard.password_file)
    cookie = _sign_in(base, "0000")
    good = {"current": "0000", "new": "a-real-password", "confirm": True}

    # no confirmation: refused, and nothing written
    code, obj, _ = post(base, "/api/password", {k: v for k, v in good.items() if k != "confirm"},
                        cookie=cookie)
    assert code == 400 and obj["confirm_required"] is True
    assert not path.exists()

    # wrong current password: refused, and nothing written
    code, obj, _ = post(base, "/api/password", {**good, "current": "0001"}, cookie=cookie)
    assert code == 401 and "current password is wrong" in obj["error"]
    assert not path.exists()

    # too short, and the same as the current one: refused, nothing written
    code, obj, _ = post(base, "/api/password", {**good, "new": "000"}, cookie=cookie)
    assert code == 400 and "at least" in obj["error"]
    code, obj, _ = post(base, "/api/password", {**good, "new": "0000"}, cookie=cookie)
    assert code == 400 and "current one" in obj["error"]
    assert not path.exists()
    assert _sign_in(base, "0000") is not None  # the old password still works, unchanged

    # the real change
    code, obj, headers = post(base, "/api/password", good, cookie=cookie)
    assert code == 200 and obj["changed"] is True and obj["sessions_signed_out"] is True
    assert obj["password"]["source"] == "stored"
    assert "HttpOnly" in (headers.get("Set-Cookie") or "")
    raw = path.read_text(encoding="utf-8")
    assert path.exists() and "a-real-password" not in raw
    assert json.loads(raw)["changed_by"] == "dashboard"


def test_a_changed_password_signs_every_other_session_out(default_pw_ctx):
    """Two signed-in sessions; the change rotates the signing key and only the session
    that made it survives - its fresh cookie comes back in the same response.

    The cookie is a signed *timestamp* (``issued.signature``), so two logins inside the
    same second are byte-identical and a "sign this one token out" scheme could not tell
    them apart: rotating the key is what makes a password change mean anything.
    """
    base, _ = default_pw_ctx
    cookie_a = _sign_in(base, "0000")
    cookie_b = _sign_in(base, "0000")
    assert get(base, "/api/overview", cookie=cookie_b)[0] == 200

    code, obj, headers = post(base, "/api/password", {
        "current": "0000", "new": "a-real-password", "confirm": True}, cookie=cookie_a)
    assert code == 200
    fresh = headers.get("Set-Cookie").split(";", 1)[0]

    assert get_http_error(base, "/api/overview", cookie=cookie_a) == 401
    assert get_http_error(base, "/api/overview", cookie=cookie_b) == 401
    assert get(base, "/api/overview", cookie=fresh)[0] == 200

    # and the login itself changed: the new password works, the old one does not
    assert _sign_in(base, "a-real-password") is not None
    assert _sign_in(base, "0000") is None


def test_the_change_endpoint_is_behind_the_login(default_pw_ctx):
    base, _ = default_pw_ctx
    code, obj, _ = post(base, "/api/password", {
        "current": "0000", "new": "a-real-password", "confirm": True})
    assert code == 401
    code, obj, _ = post(base, "/api/password", {"confirm": True}, headers={"Origin": "http://evil.example"})
    assert code == 403
    assert not Path(default_pw_ctx[1].dashboard.password_file).exists()


def test_wrong_current_passwords_are_rate_limited(default_pw_ctx):
    base, _ = default_pw_ctx
    cookie = _sign_in(base, "0000")
    for _ in range(8):
        post(base, "/api/password", {"current": "nope", "new": "a-real-password", "confirm": True},
             cookie=cookie)
    code, obj, _ = post(base, "/api/password", {
        "current": "0000", "new": "a-real-password", "confirm": True}, cookie=cookie)
    assert code == 429 and "too many" in obj["error"]


def test_an_environment_password_cannot_be_changed_from_the_page(auth_ctx):
    """The environment wins, so the page refuses instead of writing a hash nothing reads."""
    base, s = auth_ctx
    cookie = _sign_in(base, "s3cret-pw")
    code, cfg = get(base, "/api/settings", cookie=cookie)
    assert cfg["password"]["source"] == "environment"
    assert cfg["password"]["changeable"] is False
    assert "environment" in cfg["password"]["note"]

    code, obj, _ = post(base, "/api/password", {
        "current": "s3cret-pw", "new": "a-real-password", "confirm": True}, cookie=cookie)
    assert code == 400 and "environment" in obj["error"]
    assert not Path(s.dashboard.password_file).exists()
    assert _sign_in(base, "s3cret-pw") is not None  # unchanged, of course


def test_a_corrupt_credential_locks_the_login_rather_than_accepting_0000(tmp_path, monkeypatch):
    monkeypatch.delenv("SELFTD_TEST_DASH_PW", raising=False)
    s = _settings(tmp_path)
    s.dashboard.auth = True
    s.dashboard.password_env = "SELFTD_TEST_DASH_PW"
    Memory(s.memory.db)
    path = Path(s.dashboard.password_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a credential", encoding="utf-8")
    httpd, base = _serve(s)
    try:
        assert _sign_in(base, "0000") is None
        assert _sign_in(base, "anything") is None
    finally:
        httpd.shutdown()
    path.unlink()
    httpd, base = _serve(s)
    try:  # and deleting it is the documented recovery
        assert _sign_in(base, "0000") is not None
    finally:
        httpd.shutdown()


def test_a_password_written_before_startup_is_the_one_the_login_accepts(tmp_path, monkeypatch):
    """What `python -m selftrade password --set` writes, the dashboard reads."""
    from selftrade.credentials import CredentialStore

    monkeypatch.delenv("SELFTD_TEST_DASH_PW", raising=False)
    s = _settings(tmp_path)
    s.dashboard.auth = True
    s.dashboard.password_env = "SELFTD_TEST_DASH_PW"
    Memory(s.memory.db)
    CredentialStore(s.dashboard.password_file).set("written-by-the-cli", by="cli")
    httpd, base = _serve(s)
    try:
        assert _sign_in(base, "written-by-the-cli") is not None
        assert _sign_in(base, "0000") is None
        cookie = _sign_in(base, "written-by-the-cli")
        code, cfg = get(base, "/api/settings", cookie=cookie)
        assert cfg["password"]["changed_by"] == "cli"
        assert cfg["password"]["is_default"] is False
    finally:
        httpd.shutdown()


def test_lan_bind_without_auth_is_refused(tmp_path):
    s = _settings(tmp_path)
    s.dashboard.auth = False
    with pytest.raises(ValueError) as exc:
        make_server(s, "0.0.0.0", 0)
    assert "login" in str(exc.value)


# ---------------------------------------------------------------- live market


def _fake_klines(*args, **kwargs):
    idx = pd.date_range("2024-01-01", periods=6, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1, 2, 3, 4, 5, 6.0],
            "high": [2, 3, 4, 5, 6, 7.0],
            "low": [0.5, 1, 2, 3, 4, 5.0],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
            "volume": [10, 11, 12, 13, 14, 15.0],
        },
        index=idx,
    )


def _fake_ticker(*args, **kwargs):
    return {
        "last": 6.5, "open": 1.0, "high": 7.0, "low": 0.5, "volume": 75.0,
        "quote_volume": 300.0, "change": 5.5, "change_pct": 5.5, "trades": 42.0,
    }


def test_market_endpoint_returns_candles_and_ticker(ctx, monkeypatch):
    import selftrade.dashboard as dash

    monkeypatch.setattr(dash, "fetch_klines", _fake_klines)
    monkeypatch.setattr(dash, "fetch_ticker", _fake_ticker)
    base, _, _ = ctx
    code, obj = get(base, "/api/market?symbol=BTC/USDT&interval=1m&bars=10")
    assert code == 200
    assert obj["ok"] is True
    assert obj["symbol"] == "BTC/USDT" and obj["interval"] == "1m"
    assert len(obj["candles"]) == 6
    first = obj["candles"][0]
    assert set(first) == {"t", "o", "h", "l", "c", "v"}
    assert first["o"] == 1.0 and first["c"] == 1.5
    assert first["t"] > 0
    assert obj["ticker"]["last"] == 6.5
    assert obj["latency_ms"] >= 0
    assert obj["cached"] is False


def test_market_endpoint_caches_within_the_refresh_window(ctx, monkeypatch):
    import selftrade.dashboard as dash

    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return _fake_klines()

    monkeypatch.setattr(dash, "fetch_klines", counting)
    monkeypatch.setattr(dash, "fetch_ticker", _fake_ticker)
    base, _, _ = ctx
    get(base, "/api/market?symbol=ETH/USDT&interval=5m&bars=10")
    _, second = get(base, "/api/market?symbol=ETH/USDT&interval=5m&bars=10")
    assert calls["n"] == 1
    assert second["cached"] is True


def test_market_endpoint_reports_a_failure_instead_of_raising(ctx, monkeypatch):
    import selftrade.dashboard as dash

    def boom(*a, **k):
        raise RuntimeError("Binance unreachable")

    monkeypatch.setattr(dash, "fetch_klines", boom)
    base, _, _ = ctx
    code, obj = get(base, "/api/market?symbol=BTC/USDT&interval=1m&bars=10")
    assert code == 200
    assert obj["ok"] is False
    assert "unreachable" in obj["error"]
    assert obj["candles"] == []


def test_market_endpoint_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/market") == 401


def test_page_boot_carries_the_market_defaults(ctx):
    base, s, _ = ctx
    s.dashboard.market_symbol = "ETH/USDT"
    s.dashboard.market_interval = "5m"
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert '"symbol": "ETH/USDT"' in html
    assert '"interval": "5m"' in html
    assert "__BOOT__" not in html and "__REFRESH__" not in html


# --------------------------------------------------- the Binance key panel


def _fake_key_health(settings, memory=None, symbols=None, transport=None):
    """A report shaped exactly like `binance check`, with no broker and no network."""
    return {
        "provider": "binance-testnet",
        "base_url": settings.binance.base_url,
        "enabled": True,
        "host_is_testnet": True,
        "production": False,
        "mode": "paper",
        "armed": False,
        "credentials": {
            "SELFTD_BINANCE_API_KEY": False,
            "SELFTD_BINANCE_API_SECRET": False,
        },
        "steps": [
            {"name": "credentials", "ok": False, "detail": "missing SELFTD_BINANCE_API_KEY"},
            {"name": "read_only", "ok": True, "detail": "0 call(s) this run (none): 0 POST"},
        ],
        "problems": ["no credentials: export SELFTD_BINANCE_API_KEY and SELFTD_BINANCE_API_SECRET"],
        "warnings": ["the key carries production permissions"],
        "hints": ["set them for the process that runs the CLI"],
        "clock": {"server_time_ms": None, "local_time_ms": 1, "offset_ms": 0.0, "ok": False},
        "signature": {"ok": False, "path": "/api/v3/account", "error": None},
        "permissions": {"accountType": None, "canTrade": None},
        "trade_ready": None,
        "account": {"quote_asset": "USDT", "balances": [], "balances_total": 0},
        "open_orders": {"count": 0, "orders": []},
        "limits": {"weight_used": 0, "max_weight_per_min": 1200, "cooldown_s": 0.0},
        "http": {"calls": 0, "posts": 0, "methods": [], "paths": [], "read_only": True},
        "ok": False,
        "verdict": "problems found",
    }


def test_binance_panel_returns_the_check_report(ctx, monkeypatch):
    import selftrade.dashboard as dash

    monkeypatch.setattr(dash, "key_health", _fake_key_health)
    base, s, _ = ctx
    code, obj = get(base, "/api/binance")

    assert code == 200
    assert obj["provider"] == "binance-testnet"
    assert obj["base_url"] == s.binance.base_url
    # read-only and unarmed: the panel can never be a way to send an order
    assert obj["armed"] is False
    assert obj["http"]["posts"] == 0 and obj["http"]["read_only"] is True
    assert [st["name"] for st in obj["steps"]] == ["credentials", "read_only"]
    assert obj["credentials"]["SELFTD_BINANCE_API_KEY"] is False
    assert obj["limits"]["max_weight_per_min"] == 1200
    assert obj["ok"] is False and obj["cached"] is False
    assert obj["refresh_s"] == s.dashboard.key_refresh_s
    assert obj["latency_ms"] >= 0


def test_binance_panel_caches_and_rechecks_on_demand(ctx, monkeypatch):
    import selftrade.dashboard as dash

    calls = {"n": 0}

    def counting(settings, memory=None, symbols=None, transport=None):
        calls["n"] += 1
        return _fake_key_health(settings)

    monkeypatch.setattr(dash, "key_health", counting)
    base, _, _ = ctx
    get(base, "/api/binance")
    _, second = get(base, "/api/binance")
    assert calls["n"] == 1  # one probe per refresh window, even with many tabs
    assert second["cached"] is True

    _, forced = get(base, "/api/binance?force=1")
    assert calls["n"] == 2
    assert forced["cached"] is False


def test_binance_panel_reports_a_disabled_rail_without_touching_the_network(ctx, monkeypatch):
    """The rail off is the case worth showing, and it must cost zero requests."""
    import selftrade.binance as bn_mod

    def boom(*a, **k):  # pragma: no cover - a request here would be the bug
        raise AssertionError("the key panel sent a request with the rail disabled")

    monkeypatch.setattr(bn_mod, "RequestsTransport", boom)
    base, s, _ = ctx
    s.binance.enabled = False
    code, obj = get(base, "/api/binance")

    assert code == 200
    assert obj["ok"] is False and obj["verdict"] == "not checked"
    assert "disabled in config" in obj["problems"][0]
    assert obj["enabled"] is False and obj["armed"] is False
    assert obj["http"]["calls"] == 0 and obj["http"]["posts"] == 0
    assert any("binance.enabled=true" in hint for hint in obj["hints"])
    assert obj["checked_at"]


def test_binance_panel_reports_missing_credentials_instead_of_raising(ctx, monkeypatch):
    """The rail is on but the key is not exported: the panel says so, and sends nothing."""
    import selftrade.binance as bn_mod

    class RefusingTransport:
        """Building it is fine; *using* it is the bug this test looks for."""

        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):  # pragma: no cover - a request here would be the bug
            raise AssertionError("the key panel must not send a request without credentials")

    monkeypatch.setattr(bn_mod, "RequestsTransport", RefusingTransport)
    monkeypatch.delenv("SELFTD_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("SELFTD_BINANCE_API_SECRET", raising=False)
    base, s, _ = ctx
    s.binance.enabled = True
    code, obj = get(base, "/api/binance")

    assert code == 200
    assert obj["ok"] is False and obj["verdict"] == "problems found"
    assert "no credentials" in obj["problems"][0]
    assert "must not send" not in obj["problems"][0]
    assert obj["armed"] is False
    assert obj["http"]["calls"] == 0 and obj["http"]["posts"] == 0


def test_binance_panel_survives_a_probe_that_raises(ctx, monkeypatch):
    import selftrade.dashboard as dash

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dash, "key_health", boom)
    base, _, _ = ctx
    code, obj = get(base, "/api/binance")

    assert code == 200  # a panel must not be able to break the page
    assert obj["ok"] is False and obj["verdict"] == "not checked"
    assert "could not run" in obj["problems"][0]
    assert obj["steps"] == [] and obj["hints"]


def test_binance_panel_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/binance") == 401


def test_binance_panel_renders_in_the_page(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert 'id="bk-status"' in html and 'id="bk-body"' in html
    assert "Binance key" in html
    assert "async function refreshKey(force)" in html
    assert "const KEY_REFRESH" in html
    assert "__KEY_REFRESH__" not in html


# --------------------------------------------------- the emergency stop


def test_the_emergency_stop_starts_clear_and_costs_nothing(ctx):
    """The panel always shows a state, and reading it is a file read - not a request."""
    base, s, _ = ctx
    code, obj = get(base, "/api/killswitch")

    assert code == 200
    assert obj["ok"] is True and obj["blocked"] is False
    assert obj["reason"] == "" and obj["triggered_by"] == ""
    assert obj["events"] == 0 and obj["history"] == []
    assert obj["path"] == str(Path(s.session_file).parent / "kill_switch.json")
    assert obj["checked_at"]
    assert not (Path(s.session_file).parent / "kill_switch.json").exists()
    assert Memory(s.memory.db).risk_events() == []


def test_arming_without_confirmation_is_refused_and_changes_nothing(ctx):
    base, s, _ = ctx
    code, obj, _ = post(base, "/api/killswitch", {"action": "arm", "reason": "because"})

    assert code == 400
    assert obj["confirm_required"] is True and obj["ok"] is False
    assert not (Path(s.session_file).parent / "kill_switch.json").exists()
    _, state = get(base, "/api/killswitch")
    assert state["blocked"] is False
    assert Memory(s.memory.db).risk_events() == []


def test_a_string_confirm_is_not_a_confirmation(ctx):
    """``"confirm": "true"`` is a client that did not think about it."""
    base, s, _ = ctx
    code, obj, _ = post(
        base, "/api/killswitch", {"action": "arm", "confirm": "true", "reason": "x"}
    )
    assert code == 400 and obj["confirm_required"] is True
    _, state = get(base, "/api/killswitch")
    assert state["blocked"] is False


def test_arming_from_the_dashboard_stops_the_real_gate_and_is_audited(ctx):
    """The button flips the switch the broker obeys - not a dashboard-only flag."""
    from selftrade.execution import (
        AccountSnapshot,
        KillSwitch,
        OrderRequest,
        RiskGate,
        Side,
        kill_switch_path,
    )

    base, s, _ = ctx
    code, obj, _ = post(
        base, "/api/killswitch",
        {"action": "arm", "reason": "halting until I read the news", "confirm": True},
    )

    assert code == 200
    assert obj["ok"] is True and obj["action"] == "arm"
    assert obj["blocked"] is True
    assert obj["reason"] == "halting until I read the news" and obj["triggered_by"] == "dashboard"
    assert obj["triggered_at"] and obj["event_id"] is not None and obj["event_error"] is None
    assert obj["history"][-1]["event"] == "trigger" and obj["history"][-1]["by"] == "dashboard"
    assert obj["events"] == 1

    # the gate the paper loop and the Binance adapter call refuses, reading the same file
    switch = KillSwitch(kill_switch_path(s))
    assert switch.is_blocked() is True
    decision = RiskGate(max_positions=1).check(
        OrderRequest(client_id="c1", symbol="BTC/USDT", side=Side.BUY, qty=1.0, stop_loss_pct=-0.01),
        AccountSnapshot(ts="2024-01-01T00:00:00+00:00", cash=10_000.0, positions_value=0.0,
                        equity=10_000.0, realized_pnl=0.0, unrealized_pnl=0.0,
                        day_pnl_pct=0.0, drawdown=0.0, open_positions=0),
        [],
        price=100.0,
        kill_switch=switch,
    )
    assert decision.ok is False
    assert decision.reasons[0].startswith("kill switch is engaged")

    # and the audit row is where every other risk decision is filed
    events = Memory(s.memory.db).risk_events()
    assert len(events) == 1
    assert events[0]["kind"] == "kill_switch"
    assert "halting until I read the news" in events[0]["reason"]
    assert events[0]["detail"]["action"] == "arm" and events[0]["detail"]["by"] == "dashboard"
    assert events[0]["run_id"] is None


def test_disarming_lets_orders_through_again_and_is_audited_too(ctx):
    base, s, _ = ctx
    post(base, "/api/killswitch", {"action": "arm", "reason": "news", "confirm": True})
    code, obj, _ = post(
        base, "/api/killswitch", {"action": "reset", "reason": "read it, risk is fine", "confirm": True}
    )

    assert code == 200
    assert obj["blocked"] is False and obj["reason"] == ""
    assert obj["triggered_by"] == "" and obj["action"] == "reset"
    assert obj["history"][-1]["event"] == "reset"
    assert obj["events"] == 2  # both directions stay in the switch's own trail
    kinds = [e["kind"] for e in Memory(s.memory.db).risk_events()]
    assert kinds == ["kill_switch", "kill_switch"]


def test_an_unknown_action_is_refused_and_nothing_is_written(ctx):
    base, s, _ = ctx
    code, obj, _ = post(base, "/api/killswitch", {"action": "detonate", "confirm": True})
    assert code == 400
    assert "arm|reset" in obj["error"] and obj["ok"] is False
    assert "blocked" not in obj
    assert not (Path(s.session_file).parent / "kill_switch.json").exists()
    assert Memory(s.memory.db).risk_events() == []


def test_a_second_click_cannot_bury_the_first_reason(ctx):
    base, s, _ = ctx
    post(base, "/api/killswitch", {"action": "arm", "reason": "first cause", "confirm": True})
    _, obj, _ = post(base, "/api/killswitch", {"action": "arm", "reason": "second", "confirm": True})
    assert obj["blocked"] is True and obj["reason"] == "first cause"
    assert [h["event"] for h in obj["history"]] == ["trigger", "trigger_again"]
    entries = [e["kind"] for e in Memory(s.memory.db).risk_events()]
    assert entries == ["kill_switch", "kill_switch"]  # the repeat is still recorded


def test_a_burst_of_clicks_cannot_lose_an_event(ctx):
    """Two tabs (or a double click) landing together must not drop one from the trail."""
    base, s, _ = ctx
    results = []

    def click(n):
        results.append(post(base, "/api/killswitch",
                             {"action": "arm", "reason": f"click {n}", "confirm": True}))

    threads = [threading.Thread(target=click, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    from selftrade.execution import KillSwitch, kill_switch_path

    assert all(code == 200 for code, _, _ in results)
    _, state = get(base, "/api/killswitch")
    assert state["blocked"] is True
    assert state["events"] == 6                          # the file kept every action
    assert state["reason"] in {f"click {n}" for n in range(6)}  # the first writer's cause
    assert [h["event"] for h in state["history"]] == ["trigger_again"] * 5
    events = list(KillSwitch(kill_switch_path(s)).state()["history"])
    assert [e["event"] for e in events] == ["trigger"] + ["trigger_again"] * 5
    assert events[0]["by"] == "dashboard" and events[0]["reason"] == state["reason"]
    assert len(Memory(s.memory.db).risk_events()) == 6    # and every one is audited


def test_a_long_or_messy_reason_is_collapsed_and_capped(ctx):
    base, s, _ = ctx
    _, obj, _ = post(
        base, "/api/killswitch",
        {"action": "arm", "reason": "  too\n\nmany   spaces " + "x" * 900, "confirm": True},
    )
    reason = obj["reason"]
    assert reason.startswith("too many spaces x")
    assert len(reason) == 300
    assert "\n" not in reason and "  " not in reason


def test_a_database_that_cannot_be_written_still_arms_the_switch_and_says_so(ctx, monkeypatch):
    """The switch file is the control; a locked audit table must not silently pass."""
    import selftrade.dashboard as dash

    class BrokenMemory:
        def __init__(self, *a, **k):
            pass

        def add_risk_event(self, *a, **k):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(dash, "Memory", BrokenMemory)
    base, s, _ = ctx
    code, obj, _ = post(base, "/api/killswitch", {"action": "arm", "reason": "stop", "confirm": True})

    assert code == 200
    assert obj["blocked"] is True
    assert obj["event_id"] is None
    assert "database is locked" in obj["event_error"]
    assert dash.KillSwitchPanel(s).snapshot()["blocked"] is True


def test_the_kill_switch_endpoint_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/killswitch") == 401
    code, obj, _ = post(base, "/api/killswitch", {"action": "arm", "confirm": True})
    assert code == 401


def test_a_cross_origin_kill_switch_post_is_refused(ctx):
    base, s, _ = ctx
    code, obj, _ = post(
        base, "/api/killswitch", {"action": "arm", "confirm": True},
        headers={"Origin": "http://evil.example"},
    )
    assert code == 403
    _, state = get(base, "/api/killswitch")
    assert state["blocked"] is False


def test_the_emergency_stop_renders_in_the_page(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert "Emergency stop" in html
    for anchor in ('id="ks-status"', 'id="ks-body"', 'id="ks-arm"', 'id="ks-reset"',
                   'id="ks-confirm"', 'id="ks-reason"', 'id="ks-msg"'):
        assert anchor in html, anchor
    # the state is unknown until it is read, so neither direction starts clickable
    assert 'onclick="ksAsk(\'arm\')" disabled' in html
    assert 'onclick="ksAsk(\'reset\')" disabled' in html
    assert "async function refreshSwitch()" in html
    assert "confirm: true" in html            # the page always confirms explicitly
    assert "await refreshSwitch();" in html    # read with the panels, not on the key window
    assert "__KS_" not in html


# ---------------------------------------------------------------- risk limits

def _seed_session(mem, equities, cash, *, day="2024-01-01", initial=10_000.0):
    """A paper session whose recorded curve is exactly ``equities``."""
    sid = mem.create_paper_session(
        name="risk test",
        genome={"family": "rsi_reversion", "params": {}, "risk": {}},
        window="recent",
        total_bars=len(equities),
        start_time=f"{day}T00:00:00+00:00",
        end_time=f"{day}T23:00:00+00:00",
        initial_cash=initial,
        settings={},
    )
    mem.add_paper_equity(
        sid, [(f"{day} {i:02d}:00:00+00:00", float(eq)) for i, eq in enumerate(equities)]
    )
    mem.update_paper_cursor(sid, len(equities), float(cash), "active")
    return sid


def _risk_check(risk, name):
    return next(c for c in risk["checks"] if c["name"] == name)


def _snapshot_from(account):
    """The account the panel reported, in the shape the gate takes."""
    from selftrade.execution import AccountSnapshot

    return AccountSnapshot(
        ts="2024-01-01T02:00:00+00:00",
        cash=account["cash"],
        positions_value=account["positions_value"],
        equity=account["equity"],
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        day_pnl_pct=account["day_pnl_pct"] / 100.0,
        drawdown=account["drawdown_pct"] / 100.0,
        open_positions=account["open_positions"],
    )


def test_the_risk_panel_measures_the_session_against_the_sealed_caps(ctx):
    """Drawdown, today's P&L and exposure, with the room left under each cap."""
    from selftrade.risk_rules import LIMITS

    base, s, mem = ctx
    _seed_session(mem, [10_000.0, 10_200.0, 9_800.0], cash=9_300.0)
    code, risk = get(base, "/api/risk")

    assert code == 200 and risk["ok"] is True
    # the caps are the sealed ones, and the panel says which digest they are
    assert risk["limits"] == LIMITS.as_dict()
    assert risk["pinned"] is True and risk["sealed"] is True
    assert risk["session"]["session_id"] == 1 and risk["session"]["points"] == 3
    # the account is reconstructed from the curve: peak 10,200, close 9,800
    account = risk["account"]
    assert account["equity"] == pytest.approx(9_800.0)
    assert account["peak_equity"] == pytest.approx(10_200.0)
    assert account["drawdown_pct"] == pytest.approx((1 - 9_800 / 10_200) * 100, rel=1e-9)
    assert account["day_pnl_pct"] == pytest.approx(-2.0, rel=1e-9)  # 9,800 from 10,000
    # exposure is what the session is carrying: equity - cash
    assert account["positions_value"] == pytest.approx(500.0)
    assert account["open_positions"] == 1

    dd = _risk_check(risk, "max_drawdown")
    assert dd["limit"] == 15.0 and dd["source"] == "sealed" and dd["arms"] is True
    assert dd["effect"] == "arms the kill switch" and dd["unit"] == "pp"
    assert dd["headroom"] == pytest.approx(15.0 - dd["value"])

    day = _risk_check(risk, "max_daily_loss")
    assert day["limit"] == 2.0 and day["ok"] is True
    # room is measured from the signed P&L: 2.0% of cap + (-2.0%) of the day
    assert day["headroom"] == pytest.approx(0.0, abs=1e-6)

    pct = _risk_check(risk, "position_pct")
    assert pct["value"] == pytest.approx(500 / 9_800 * 100, rel=1e-9)
    assert pct["limit"] == 5.0 and pct["source"] == "sealed" and pct["arms"] is False
    assert pct["effect"] == "refuses the order"
    assert pct["ok"] is False and pct["headroom"] < 0     # 5.10% on a 9,800 equity
    assert risk["breached"] == ["position_pct"]
    assert risk["would_arm"] is False                      # nothing halts trading yet

    lev = _risk_check(risk, "leverage")
    assert lev["source"] == "config" and lev["unit"] == "x"
    assert lev["limit"] == pytest.approx(s.risk_gate.max_leverage)
    assert lev["value"] == pytest.approx(500 / 9_800, rel=1e-9)
    # the panel leads with what is nearest, and the halting limits come first
    names = [c["name"] for c in risk["checks"]]
    assert set(names) == {"max_drawdown", "max_daily_loss", "position_pct", "leverage"}
    assert names.index("max_drawdown") < names.index("leverage")
    assert risk["nearest"]["name"] == "max_daily_loss"     # the tightest account limit


def test_the_panel_reports_the_limits_the_gate_actually_enforces(ctx):
    """The page must not show a limit the gate decides differently.

    Same account, same order: the two account limits the panel renders are compared to
    the checks the real gate puts on a decision - values, caps, verdict and wording -
    and then the alert the panel calls "halts" is followed all the way to the broker
    arming the switch the page sits next to.
    """
    from selftrade.execution import KillSwitch, OrderRequest, PaperBroker, RiskGate, Side

    base, s, mem = ctx
    _seed_session(mem, [10_000.0, 10_300.0, 9_700.0], cash=9_700.0)
    _, risk = get(base, "/api/risk")
    gate = RiskGate.from_config(s.risk_gate)
    decision = gate.check(
        OrderRequest("c1", "BTC/USDT", Side.BUY, qty=1.0, stop_loss_pct=-0.01),
        _snapshot_from(risk["account"]),
        [],
        price=100.0,
    )
    for name in ("max_drawdown", "max_daily_loss"):
        g = next(c for c in decision.checks if c["name"] == name)
        p = _risk_check(risk, name)
        assert (g["value"], g["limit"], g["ok"]) == (p["value"], p["limit"], p["ok"]), name
        assert g["detail"] == p["detail"] and g["reason"] == p["reason"]

    # the same account, but past the daily halt: the panel says the stop is coming,
    # the gate raises the alert, and the broker arms the very switch the page shows
    _seed_session(mem, [10_000.0, 10_000.0, 9_600.0], cash=9_600.0)
    _, risk = get(base, "/api/risk")
    day = _risk_check(risk, "max_daily_loss")
    assert day["ok"] is False and day["headroom"] < 0
    assert risk["would_arm"] is True and "max_daily_loss" in risk["breached"]

    snapshot = _snapshot_from(risk["account"])
    gate = RiskGate.from_config(s.risk_gate)
    decision = gate.check(
        OrderRequest("c2", "BTC/USDT", Side.BUY, qty=1.0, stop_loss_pct=-0.01),
        snapshot, [], price=100.0,
    )
    assert decision.ok is False
    assert decision.alerts == ["daily loss limit breached: new orders halted"]

    class FromThePanel(PaperBroker):
        """The broker the panel is describing: the same account, the same limits."""

        def get_account_balance(self):
            return snapshot

    df = pd.DataFrame(
        [(100.0, 101.0, 99.0, 100.0)] * 3,
        columns=["open", "high", "low", "close"],
        index=pd.date_range("2024-01-01", periods=3, freq="4h", tz="UTC"),
    )
    switch = KillSwitch(Path(s.session_file).parent / "kill_switch.json")
    broker = FromThePanel(
        df, symbol="BTC/USDT", cash=9_600.0, gate=gate, kill_switch=switch,
        memory=Memory(s.memory.db),
    )
    order = broker.submit_order(
        OrderRequest("c3", "BTC/USDT", Side.BUY, qty=1.0, stop_loss_pct=-0.01)
    )
    assert order.status.value == "rejected"
    assert switch.is_blocked() is True          # the red row is a real stop
    assert "daily halt" in order.reject_reason


def test_a_profitable_day_adds_room_instead_of_eating_the_daily_cap(ctx):
    """Room is the distance to the halt, so a gain on the day pushes it up."""
    base, s, mem = ctx
    _seed_session(mem, [10_000.0, 10_100.0], cash=10_100.0)
    _, risk = get(base, "/api/risk")
    day = _risk_check(risk, "max_daily_loss")
    assert day["value"] == pytest.approx(1.0)      # |today's P&L|, as the gate shows it
    assert day["headroom"] == pytest.approx(3.0)   # 2% of cap + 1% of gains
    assert day["ok"] is True and risk["would_arm"] is False


def test_a_session_with_no_curve_is_measured_at_its_initial_cash(ctx):
    base, s, mem = ctx
    sid = _seed_session(mem, [], cash=10_000.0)
    _, risk = get(base, "/api/risk")
    assert risk["ok"] is True and risk["session"]["session_id"] == sid
    assert risk["session"]["points"] == 0 and risk["session"]["as_of"] is None
    assert risk["account"]["equity"] == pytest.approx(10_000.0)
    assert risk["account"]["drawdown_pct"] == 0.0 and risk["account"]["day_pnl_pct"] == 0.0
    assert _risk_check(risk, "max_drawdown")["headroom"] == pytest.approx(15.0)
    assert _risk_check(risk, "max_daily_loss")["headroom"] == pytest.approx(2.0)
    assert risk["breached"] == [] and risk["would_arm"] is False


def test_no_session_is_said_rather_than_shown_as_four_zeros(ctx):
    base, s, _ = ctx
    code, risk = get(base, "/api/risk")
    assert code == 200
    assert risk["ok"] is False and "no paper session" in risk["error"]
    assert risk["checks"] == [] and risk["session"] is None and risk["breached"] == []
    assert risk["hint"] and "paper --create" in risk["hint"]
    # the caps are still reported: what is missing is the account, not the limits
    assert risk["limits"]["MAX_DRAWDOWN_PCT"] == 15.0 and risk["pinned"] is True


def test_reading_the_risk_panel_changes_nothing(ctx):
    """A GET that armed, refused or audited something would be a bug in this panel."""
    base, s, mem = ctx
    _seed_session(mem, [10_000.0, 9_000.0], cash=9_000.0)
    get(base, "/api/risk")
    assert Memory(s.memory.db).risk_events() == []
    assert not (Path(s.session_file).parent / "kill_switch.json").exists()


def test_reading_the_risk_panel_leaves_the_alert_ledger_alone(ctx):
    """Asking what is owed must not consume it: `pending` is a question, not a send."""
    base, s, mem = ctx
    _seed_session(mem, [10_000.0, 9_600.0], cash=9_600.0)   # past the daily cap
    _, risk = get(base, "/api/risk")
    assert risk["pending"], "the breach should be owed to somebody"
    assert not (Path(s.session_file).parent / "notify_state.json").exists()
    _, again = get(base, "/api/risk")
    assert again["pending"] == risk["pending"]              # still owed, not consumed


def test_the_risk_panel_says_what_the_channel_still_owes(ctx):
    """A red row nobody has been told about is the one row worth acting on."""
    base, s, mem = ctx
    _seed_session(mem, [10_000.0], cash=10_000.0)           # flat: nothing to say
    _, risk = get(base, "/api/risk")
    assert risk["pending"] == []

    # a loss inside the day leaves the account past the daily cap with nothing stopped
    _seed_session(mem, [10_000.0, 9_600.0], cash=9_600.0)
    _, risk = get(base, "/api/risk")
    assert risk["would_arm"] is True
    pending = risk["pending"]
    assert [p["kind"] for p in pending] == ["warning"]
    assert pending[0]["band"] == "past" and pending[0]["reason"] == "max_daily_loss"
    assert "past its cap" in pending[0]["headline"]

    # an engaged stop supersedes it: the halt message already carries every limit
    from selftrade.execution import KillSwitch, kill_switch_path

    KillSwitch(kill_switch_path(s)).trigger("daily loss limit breached", by="risk_gate")
    _, risk = get(base, "/api/risk")
    assert [p["kind"] for p in risk["pending"]] == ["halt"]
    assert risk["pending"][0]["headline"].startswith("selftrade HALT")


def test_the_stop_says_when_the_program_armed_it_and_with_how_much_room(ctx):
    """The numbers exist only at the refusal: the panel shows what the gate saw there."""
    from selftrade.execution import KillSwitch, arming_detail, kill_switch_path

    base, s, _ = ctx
    _, armed, _ = post(
        base, "/api/killswitch", {"action": "arm", "reason": "weekend", "confirm": True}
    )
    assert armed["triggered_by"] == "dashboard"
    assert armed["detail"] == {}        # a human has no refusal to report

    class Snap:
        equity = 8_456.0
        drawdown = 0.2387
        day_pnl_pct = -0.004
        ts = "2026-10-10T08:00:00+00:00"

    checks = [
        {"name": "max_drawdown", "value": 23.87, "limit": 15.0, "headroom": -8.87,
         "headroom_frac": -0.59, "unit": "pp", "ok": False, "arms": True},
        {"name": "max_daily_loss", "value": 0.4, "limit": 2.0, "headroom": 1.6,
         "headroom_frac": 0.8, "unit": "pp", "ok": True, "arms": True},
    ]
    KillSwitch(kill_switch_path(s)).reset("the gate is about to do it")
    KillSwitch(kill_switch_path(s)).trigger(
        "account drawdown 23.87% breaks the sealed 15.0% limit: trading halts",
        by="risk_gate",
        detail=arming_detail(Snap(), checks, alerts=["drawdown limit breached: new orders halted"]),
    )

    _, state = get(base, "/api/killswitch")
    assert state["triggered_by"] == "risk_gate"
    names = [c["name"] for c in state["detail"]["checks"]]
    assert names == ["max_drawdown", "max_daily_loss"]      # only the halting ones
    assert state["detail"]["drawdown"] == 0.2387

    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert "at arming" in html and "of margin left" in html and "past it" in html


def test_the_alerts_card_lists_what_was_actually_sent(ctx, monkeypatch):
    """The history is the channel's own ledger: a message that never left is not in it."""
    from selftrade import notify as notify_mod
    from selftrade.execution import KillSwitch, kill_switch_path

    class Sends:
        def post(self, url, json=None, timeout=None):  # noqa: A002 - requests' signature
            return type("R", (), {"status_code": 200})()

    base, s, mem = ctx
    _seed_session(mem, [10_000.0, 10_000.0, 8_400.0], cash=8_400.0)

    _, empty = get(base, "/api/alerts")
    assert empty["ok"] is True and empty["items"] == [] and empty["sent"] == 0
    assert empty["channel"]["reason"] == "notify.disabled"
    assert empty["state_file"].endswith("notify_state.json") and empty["limit"] > 0

    # a real delivery through the channel's own code path, transport replaced
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    KillSwitch(kill_switch_path(s)).trigger("drawdown limit breached", by="risk_gate")
    assert notify_mod.RiskWatch(s, session=Sends()).notify()[0]["sent"] is True

    _, payload = get(base, "/api/alerts")
    assert payload["sent"] == 1 and payload["channel"]["ready"] is True
    entry = payload["items"][0]
    assert entry["kind"] == "halt" and entry["automatic"] is True
    assert entry["at"] and entry["headline"].startswith("selftrade HALT (automatic)")
    # reading it twice changes nothing: this panel never sends and never writes
    assert get(base, "/api/alerts")[1]["items"] == payload["items"]


def test_the_alerts_card_renders_in_the_page(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")

    assert "Alerts" in html
    for anchor in ('id="al-status"', 'id="al-meta"', 'id="al-body"',
                   'onclick="refreshAlerts()"'):
        assert anchor in html, anchor
    assert "async function refreshAlerts()" in html
    assert "await refreshAlerts();" in html          # read with the panels
    assert "HALT (automatic)" in html                # the row that matters at 3am
    assert html.index("await refreshRisk();") < html.index("await refreshAlerts();")


def test_the_risk_endpoint_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/risk") == 401


def test_the_alerts_endpoint_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/alerts") == 401


def test_the_risk_panel_renders_in_the_page(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert "Risk limits" in html
    for anchor in ('id="risk-status"', 'id="risk-headroom"', 'id="risk-body"',
                   'id="risk-meta"', 'onclick="refreshRisk()"'):
        assert anchor in html, anchor
    assert "async function refreshRisk()" in html
    # read with the panels, right after the stop it belongs to
    assert "await refreshRisk();" in html
    assert html.index("await refreshSwitch();") < html.index("await refreshRisk();")
    # and the panel says what the alert channel still owes, next to whether it is on
    assert "alertChip(r && r.alerts, r && r.pending)" in html
    assert "waiting to be sent" in html


# ------------------------------------------- the lab, the logs, the settings

def _seed_run_with_report(mem, tmp_path, *, report=None, summary=None):
    run_id = mem.start_run("lab test", {})
    genome_a = {"family": "rsi_reversion", "params": {"rsi_period": 14}, "risk": {"stop_loss_pct": -0.05}}
    genome_b = {"family": "rsi_reversion", "params": {"rsi_period": 21}, "risk": {"stop_loss_pct": -0.05}}
    mem.add_candidate(run_id, 0, genome_a, "seed", [{"metric": 0.1}], 0.1, True, "")
    mem.add_candidate(run_id, 1, genome_b, "llm", [], 0.4, True, "")
    path = tmp_path / "report.json"
    if report is not None:
        path.write_text(json.dumps(report), encoding="utf-8")
        payload = dict(summary or {})
        payload["report_json"] = str(path)
        mem.finish_run(run_id, payload, "ok")
    return run_id, genome_a, genome_b


def test_lab_reports_provenance_and_the_recorded_version_history(ctx):
    base, s, mem = ctx
    run_id = mem.start_run(
        "versioned",
        {},
        repro={
            "repro_id": "deadbeefdeadbeef",
            "reproducible": False,
            "commit": "f" * 40,
            "commit_short": "fffffff",
            "dataset": {"sha256": "e" * 64, "rows": 18000},
        },
    )
    parent = {"family": "trend_momentum", "params": {"ema_fast": 10, "ema_slow": 50},
              "risk": {"stop_loss_pct": -0.05}}
    child = {"family": "trend_momentum", "params": {"ema_fast": 12, "ema_slow": 50},
             "risk": {"stop_loss_pct": -0.05}}
    vid = mem.add_strategy_version(
        run_id, genome=parent, generation=0, version=1, origin="seed",
        fitness=0.2, passed=True, outcome="kept",
    )
    mem.add_strategy_version(
        run_id, genome=child, parent=parent, generation=1, version=2, origin="deterministic",
        rationale="faster entry", reason="", fitness=0.6, parent_fitness=0.2,
        passed=True, outcome="champion",
    )
    code, lab = get(base, f"/api/lab?run_id={run_id}")

    assert code == 200 and lab["ok"] is True
    # provenance: the id, the commit and the dataset come from the run row itself
    rp = lab["repro"]
    assert rp["repro_id"] == "deadbeefdeadbeef" and rp["reproducible"] is False
    assert rp["commit"] == "f" * 40 and rp["commit_short"] == "fffffff"
    assert rp["dataset"] == "e" * 64 and rp["dataset_short"] == "e" * 12
    assert rp["dataset_rows"] == 18000 and rp["versions_recorded"] == 2
    # the run list carries the same provenance, so a run can be picked by id
    listed = next(r for r in lab["runs"] if r["id"] == run_id)
    assert listed["repro_id"] == "deadbeefdeadbeef" and listed["commit"] == "fffffff"
    # the version history is the recorded lineage, oldest first, with the stored diff
    versions = lab["versions"]
    assert lab["versions_truncated"] is False
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["has_parent"] is False and versions[0]["diff"] == []
    assert versions[1]["has_parent"] is True and versions[1]["changed"] == 1
    assert versions[1]["diff"][0]["key"] == "ema_fast"
    assert versions[1]["diff"][0]["from"] == 10 and versions[1]["diff"][0]["to"] == 12
    assert versions[1]["delta_fitness"] == pytest.approx(0.4)
    assert versions[1]["rationale"] == "faster entry"
    assert versions[1]["outcome"] == "champion"
    assert versions[0]["id"] == vid and versions[0]["run_id"] == run_id
    # both genomes travel with the row, so the page can show the before and the after
    assert versions[1]["parent_genome"]["params"]["ema_fast"] == 10
    assert versions[1]["genome"]["params"]["ema_fast"] == 12
    # and a flat, searchable rendering of the diff, so a parameter is findable
    assert "ema_fast" in versions[1]["diff_keys"]
    assert "12" in versions[1]["diff_keys"]
    assert versions[0]["diff_keys"] == ""


def test_lab_says_so_when_a_run_has_no_provenance_or_versions(ctx):
    base, _, _ = ctx
    code, lab = get(base, "/api/lab")
    assert code == 200
    # the ctx run was recorded before provenance existed: N/D, not an invented commit
    assert lab["repro"]["repro_id"] is None
    assert lab["repro"]["commit"] is None and lab["repro"]["reproducible"] is None
    assert lab["versions"] == [] and lab["repro"]["versions_recorded"] == 0


def test_lab_endpoint_joins_the_run_with_its_report(ctx, tmp_path):
    base, s, mem = ctx
    genome_a = {"family": "rsi_reversion", "params": {"rsi_period": 14}, "risk": {"stop_loss_pct": -0.05}}
    genome_b = {"family": "rsi_reversion", "params": {"rsi_period": 21}, "risk": {"stop_loss_pct": -0.05}}
    report = {
        "run_id": None, "metric": "sharpe", "llm_available": True,
        "proposals": {"llm": 1, "deterministic": 0, "seed": 1},
        "champion": genome_b, "champion_fitness": 0.4,
        "holdout": {"metric": "sharpe", "metric_value": 0.2, "fold_mean": 0.3,
                    "fold_values": [0.1, 0.3], "n_trades": 9},
        "critic": {"approved": False, "text": "refused", "reasons": ["too much drawdown"],
                   "warnings": [], "checks": []},
        "autopsy": {"summary": "72 losers", "n_losers": 72, "worst": [], "modes": []},
        "generations": [
            {"generation": 0, "best_fitness": 0.1, "kept": 1, "lesson": "l0", "candidates": [
                {"family": "rsi_reversion", "genome": genome_a, "origin": "seed", "fitness": 0.1,
                 "passed": True, "reason": "", "rationale": "seed genome", "n_trades": 8,
                 "max_drawdown": 0.1, "metric_mean": 0.1, "metric_std": 0.0, "complexity": 2}]},
            {"generation": 1, "best_fitness": 0.4, "kept": 1, "lesson": "l1", "candidates": [
                {"family": "rsi_reversion", "genome": genome_b, "origin": "llm", "fitness": 0.4,
                 "passed": True, "reason": "", "rationale": "raised rsi_period", "n_trades": 10,
                 "max_drawdown": 0.08, "metric_mean": 0.4, "metric_std": 0.1, "complexity": 3}]},
        ],
    }
    run_id, _, _ = _seed_run_with_report(
        mem, tmp_path, report={**report, "run_id": 0},
        summary={"generations": 2, "candidates": 2, "passed": 2, "champion": "rsi_reversion",
                 "champion_fitness": 0.4, "metric": "sharpe", "llm_available": True},
    )
    code, lab = get(base, "/api/lab")

    assert code == 200 and lab["ok"] is True
    assert lab["selected"] == run_id and lab["report_available"] is True
    assert lab["metric"] == "sharpe" and lab["llm_available"] is True
    # the whole cycle is present, in order, each stage carrying evidence
    assert [s["key"] for s in lab["cycle"]] == [
        "collect", "hypothesize", "backtest", "validate", "paper", "learn"
    ]
    assert all("status" in s and s["detail"] for s in lab["cycle"])
    # experiments come from the report: hypothesis and the diff against the family baseline
    exps = lab["experiments"]
    assert [e["origin"] for e in exps] == ["seed", "llm"]
    assert exps[1]["rationale"] == "raised rsi_period"
    assert exps[1]["delta"] == pytest.approx(0.3)
    assert any(p["key"] == "rsi_period" for p in exps[1]["params_changed"])
    assert exps[1]["is_champion"] is True and exps[0]["is_champion"] is False
    # the critic and the autopsy are the report's own, not a re-derivation
    assert lab["critic"]["approved"] is False
    assert "too much drawdown" in lab["critic"]["reasons"]
    assert lab["autopsy"]["n_losers"] == 72
    assert lab["holdout"]["metric_value"] == pytest.approx(0.2)


def test_lab_endpoint_picks_the_latest_run_and_survives_a_missing_report(ctx):
    base, s, mem = ctx
    # ctx already recorded a run with no summary; a second one is the newest
    mem.add_lesson(1, "no report here")
    code, lab = get(base, "/api/lab")
    assert code == 200 and lab["ok"] is True
    assert lab["report_available"] is False
    assert lab["experiments"] == []          # nothing recorded, nothing invented
    assert len(lab["cycle"]) == 6             # the cycle is still described
    # an unreadable run id must not blow up: it falls back to the latest
    assert get(base, "/api/lab?run_id=not-a-number")[0] == 200
    assert get(base, "/api/lab?run_id=999999")[1]["selected"] == 999999


def test_logs_endpoint_returns_events_calls_and_the_loop_tail(ctx):
    base, s, mem = ctx
    mem.add_risk_event(None, "order_blocked", "the order was too big")
    mem.add_api_call(None, provider="binance-testnet", method="GET", path="/v3/time",
                     status=200, ok=True, latency_ms=1.5)
    log = Path(s.session_file).parent / "loop.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("line one\nline two\nline three\n", encoding="utf-8")
    code, lg = get(base, "/api/logs")

    assert code == 200 and lg["ok"] is True
    assert lg["risk_events"][0]["kind"] == "order_blocked"
    assert lg["api_calls"][0]["provider"] == "binance-testnet"
    assert lg["loop_log"]["exists"] is True
    assert lg["loop_log"]["tail"] == ["line one", "line two", "line three"]


def test_settings_endpoint_lists_variable_names_never_values(ctx, monkeypatch):
    base, s, _ = ctx
    monkeypatch.setenv("SELFTD_BINANCE_API_KEY", "SUPER-SECRET-VALUE")
    monkeypatch.setenv(s.dashboard.password_env, "hunter2-secret")
    code, cfg = get(base, "/api/settings")

    assert code == 200 and cfg["ok"] is True
    blob = json.dumps(cfg)
    assert "SUPER-SECRET-VALUE" not in blob and "hunter2-secret" not in blob
    assert cfg["binance"]["api key env"] == "SELFTD_BINANCE_API_KEY"
    assert cfg["dashboard"]["password env"] == s.dashboard.password_env
    # the sealed caps are reported with their digest, and they are the real ones
    assert cfg["sealed"]["limits"]["MAX_DRAWDOWN_PCT"] == 15.0
    assert cfg["sealed"]["pinned"] is True
    # the capital account's policy is a section like the others, not a hidden key
    assert cfg["funding"]["max deposit"] == s.funding.max_deposit
    assert cfg["funding"]["currency"] == s.funding.currency
    assert cfg["funding"]["allow withdrawal"] is True
    # and every section the page looks up is present under the name it looks up, so no
    # card can silently render `N/D` for a config that is right there in the payload
    for name in ("paths", "data", "backtest", "evaluation", "evolution", "paper",
                 "funding", "risk_gate", "dashboard", "llm", "notify", "binance",
                 "mt5", "rea"):
        assert isinstance(cfg.get(name), dict) and cfg[name], name


def test_the_new_endpoints_are_protected(auth_ctx):
    base, _ = auth_ctx
    for path in ("/api/lab", "/api/logs", "/api/settings"):
        assert get_http_error(base, path) == 401, path


def test_the_new_views_render_in_the_page(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    for view in ("overview", "trading", "lab", "agents", "backtest", "memory",
                 "logs", "settings"):
        assert 'data-view="' + view + '"' in html, view
    assert "Improvement cycle" in html
    assert "Agent Center" in html
    assert "Hypothesize" in html and "Paper trade" in html  # the cycle stages
    assert "setView(" in html and "renderActive()" in html
    assert "N/D" in html  # the honest "not available" marker exists
    # and there is deliberately no made-up AI accuracy figure
    assert "AI accuracy" not in html


def test_each_view_carries_its_own_controls(ctx):
    """Every menu entry must have the controls of its own subject, not just a title."""
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    # Overview: a manual refresh and the market selection it shares with Trading.
    assert 'id="ov-updated"' in html and "refreshOverview(true)" in html
    assert 'id="ov-symbol"' in html and 'id="ov-interval"' in html
    # each toolbar's apply button names its own fields, so neither overrides the other
    assert "applyMarket('ov')" in html and "applyMarket('mk')" in html
    # Agent Center: the same loop controls as the header, plus a refresh.
    for ctl in ("ag-start", "ag-pause", "ag-resume", "ag-stop"):
        assert 'id="' + ctl + '"' in html, ctl
    assert "renderAgents()" in html
    # Backtesting: choose what to evaluate and how, then run it.
    assert 'id="bt-genome"' in html and 'id="bt-window"' in html
    assert 'id="bt-run"' in html and "runBacktest()" in html and "window=folds" in html
    # AI Lab: re-run a recorded version against its stored fitness.
    assert "rerunVersion(" in html and "verrun-" in html
    # Memory: scope lessons by run, expand a hall entry.
    assert "setRunFilter(" in html and "backtestHall(" in html
    # System Logs: a row budget, filters, copy and follow.
    assert 'id="log-limit"' in html and "setLogLimit(" in html
    assert 'id="log-follow-btn"' in html and "toggleFollow()" in html and "copyLoopLog()" in html
    assert 'id="log-ev-bar"' in html and 'id="log-call-bar"' in html
    # Settings: the seal is verified, not merely echoed.
    assert "renderSeal(" in html and "sealed.verification" in html
    assert "Seal verification" in html


# ------------------------------------------------- requested backtests


def _bt_settings(tmp_path):
    """A dashboard pointed at synthetic bars: no file, no network, deterministic."""
    s = _settings(tmp_path)
    s.dashboard.auth = False
    s.data.source = "synthetic"
    s.data.synthetic_rows = 2500
    return s


def test_backtest_endpoint_runs_a_real_evaluation_and_writes_nothing(tmp_path):
    import numpy as np

    from selftrade.dashboard import BacktestRunner
    from selftrade.genome import sample_genome

    s = _bt_settings(tmp_path)
    mem = Memory(s.memory.db)
    before = mem.stats()
    genome = sample_genome("rsi_reversion", rng=np.random.default_rng(7))
    httpd, base = _serve(s)
    httpd.RequestHandlerClass.backtest = BacktestRunner(s)
    try:
        q = urllib.parse.quote(json.dumps(genome))
        code, res = get(base, "/api/backtest?window=folds&genome=" + q)
    finally:
        httpd.shutdown()

    assert code == 200 and res["ok"] is True
    assert res["window"]["name"] == "folds" and res["window"]["in_sample"] is False
    assert res["window"]["bars"] > 0 and res["metric"] == "sharpe"
    assert isinstance(res["fitness"], float) and res["n_trades"] >= 0
    assert res["costs"]["round_trip_bps"] == 30.0  # 10 fee + 5 slippage, per side
    assert res["provenance"]["dataset"]["sha256"]          # the dataset is fingerprinted
    assert len(res["equity"]) >= 1 and res["equity_step"] >= 1
    assert res["runtime_ms"] > 0 and "comparison" not in res  # nothing recorded to compare
    # a GET that computes is still a GET: no row, no event, no call was written
    assert mem.stats() == before


def test_backtest_endpoint_reproduces_a_recorded_version(tmp_path):
    import numpy as np

    from selftrade.dashboard import BacktestRunner
    from selftrade.genome import sample_genome

    s = _bt_settings(tmp_path)
    mem = Memory(s.memory.db)
    genome = sample_genome("rsi_reversion", rng=np.random.default_rng(11))
    runner = BacktestRunner(s)
    code, first = runner.run(genome=genome, window="folds")
    assert code == 200

    # Record the version the way a run would: its fitness and the run's dataset digest.
    prov = first["provenance"]
    run_id = mem.start_run("dash backtest", {},
                           repro={"commit": "deadbeef", "commit_short": "deadbeef",
                                  "dataset": prov["dataset"], "repro_id": prov["repro_id"],
                                  "reproducible": True})
    vid = mem.add_strategy_version(run_id, genome=genome, generation=0, version=1,
                                   origin="seed", fitness=first["fitness"], passed=True,
                                   outcome="champion")

    httpd, base = _serve(s)
    httpd.RequestHandlerClass.backtest = BacktestRunner(s)
    try:
        code, res = get(base, f"/api/backtest?version_id={vid}&window=folds")
    finally:
        httpd.shutdown()

    assert code == 200 and res["ok"] is True
    cmp = res["comparison"]
    assert cmp["same_genome"] is True and cmp["matches"] is True
    assert abs(cmp["delta"]) < 1e-9
    assert cmp["dataset_matches"] is True
    assert cmp["stored_repro_id"] == prov["repro_id"] == cmp["current_repro_id"]
    assert "reproduces" in cmp["note"]


def test_backtest_endpoint_refuses_what_it_cannot_evaluate(tmp_path):
    from selftrade.dashboard import BacktestRunner

    s = _bt_settings(tmp_path)
    Memory(s.memory.db)
    httpd, base = _serve(s)
    httpd.RequestHandlerClass.backtest = BacktestRunner(s)
    try:
        # nothing named
        assert get_http_error(base, "/api/backtest") == 400
        # an unknown window is named back to the caller
        assert get_http_error(base, "/api/backtest?window=weekly&genome=%7B%7D") == 400
        # a version id that was never recorded
        assert get_http_error(base, "/api/backtest?version_id=424242&window=folds") == 404
        # a genome that is not one
        assert get_http_error(base, '/api/backtest?genome=%22nope%22') == 400
    finally:
        httpd.shutdown()


def test_the_seal_is_verified_read_only(ctx):
    """The Settings page's own check: three comparisons, and a GET that never writes."""
    base, s, _ = ctx
    from selftrade.risk_rules import seal_path

    seal = seal_path(s)
    assert not seal.exists()          # nothing pre-created by the test
    code, cfg = get(base, "/api/settings")

    assert code == 200
    v = cfg["sealed"]["verification"]
    assert v["ok"] is True
    assert v["pinned"] is True and v["digest"] == v["pin"]
    assert v["levers_closed"] is True and v["levers_error"] is None
    assert v["matches_disk"] is None      # no seal file yet is "unknown", not "mismatch"
    assert v["on_disk"]["exists"] is False
    assert v["file"] and v["path"]
    # reading the verification must not create the seal file
    assert not seal.exists()


def test_the_backtest_endpoint_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/backtest?genome=%7B%7D") == 401


# ---------------------------------------------------------- the capital card


def _paper_session(mem, settings):
    """A session the capital card can fund (no genome needed for the funding rules)."""
    from selftrade.genome import sample_genome

    import numpy as np

    return mem.create_paper_session(
        name="dash capital test",
        genome=sample_genome("rsi_reversion", rng=np.random.default_rng(5)),
        window="custom",
        total_bars=100,
        start_time="2025-01-01T00:00:00+00:00",
        end_time="2025-01-10T00:00:00+00:00",
        initial_cash=settings.backtest.initial_cash,
        settings={},
    )


def test_the_capital_endpoint_reports_the_simulated_account(ctx):
    base, s, _ = ctx
    code, cap = get(base, "/api/capital")

    assert code == 200 and cap["ok"] is True
    assert cap["simulated"] is True                 # never shown as a real account
    assert cap["balance"] == 0.0 and cap["entries"] == 0 and cap["ledger"] == []
    assert cap["currency"] == s.funding.currency
    assert cap["policy"]["max_deposit"] == s.funding.max_deposit
    assert cap["session"] is None                   # nothing is holding capital
    assert "no exchange" in cap["note"].lower()
    assert cap["checked_at"]


def test_crediting_from_the_dashboard_needs_a_confirmation_and_a_reason(ctx):
    base, _, mem = ctx
    # no confirmation: refused, and the ledger stays empty
    code, obj, _ = post(base, "/api/capital", {"action": "deposit", "amount": 500,
                                               "reason": "seed"})
    assert code == 400 and obj["confirm_required"] is True
    assert mem.ledger_entries(10) == []

    # confirmed but with no reason: still refused - an unauditable credit is not a credit
    code, obj, _ = post(base, "/api/capital", {"action": "deposit", "amount": 500,
                                               "confirm": True})
    assert code == 400 and "reason" in obj["error"]
    assert mem.ledger_entries(10) == []

    # an unknown action is named back
    code, obj, _ = post(base, "/api/capital", {"action": "mint", "amount": 1,
                                               "reason": "why not", "confirm": True})
    assert code == 400 and "invalid action" in obj["error"]


def test_a_credit_from_the_dashboard_is_ledgered_and_visible(ctx):
    base, _, mem = ctx
    code, obj, _ = post(base, "/api/capital", {"action": "deposit", "amount": 25_000,
                                               "reason": "seed capital", "confirm": True})
    assert code == 200 and obj["ok"] is True
    assert obj["entry"]["kind"] == "deposit" and obj["entry"]["amount"] == 25_000
    assert obj["entry"]["source"] == "dashboard"   # the audit line says where it came from
    assert obj["account"]["balance"] == 25_000

    _, cap = get(base, "/api/capital")
    assert cap["balance"] == 25_000 and cap["credited"] == 25_000
    assert cap["ledger"][0]["reason"] == "seed capital"

    # the policy still refuses a credit past the cap
    code, obj, _ = post(base, "/api/capital", {
        "action": "deposit", "amount": 999_999_999, "reason": "typo", "confirm": True})
    assert code == 400 and "max_deposit" in obj["error"]
    assert mem.ledger_totals()["credited"] == 25_000


def test_the_dashboard_funds_a_session_from_the_ledger(ctx):
    base, s, mem = ctx
    sid = _paper_session(mem, s)
    post(base, "/api/capital", {"action": "deposit", "amount": 8_000,
                                "reason": "seed", "confirm": True})

    code, obj, _ = post(base, "/api/capital", {"action": "allocate", "amount": 8_000,
                                                "reason": "fund the session",
                                                "session_id": sid, "confirm": True})
    assert code == 200 and obj["session_id"] == sid and obj["capital"] == 8_000
    assert mem.get_paper_session(sid)["initial_cash"] == 8_000

    _, cap = get(base, "/api/capital")
    assert cap["session"]["session_id"] == sid
    assert cap["session"]["funded"] == 8_000 and cap["session"]["started"] is False
    assert cap["balance"] == 0.0 and cap["allocated"] == 8_000

    # the paper card names where the capital came from
    _, paper = get(base, "/api/paper")
    assert paper["session"]["capital_source"] == "ledger"
    assert paper["session"]["funded_capital"] == 8_000

    # and the release puts it back, because the session has not traded
    code, obj, _ = post(base, "/api/capital", {"action": "release", "reason": "take it back",
                                                "session_id": sid, "confirm": True})
    assert code == 200 and obj["capital"] == 0.0
    assert mem.get_paper_session(sid)["initial_cash"] == 0.0
    _, cap = get(base, "/api/capital")
    assert cap["balance"] == 8_000 and cap["allocated"] == 0.0


def test_the_capital_endpoint_is_protected(auth_ctx):
    base, _ = auth_ctx
    assert get_http_error(base, "/api/capital") == 401
    code, obj, _ = post(base, "/api/capital", {"action": "deposit", "amount": 1,
                                              "reason": "seed", "confirm": True})
    assert code == 401


def test_every_settings_card_the_page_asks_for_has_a_section(ctx):
    """The page calls `sec(name, s.name)`; a payload key that does not match renders N/D."""
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    code, cfg = get(base, "/api/settings")
    assert code == 200

    import re as _re

    asked = set(_re.findall(r"sec\('([a-z_]+)', s\.", html))
    assert asked, "the page's own section list must be findable in the markup"
    missing = sorted(name for name in asked if name not in cfg)
    assert not missing, f"these cards would render N/D: {missing}"
    # the two sections this feature added are among them
    assert {"funding", "risk_gate"} <= asked


def test_the_capital_card_renders_its_own_controls(ctx):
    base, _, _ = ctx
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    assert 'id="cap-card"' in html and "Capital account" in html
    for el in ("cap-tiles", "cap-amount", "cap-reason", "cap-deposit", "cap-withdraw",
               "cap-allocate", "cap-release", "cap-confirm", "cap-ledger"):
        assert 'id="' + el + '"' in html, el
    for fn in ("refreshCapital(", "capRender(", "capAsk(", "capGo(", "capCancel("):
        assert fn in html, fn
    # the card states the two things that matter about this account
    assert "simulated" in html and "credit itself" in html
