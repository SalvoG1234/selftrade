"""The admin dashboard, over real HTTP.

The trading dashboard is tested the same way (``tests/test_dashboard.py``):
a real server on an ephemeral loopback port and a real client, because the
things worth checking here - the login, the CSRF token, the rate limiter, the
kill switch - only exist at the transport layer.  A unit test that calls
``_api_post`` directly would pass with the authentication removed.

Two more properties this file exists to protect:

* **a loopback bind may drop the login, a public bind may not**: the refusal is
  a startup error, not a warning;
* **nothing is rendered that the database did not return**: with an empty store
  every page reports zeros and "unavailable", never a plausible-looking number.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from urllib.parse import urlencode, urlparse

import pytest

from socialmanager.dashboard import MAX_LOGIN_ATTEMPTS, make_server
from socialmanager.db import SocialDB
from socialmanager.orchestrator import Orchestrator
from tests.fake_social import make_settings, seed_item_with_variant, seed_release

PASSWORD_ENV = "SOCIAL_TEST_DASHBOARD_PASSWORD"
PASSWORD = "s3cret-pw-2026"


# ------------------------------------------------------------------ the client
def call(base, method, path, *, json_body=None, form=None, headers=None, cookie=None):
    """One request, no redirect following, decoded by content type."""
    parsed = urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
    hdrs = dict(headers or {})
    payload = None
    if form is not None:
        payload = urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if payload is not None:
        hdrs["Content-Length"] = str(len(payload))
    if cookie:
        hdrs["Cookie"] = cookie
    try:
        conn.request(method, path, payload, hdrs)
        response = conn.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type") or ""
        body: object = (
            json.loads(raw.decode("utf-8"))
            if content_type.startswith("application/json") and raw
            else raw.decode("utf-8", "replace")
        )
        return response.status, body, {k.lower(): v for k, v in response.getheaders()}
    finally:
        conn.close()


def get(base, path, **kwargs):
    return call(base, "GET", path, **kwargs)


def post(base, path, **kwargs):
    return call(base, "POST", path, **kwargs)


def sidebar_pages(html: str) -> list[str]:
    """The page ids the rail routes to, read out of the served shell.

    Read from the document rather than from a constant in this file on purpose: a page
    added to the navigation is exactly the change that must make the coverage test fail,
    and a hand-written list here would quietly stop matching the rail.
    """
    block = re.search(r"const PAGES = \[(.*?)\];", html, re.S)
    assert block, "the served shell must define the PAGES rail"
    return re.findall(r'\["([A-Za-z0-9_-]+)"\s*,\s*"', block.group(1))


def csrf_of(html: str) -> str:
    match = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    assert match, "the served page must carry the session's CSRF token"
    return match.group(1)


# ----------------------------------------------------------------- fixtures
def _serve(settings, *, no_auth: bool):
    database = SocialDB.open(settings.database.url)
    ctx = Orchestrator(settings, database)
    httpd = make_server(ctx, "127.0.0.1", 0, no_auth=no_auth)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return ctx, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _stop(ctx, httpd):
    httpd.shutdown()
    httpd.server_close()
    ctx.close()


@pytest.fixture()
def panel(tmp_path):
    """A loopback panel with the login disabled: the read paths under test."""
    settings = make_settings(tmp_path)
    settings.llm.enabled = False
    ctx, httpd, base = _serve(settings, no_auth=True)
    try:
        yield ctx, base
    finally:
        _stop(ctx, httpd)


@pytest.fixture()
def secured(tmp_path, monkeypatch):
    """The same panel with authentication on, as a real deployment runs it."""
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    settings = make_settings(tmp_path)
    settings.llm.enabled = False
    settings.dashboard.password_env = PASSWORD_ENV
    ctx, httpd, base = _serve(settings, no_auth=False)
    try:
        yield ctx, base
    finally:
        _stop(ctx, httpd)


def sign_in(base, password=PASSWORD):
    """Log in like the browser does, and return ``(cookie, csrf)``."""
    status, _, headers = post(base, "/login", form={"password": password})
    assert status == 302, f"login should redirect, got {status}"
    cookie = headers["set-cookie"].split(";", 1)[0]
    status, html, _ = get(base, "/", cookie=cookie)
    assert status == 200 and isinstance(html, str)
    return cookie, csrf_of(html)


# ------------------------------------------------------------- the page itself
def test_the_page_carries_every_view_the_brief_asks_for(panel):
    _, base = panel
    status, html, headers = get(base, "/")
    assert status == 200
    for label in ("Overview", "Guida operativa", "Editorial Calendar", "Content Studio",
                  "Approval Queue", "Published Posts", "Community Inbox",
                  "Feedback Intelligence", "Analytics", "Social Accounts", "Agent Activity",
                  "Logs & Audit", "Settings"):
        assert label in html, f"the navigation is missing {label}"
    # The same headers the trading dashboard sets: this page executes inline
    # script, so everything else is denied and the response is never cached.
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "no-store" in headers["cache-control"]


def test_the_page_reads_only_the_api_it_calls(panel):
    """The shell has no inline data: every figure comes from a JSON endpoint."""
    _, base = panel
    _, html, _ = get(base, "/")
    assert "__DASHBOARD_BOOTSTRAP__" not in html      # the placeholder was replaced
    assert "api(\"/api/status\")" in html


def test_the_page_carries_the_daily_use_affordances(panel):
    """The controls an operator uses every day are part of the served page.

    This is the counterpart of ``test_the_page_carries_every_view_the_brief_asks_for``:
    that one lists the thirteen pages, this one the affordances that make them usable -
    batch approvals with filters, a command palette, keyboard shortcuts, a freshness
    indicator and a confirmation gate before a real publish.
    """
    _, base = panel
    _, html, _ = get(base, "/")
    for marker in ("ap-state", "ap-risk", "ap-note", "apBulk", "apSyncControls",
                   "palette-input", "openPalette", "Ctrl / Cmd + K", "showHelp",
                   "confirmDialog", "confirmPublish", "freshness", "attention",
                   "editingNow"):
        assert marker in html, f"the daily-use controls are missing {marker}"


def test_a_silent_poll_asks_the_guard_before_it_redraws(panel):
    """The 30-second poll must not overwrite an unsaved edit with the stored copy.

    A guard that exists but is never consulted would still lose the work, so this
    asserts the call site: the silent path returns before ``render()``.
    """
    _, base = panel
    _, html, _ = get(base, "/")
    assert "if(silent && editingNow()) return;" in html
    assert "setInterval(() => { if(!document.hidden) refresh(true); }, 30000)" in html


# ------------------------------------------------------------------ the login
def test_the_api_answers_401_without_a_session(secured):
    _, base = secured
    status, body, _ = get(base, "/api/status")
    assert status == 401
    assert body["error"] == "authentication required"


def test_the_login_page_does_not_ask_for_a_password_it_already_has(secured):
    """Telling an operator who set the variable to set the variable is a dead end."""
    _, base = secured
    status, html, _ = get(base, "/login")
    assert status == 200
    assert f"Imposta {PASSWORD_ENV} nell'ambiente e riavvia." not in html


def test_the_login_page_says_what_is_missing_when_nothing_is_configured(tmp_path, monkeypatch):
    """The other direction: with no password anywhere, the notice is the instruction."""
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    settings = make_settings(tmp_path)
    settings.dashboard.password_env = PASSWORD_ENV
    ctx, httpd, base = _serve(settings, no_auth=False)
    try:
        status, html, _ = get(base, "/login")
        assert status == 200
        assert f"Imposta {PASSWORD_ENV} nell'ambiente e riavvia." in html
    finally:
        _stop(ctx, httpd)


def test_a_password_less_deployment_refuses_every_login(tmp_path, monkeypatch):
    """No stored hash, no environment value: login is not "open", it is closed."""
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    settings = make_settings(tmp_path)
    settings.dashboard.password_env = PASSWORD_ENV
    ctx, httpd, base = _serve(settings, no_auth=False)
    try:
        status, body, _ = post(base, "/login", form={"password": "0000"})
        assert status == 503
        assert "no password is configured" in body["error"]
    finally:
        _stop(ctx, httpd)


def test_the_right_password_returns_an_httponly_session_cookie(secured):
    _, base = secured
    status, _, headers = post(base, "/login", form={"password": PASSWORD})
    assert status == 302 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith("stsm_session=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_a_wrong_password_is_401_and_does_not_set_a_cookie(secured):
    _, base = secured
    status, body, headers = post(base, "/login", form={"password": "not-it"})
    assert status == 401
    assert "Password errata" in body
    assert "set-cookie" not in headers


def test_repeated_wrong_passwords_are_rate_limited(secured):
    _, base = secured
    for _ in range(MAX_LOGIN_ATTEMPTS):
        post(base, "/login", form={"password": "nope"})
    status, body, headers = post(base, "/login", form={"password": PASSWORD})
    assert status == 429
    assert "too many attempts" in body["error"]
    assert headers["retry-after"] == "300"


# ------------------------------------------------------------- CSRF / origin
def test_a_mutation_without_the_csrf_token_is_refused(secured):
    _, base = secured
    cookie, csrf = sign_in(base)
    status, body, _ = post(base, "/api/actions/killswitch", json_body={"engaged": True},
                           cookie=cookie)
    assert status == 403 and "CSRF" in body["error"]
    status, body, _ = post(base, "/api/actions/killswitch", json_body={"engaged": True},
                           cookie=cookie, headers={"X-CSRF-Token": "wrong"})
    assert status == 403 and "CSRF" in body["error"]
    status, body, _ = post(base, "/api/actions/killswitch", json_body={"engaged": True},
                           cookie=cookie, headers={"X-CSRF-Token": csrf})
    assert status == 200 and body["kill_switch"] is True


def test_a_mutation_from_another_origin_is_refused(secured):
    _, base = secured
    cookie, csrf = sign_in(base)
    status, body, _ = post(
        base, "/api/actions/killswitch", json_body={"engaged": True}, cookie=cookie,
        headers={"X-CSRF-Token": csrf, "Origin": "http://evil.example"},
    )
    assert status == 403 and "cross-origin" in body["error"]


def test_a_signed_out_session_cannot_read_the_api(secured):
    _, base = secured
    cookie, _ = sign_in(base)
    status, _, headers = get(base, "/logout", cookie=cookie)
    assert status == 302 and headers["location"] == "/login"
    status, body, _ = get(base, "/api/status", cookie=cookie)
    assert status == 401


# ------------------------------------------------------- the binds that matter
def test_a_public_bind_without_a_password_refuses_to_start(tmp_path):
    settings = make_settings(tmp_path)
    settings.dashboard.host = "0.0.0.0"
    settings.llm.enabled = False
    ctx = Orchestrator(settings, SocialDB.open(settings.database.url))
    try:
        with pytest.raises(SystemExit) as exc:
            make_server(ctx, "0.0.0.0", 0)
        assert "refusing to start" in str(exc.value)
    finally:
        ctx.close()


def test_a_loopback_bind_is_allowed_to_drop_the_login(tmp_path):
    settings = make_settings(tmp_path)
    settings.llm.enabled = False
    ctx = Orchestrator(settings, SocialDB.open(settings.database.url))
    try:
        httpd = make_server(ctx, "127.0.0.1", 0, no_auth=True)
    finally:
        ctx.close()
    httpd.server_close()


# ---------------------------------------------------------------- read paths
READ_PATHS = (
    "/api/status", "/api/health", "/api/overview", "/api/guide", "/api/calendar",
    "/api/content", "/api/approvals", "/api/published", "/api/community", "/api/feedback",
    "/api/analytics", "/api/accounts", "/api/connectors", "/api/agents", "/api/logs",
    "/api/audit", "/api/settings",
)

#: Pages that legitimately have no step of their own in the guide.  ``guide`` is the
#: page doing the guiding; a page that only *displays* the cycle would be a self-step.
GUIDE_EXEMPT = {"guide"}


@pytest.mark.parametrize("path", READ_PATHS)
def test_every_read_endpoint_answers_json_on_an_empty_store(panel, path):
    """A route typo or a missing table must surface here, not in the browser."""
    _, base = panel
    status, body, headers = get(base, path)
    assert status == 200, (path, body)
    assert isinstance(body, dict) and "error" not in body
    assert headers["cache-control"] == "no-store"


def test_an_unknown_endpoint_is_a_404_with_a_reason(panel):
    _, base = panel
    status, body, _ = get(base, "/api/does-not-exist")
    assert status == 404 and "unknown endpoint" in body["error"]


def test_the_overview_reports_what_is_actually_stored(panel):
    ctx, base = panel
    seed_release(ctx.db, tag="v0.3.0")
    item_id, _ = seed_item_with_variant(ctx.db)
    status, body, _ = get(base, "/api/overview")
    assert status == 200
    assert "release:v0.3.0" in [s["external_ref"] for s in body["sources"]]
    assert body["status"]["project"] == "SalvoG1234/selftrade"
    assert body["status"]["stats"]["items"] >= 1
    assert item_id >= 1


def test_an_empty_store_reports_zeros_and_never_a_plausible_number(panel):
    """The brief is explicit: no invented metrics.  An absent value is absent."""
    _, base = panel
    status, body, _ = get(base, "/api/analytics")
    assert status == 200
    assert body["totals"] == {"published": 0, "dry_run": 0, "unknown_needs_inspection": 0,
                              "failed": 0, "feedback": 0}
    assert body["by_platform"] == {}
    assert "unavailable" in body["sources"]["unavailable"]
    blob = json.dumps(body).lower()
    for invented in ("follower", "impressions", "engagement_rate"):
        assert invented not in blob, f"{invented} must not be fabricated"


def test_the_analytics_export_is_really_csv(panel):
    _, base = panel
    status, body, _ = get(base, "/api/analytics/export?fmt=csv")
    assert status == 200 and body["format"] == "csv"
    assert body["content"].splitlines()[0] == (
        "platform,posts,dry_run,unknown,failed,metrics_available,metrics_unavailable"
    )


def test_the_guide_is_a_daily_cycle_whose_lines_are_measurements(panel):
    """The guide is a checklist, and every line carries a real reading.

    An empty store is the interesting case: the guide must say *mai eseguita* and
    *nessuna richiesta in attesa* rather than showing a plausible-looking example it
    does not have.

    It must also guide **every** screen: the rails' own page ids are read out of the
    served document and compared with the pages the steps point at.
    """
    ctx, base = panel
    _, html, _ = get(base, "/")
    rail = set(sidebar_pages(html))
    status, body, _ = get(base, "/api/guide")
    assert status == 200
    steps = body["steps"]
    assert [s["n"] for s in steps] == list(range(1, len(steps) + 1))
    assert len(steps) == 13
    for step in steps:
        assert step["title"] and step["action"] and step["detail"]
        assert step["page"] in rail, f"{step['page']} is not a page in the rail"
        assert step["state"] in {"ok", "todo", "attention", "blocked"}
    missing = sorted(rail - GUIDE_EXEMPT - {s["page"] for s in steps})
    assert not missing, f"these pages of the rail have no step in the guide: {missing}"

    sources = next(s for s in steps if "fonti" in s["title"].lower())
    assert "mai eseguita" in sources["detail"] and sources["state"] == "todo"
    review = next(s for s in steps if "approvazioni" in s["title"].lower())
    assert review["detail"] == "nessuna richiesta in attesa" and review["state"] == "ok"

    blob = json.dumps(body).lower()
    for invented in ("follower", "impressions", "engagement_rate"):
        assert invented not in blob, f"{invented} must not be fabricated"


def test_a_page_added_to_the_rail_without_a_guide_step_is_caught(panel):
    """Prove the coverage guard is not vacuous: inject a new page and watch it fire.

    The guard is ``rail - GUIDE_EXEMPT - referenced``, and ``rail`` comes from parsing the
    served document.  So this adds a page to the real rail - as a future commit would -
    and checks both halves: the parse sees the new page, and the predicate then reports it
    as undocumented.  Without this, the coverage assertion in the daily-cycle test could
    pass for the wrong reason.
    """
    _, base = panel
    _, html, _ = get(base, "/")
    rail = sidebar_pages(html)
    assert "brand_new_page" not in rail

    doctored = html.replace('const PAGES = [',
                            'const PAGES = [["brand_new_page","Brand new","?"],', 1)
    new_rail = sidebar_pages(doctored)
    assert "brand_new_page" in new_rail
    assert len(new_rail) == len(rail) + 1

    status, body, _ = get(base, "/api/guide")
    referenced = {s["page"] for s in body["steps"]}
    missing = sorted(set(new_rail) - GUIDE_EXEMPT - referenced)
    assert missing == ["brand_new_page"], missing


def test_the_guide_counts_what_is_really_queued_and_follows_the_kill_switch(panel):
    ctx, base = panel
    for _ in range(2):
        item_id, _ = seed_item_with_variant(ctx.db, risk="high")
        ctx.approvals().ensure_request(item_id, risk="high", reason="mode A")

    status, body, _ = get(base, "/api/guide")
    review = next(s for s in body["steps"] if "approvazioni" in s["title"].lower())
    assert "2 in attesa" in review["detail"]
    assert "ad alto rischio o sensibili" in review["detail"]
    assert review["state"] == "attention"
    assert body["kill_switch"] is False and body["steps"][0]["state"] == "ok"

    post(base, "/api/actions/killswitch", json_body={"engaged": True})
    status, body, _ = get(base, "/api/guide")
    assert body["kill_switch"] is True
    assert body["steps"][0]["state"] == "blocked"
    assert "ENGAGED" in body["steps"][0]["action"]


def test_the_audit_endpoint_verifies_its_own_chain(panel):
    ctx, base = panel
    seed_release(ctx.db, tag="v0.4.0")
    ctx.db.audit("test.event", actor="pytest", subject="dashboard")
    status, body, _ = get(base, "/api/audit")
    assert status == 200 and body["verify"]["ok"] is True
    assert any(e["action"] == "test.event" for e in body["events"])


# -------------------------------------------------------------- Content Studio
def test_editing_a_draft_saves_it_and_re_runs_the_policy(panel):
    ctx, base = panel
    _, variant_id = seed_item_with_variant(ctx.db)
    edited = "Edited by hand, still sourced: https://github.com/SalvoG1234/selftrade"
    status, body, _ = post(base, f"/api/content/variant/{variant_id}",
                           json_body={"body": edited})
    assert status == 200 and body["ok"] is True
    assert body["policy"]["outcome"] == "pass"
    assert ctx.db.variant(variant_id)["body"] == edited
    assert ctx.db.variant(variant_id)["generator"] == "manual:dashboard"


def test_an_empty_edit_is_refused(panel):
    ctx, base = panel
    _, variant_id = seed_item_with_variant(ctx.db)
    for value in ("", "   ", None):
        status, body, _ = post(base, f"/api/content/variant/{variant_id}",
                               json_body={"body": value})
        assert status == 400, value
    status, body, _ = post(base, "/api/content/variant/99999",
                           json_body={"body": "no such variant"})
    assert status == 404


def test_an_edit_that_promises_profit_is_still_caught_by_the_policy(panel):
    """The manual path is not a bypass: the policy runs on hand-written text too."""
    ctx, base = panel
    _, variant_id = seed_item_with_variant(ctx.db)
    status, body, _ = post(
        base, f"/api/content/variant/{variant_id}",
        json_body={"body": "Guaranteed profit with our risk free strategy, results in the "
                           "repo: https://github.com/SalvoG1234/selftrade"},
    )
    assert status == 200
    assert body["policy"]["outcome"] == "block"
    assert body["policy"]["reasons"]


# --------------------------------------------------------- the format preview
def test_the_preview_reports_the_format_and_the_verdict_for_every_variant(panel):
    """One read path that measures the draft and says what would refuse it."""
    ctx, base = panel
    item_id, variant_id = seed_item_with_variant(ctx.db)
    ctx.db.upsert_variant(item_id, "instagram", "en", "A caption for the feed.",
                          hashtags="#Python")
    status, body, _ = get(base, f"/api/content/{item_id}/previews")
    assert status == 200
    by_platform = {v["platform"]: v for v in body["variants"]}
    assert by_platform["x"]["measured"]["limit"] == 280
    assert by_platform["x"]["measured"]["effective_length"] >= 23   # the link weight
    assert by_platform["instagram"]["measured"]["feed"]["cut_at"] == 125
    # Instagram refuses a post with no image, so that variant is not ready; the
    # item is still approvable because the post on X is.
    assert by_platform["instagram"]["verify"]["ok"] is False
    assert body["ready"] == [variant_id] and body["blocked"] != []
    assert body["approvable"] is True and body["blocked_reason"] == ""


def test_the_preview_of_an_item_that_is_not_there_is_a_404(panel):
    _, base = panel
    status, body, _ = get(base, "/api/content/999/previews")
    assert status == 404 and "not found" in body["error"]


def test_the_approval_is_refused_when_nothing_could_be_published(panel):
    """Approving is a decision about content that can go out.

    A draft whose only variant cannot be sent is not approvable, the refusal names
    the field to fix, the item stays in ``draft``, the request stays ``pending``,
    and the refusal is in the audit rather than being swallowed.
    """
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db, body="   ")
    ctx.approvals().ensure_request(item_id, risk="low", reason="mode A")

    status, body, _ = post(base, "/api/approvals",
                           json_body={"item_id": item_id, "decision": "approved"})
    assert status == 409 and "correggi i campi indicati" in body["error"]
    assert ctx.db.item(item_id)["state"] == "draft"
    assert ctx.db.approval_for("content_item", item_id)["state"] == "pending"

    _, audit, _ = get(base, "/api/audit")
    assert any(e["action"] == "approval.refused" for e in audit["events"])
    assert audit["verify"]["ok"] is True


def test_the_batch_path_the_queue_uses_is_gated_too(panel):
    """The queue approves by ``approval_id`` and the batch button loops over that
    very call, so a gate on ``item_id`` alone would be bypassable from the page."""
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db, body="")
    ctx.approvals().ensure_request(item_id, risk="low", reason="mode A")
    approval_id = ctx.db.approval_for("content_item", item_id)["id"]
    status, body, _ = post(base, "/api/approvals",
                           json_body={"approval_id": approval_id, "decision": "approved"})
    assert status == 409
    assert ctx.db.get("approval", approval_id)["state"] == "pending"


def test_a_warning_informs_and_still_lets_the_approval_through(panel):
    """Only an error refuses.  A short body is under the configured floor, which
    the policy answers with a review - not with a block, and neither does this."""
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db, body="ciao")
    ctx.approvals().ensure_request(item_id, risk="low", reason="mode A")
    _, previews, _ = get(base, f"/api/content/{item_id}/previews")
    assert previews["error_count"] == 0 and previews["warning_count"] >= 1
    status, body, _ = post(base, "/api/approvals",
                           json_body={"item_id": item_id, "decision": "approved"})
    assert status == 200 and ctx.db.item(item_id)["state"] == "approved"


def test_a_live_send_is_refused_for_an_invalid_variant_but_a_simulation_reports_it(panel):
    """The gate stands in front of the real send only; a dry run carries the
    findings back instead of being refused, because seeing them is its purpose."""
    ctx, base = panel
    _, variant_id = seed_item_with_variant(ctx.db, body="   ")

    status, body, _ = post(base, "/api/actions/publish",
                           json_body={"variant_id": variant_id, "live": True})
    assert status == 409 and body["findings"]
    assert ctx.db.published_posts(limit=10) == []

    status, body, _ = post(base, "/api/actions/publish", json_body={"variant_id": variant_id})
    assert status == 200 and body["results"]
    assert body["format"]["ok"] is False


def test_a_simulation_of_a_clean_variant_carries_a_clean_report(panel):
    """The gate must not fire on the ordinary path."""
    ctx, base = panel
    _, variant_id = seed_item_with_variant(ctx.db)
    status, body, _ = post(base, "/api/actions/publish", json_body={"variant_id": variant_id})
    assert status == 200 and body["results"][0]["state"] == "dry_run"
    assert body["format"]["ok"] is True and body["format"]["error_count"] == 0


def test_the_served_studio_carries_the_preview_it_promises(panel):
    """The rail's own tooltip promised an anteprima per piattaforma before there
    was one; this is what keeps the promise wired to a real renderer."""
    _, base = panel
    _, html, _ = get(base, "/")
    assert "function pvVariant" in html
    assert "function pvChecks" in html
    assert "/previews" in html


# ---------------------------------------------------------------- the kill switch
def test_the_kill_switch_stops_a_live_publish_and_is_audited(panel):
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db)

    status, body, _ = post(base, "/api/actions/killswitch", json_body={"engaged": True})
    assert status == 200 and body["kill_switch"] is True

    status, health, _ = get(base, "/api/health")
    assert health["kill_switch"] is True

    status, result, _ = post(base, "/api/actions/publish",
                             json_body={"item_id": item_id, "live": True})
    assert status == 200
    assert [r["state"] for r in result["results"]] == ["blocked"], result
    assert not any(p["state"] == "published" for p in ctx.db.published_posts(limit=20))

    status, audit, _ = get(base, "/api/audit")
    actions = [e["action"] for e in audit["events"]]
    assert "kill_switch.engaged" in actions
    assert audit["verify"]["ok"] is True


def test_releasing_the_kill_switch_lets_the_policy_decide_again(panel):
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db)
    post(base, "/api/actions/killswitch", json_body={"engaged": True})
    status, body, _ = post(base, "/api/actions/killswitch", json_body={"engaged": False})
    assert status == 200 and body["kill_switch"] is False
    status, health, _ = get(base, "/api/health")
    assert health["kill_switch"] is False


def test_a_publish_without_live_is_a_simulation_and_says_so(panel):
    """The default is the safe one: no flag means nothing leaves the machine."""
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db)
    status, body, _ = post(base, "/api/actions/publish", json_body={"item_id": item_id})
    assert status == 200
    assert [r["state"] for r in body["results"]] == ["dry_run"]
    posts = ctx.db.published_posts(limit=20)
    assert [p["state"] for p in posts] == ["dry_run"]
    assert posts[0]["external_id"] in (None, "")
    assert posts[0]["meta"]["simulated"] is True


# ------------------------------------------------------------------ approvals
def test_an_operator_approves_a_drafted_item_and_the_state_follows(panel):
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db)
    ctx.approvals().ensure_request(item_id, risk="low", reason="mode A: a human publishes")

    status, body, _ = post(base, "/api/approvals",
                           json_body={"item_id": item_id, "decision": "approved",
                                      "note": "looks fine"})
    assert status == 200 and body["state"] == "approved"
    assert ctx.db.item(item_id)["state"] == "approved"

    status, listing, _ = get(base, "/api/approvals")
    assert listing["counts"]["approved"] == 1 and listing["counts"]["pending"] == 0


def test_a_decision_must_be_one_of_the_two_names(panel):
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db)
    status, body, _ = post(base, "/api/approvals",
                           json_body={"item_id": item_id, "decision": "maybe"})
    assert status == 400
    status, body, _ = post(base, "/api/approvals", json_body={"decision": "approved"})
    assert status == 400 and "required" in body["error"]


def test_rejecting_an_item_keeps_it_out_of_the_publishing_queue(panel):
    ctx, base = panel
    item_id, _ = seed_item_with_variant(ctx.db)
    ctx.approvals().ensure_request(item_id, risk="low", reason="needs a look")
    status, body, _ = post(base, "/api/approvals",
                           json_body={"item_id": item_id, "decision": "rejected"})
    assert status == 200 and ctx.db.item(item_id)["state"] == "rejected"


def test_a_batch_of_decisions_is_recorded_one_by_one_with_the_note(panel):
    """The batch button in the queue is a loop over the single-decision endpoint.

    That is deliberate: each approval keeps its own audit row and its own note, so a
    bulk approval is still reviewable person-by-person afterwards rather than one
    opaque event.
    """
    ctx, base = panel
    requested = []
    for _ in range(3):
        item_id, _ = seed_item_with_variant(ctx.db)
        ctx.approvals().ensure_request(item_id, risk="low", reason="mode A")
        requested.append(ctx.db.approval_for("content_item", item_id)["id"])

    for approval_id in requested:
        status, body, _ = post(base, "/api/approvals",
                               json_body={"approval_id": approval_id,
                                          "decision": "approved", "note": "batch: reviewed"})
        assert status == 200 and body["ok"] is True

    status, listing, _ = get(base, "/api/approvals")
    assert listing["counts"]["pending"] == 0
    status, audit, _ = get(base, "/api/audit")
    decisions = [e for e in audit["events"] if e["action"] == "approval.approved"]
    assert len(decisions) == 3, decisions
    assert all(e["detail"].get("note") == "batch: reviewed" for e in decisions)
    assert audit["verify"]["ok"] is True


# ------------------------------------------------------------------- settings
def test_an_agent_cannot_widen_its_own_autonomy_from_the_dashboard(panel):
    ctx, base = panel
    status, body, _ = post(base, "/api/settings",
                           json_body={"key": "mode", "value": "advanced"})
    assert status == 200 and body["pending_approval"], body
    assert "approvazione umana" in body["message"]
    assert ctx.db.setting("mode", None) is None          # nothing was applied
    assert str(ctx.settings.mode()) == "draft"

    status, cfg, _ = get(base, "/api/settings")
    assert cfg["autonomy"]["mode"] == "draft"
    assert cfg["kill_switch"] is False
    assert cfg["env_present"]["SOCIAL_X_ACCESS_TOKEN"] is False
    # The effective configuration is served as YAML with names, never values.
    assert "token_env: SOCIAL_X_ACCESS_TOKEN" in cfg["effective_yaml"]


def test_an_ordinary_setting_is_applied_and_recorded(panel):
    ctx, base = panel
    status, body, _ = post(base, "/api/settings",
                           json_body={"key": "editorial_notes", "value": "reviewed"})
    assert status == 200 and body["after"] == "reviewed"
    assert ctx.db.setting("editorial_notes") == "reviewed"
    status, audit, _ = get(base, "/api/audit")
    changed = [e for e in audit["events"] if e["action"] == "settings.changed"]
    assert changed and changed[0]["subject"] == "editorial_notes"


# ----------------------------------------------------------------- the media
def test_the_media_route_serves_generated_assets_only(panel, tmp_path):
    ctx, base = panel
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "card.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (media / "notes.txt").write_text("private notes", encoding="utf-8")

    status, _, headers = get(base, "/media/card.png")
    assert status == 200 and headers["content-type"] == "image/png"

    status, body, _ = get(base, "/media/notes.txt")
    assert status == 415 and "unsupported media type" in body["error"]

    # A traversal is flattened to a bare name inside the media directory, so a
    # file that *does* exist one level up is still never served.
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG-not-really-a-card")
    status, body, _ = get(base, "/media/../secret.png")
    assert status == 404 and "asset not found" in body["error"]
    status, body, _ = get(base, "/media/../../../etc/passwd")
    assert status == 415          # flattened to "passwd": not an allowed type
    status, body, _ = get(base, "/media/missing.png")
    assert status == 404 and "asset not found" in body["error"]


def test_a_broken_store_answers_json_instead_of_dropping_the_socket(tmp_path):
    """A store the backend cannot read must explain itself.

    Every page polls on a timer, so a traceback per poll is not an error report:
    the browser would show a network failure and the loading state forever.
    """
    settings = make_settings(tmp_path)
    settings.llm.enabled = False
    ctx, httpd, base = _serve(settings, no_auth=True)
    try:
        ctx.db.close()               # an unusable store, whatever the cause
        for path in ("/api/overview", "/api/status", "/api/content", "/api/approvals"):
            status, body, _ = get(base, path)
            assert status == 503, (path, status)
            assert body["error"] == "the backend could not answer"
            assert body["detail"]
            assert "doctor" in body["hint"]
        status, body, _ = post(base, "/api/actions/tick", json_body={})
        assert status == 503 and body["error"] == "the backend could not answer"
    finally:
        _stop(ctx, httpd)
