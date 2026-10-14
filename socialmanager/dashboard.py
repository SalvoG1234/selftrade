"""The admin dashboard server: authentication, CSRF, a JSON API, and no surprises.

Built on the standard library, like the trading dashboard this project sits
beside.  The security posture is the part that matters:

* **authentication is required** for any bind that is not loopback, and
  :func:`bind_refusal` refuses to start otherwise - a configuration mistake
  becomes a startup error, not an open admin panel;
* **there is no default password.**  The first password comes from
  ``SOCIAL_DASHBOARD_PASSWORD`` (the *name* is in the config; the value is read
  from the environment) or from a salted PBKDF2 hash written by the dashboard
  itself.  Without one, login is refused and the page says how to set it;
* **every mutation is same-origin and CSRF-checked**: a per-session token in a
  header, plus an ``Origin``/``Host`` comparison;
* **login attempts are rate limited** per address and audited;
* **nothing renders that the API did not return**, and the API only reads the
  database - so the page cannot show a number that does not exist.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__, dashboard_ui, obs, preview, secrets
from .db import to_ts, utcnow
from .models import EditorialState, Platform
from .policy import kill_switch_engaged

COOKIE_NAME = "stsm_session"
SESSION_TTL_S = 86400
PBKDF2_ROUNDS = 240_000
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_S = 300
#: Keys an agent must never change by itself; the dashboard routes them through
#: an approval record instead of writing them.
SENSITIVE_SETTING_KEYS = {"autonomy", "mode", "permissions", "credentials", "kill_switch"}
API_PREFIX = "/api/"


def _json_default(value: Any) -> Any:
    return str(value)


# ---------------------------------------------------------------------- auth
@dataclass
class Session:
    token: str
    csrf: str
    created: float


@dataclass
class Auth:
    """Password storage (salted PBKDF2), sessions and a login rate limiter."""

    settings: Any
    db: Any = None
    sessions: dict[str, Session] = field(default_factory=dict)
    attempts: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------ password
    @property
    def password_file(self) -> Path:
        return Path(self.settings.dashboard.password_file)

    def env_password(self) -> str | None:
        return secrets.read(self.settings.dashboard.password_env)

    def source(self) -> str:
        if self.env_password():
            return "env"
        if self.password_file.exists():
            return "file"
        return "unset"

    def configured(self) -> bool:
        return self.source() != "unset"

    def verify(self, password: str) -> bool:
        env_value = self.env_password()
        if env_value is not None:
            return hmac.compare_digest(str(password), env_value)
        record = self._read_file()
        if not record:
            return False
        salt = bytes.fromhex(str(record.get("salt", "")))
        expected = str(record.get("hash", ""))
        rounds = int(record.get("iterations", PBKDF2_ROUNDS))
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, rounds)
        return hmac.compare_digest(digest.hex(), expected)

    def set_password(self, password: str) -> None:
        if self.env_password() is not None:
            raise PermissionError(
                f"{self.settings.dashboard.password_env} is set: the environment wins, "
                "nothing is written to the file"
            )
        minimum = int(self.settings.dashboard.password_min_length or 8)
        if len(str(password)) < minimum:
            raise ValueError(f"the password must be at least {minimum} characters")
        salt = pysecrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, PBKDF2_ROUNDS)
        self.password_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "algo": "pbkdf2_hmac_sha256", "salt": salt.hex(), "hash": digest.hex(),
            "iterations": PBKDF2_ROUNDS, "updated_at": to_ts(),
        }
        tmp = self.password_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - Windows ACLs
            pass
        tmp.replace(self.password_file)
        if self.db is not None:
            self.db.audit("auth.password_changed", actor="operator", subject="dashboard")

    def _read_file(self) -> dict[str, Any] | None:
        if not self.password_file.exists():
            return None
        try:
            return json.loads(self.password_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------- sessions
    def _rate_limited(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            history = [t for t in self.attempts.get(address, []) if now - t < LOGIN_WINDOW_S]
            self.attempts[address] = history
            return len(history) >= MAX_LOGIN_ATTEMPTS

    def note_attempt(self, address: str) -> None:
        with self._lock:
            self.attempts.setdefault(address, []).append(time.monotonic())

    def start_session(self, address: str = "") -> Session:
        session = Session(token=pysecrets.token_urlsafe(32),
                          csrf=pysecrets.token_urlsafe(24), created=time.time())
        with self._lock:
            self.sessions[session.token] = session
            self.attempts.pop(address, None)
        return session

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            if time.time() - session.created > float(self.settings.dashboard.session_ttl_s):
                self.sessions.pop(token, None)
                return None
            return session

    def end_session(self, token: str | None) -> None:
        if token:
            with self._lock:
                self.sessions.pop(token, None)


# -------------------------------------------------------------------- server
class DashboardHandler(BaseHTTPRequestHandler):
    orchestrator: Any = None
    auth: Auth | None = None
    settings: Any = None
    server_version = f"SocialManager/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - stderr noise
        obs.get_logger().debug("dashboard." + fmt.split()[0], detail={"args": args})

    @property
    def scheme_host(self) -> str:
        host = self.headers.get("Host", "")
        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        return f"{scheme}://{host}"

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def _json(self, payload: Any, status: int = 200, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        headers = {"Cache-Control": "no-store", **(extra or {})}
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _html(self, text: str, status: int = 200, extra: dict[str, str] | None = None) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8",
                   {"Cache-Control": "no-store", **(extra or {})})

    def _error(self, status: int, message: str, extra: dict[str, str] | None = None) -> None:
        self._json({"error": message, "status": int(status)}, status=status, extra=extra)

    def _failed(self, exc: Exception) -> None:
        """Answer an unexpected failure as JSON, once.

        Without this an unreadable store (a database that is not a database, a
        schema from a newer version) turns every page into a dropped connection:
        the browser reports a network error, the panel keeps its loading state
        and the traceback is printed where nobody reads it.  The panels poll on
        timers, so it repeats forever.  The detail goes only to an authenticated
        operator behind the login, and it names the console command that reports
        the same thing - the same contract the trading dashboard has.
        """
        if getattr(self, "_responded", False):
            return
        self._json({
            "error": "the backend could not answer",
            "detail": f"{type(exc).__name__}: {exc}",
            "hint": ("this dashboard reads the same database the CLI does; "
                     "`python -m socialmanager doctor` reports what it can and "
                     "cannot open"),
        }, status=503)

    # ------------------------------------------------------------- session
    def current_session(self) -> Session | None:
        if self.auth is None:
            return None
        cookie = self.headers.get("Cookie", "")
        token = None
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                token = value
                break
        return self.auth.session(token)

    def _require_auth(self) -> Session | None:
        if self.settings is not None and not self.settings.dashboard.auth:
            return Session(token="no-auth", csrf="no-auth", created=time.time())
        session = self.current_session()
        if session is None:
            if self.path.startswith(API_PREFIX):
                self._error(401, "authentication required")
            else:
                self._html(dashboard_ui.render_login(
                    notice="" if (self.auth and self.auth.configured())
                    else f"Imposta {self.settings.dashboard.password_env} nell'ambiente e riavvia.",
                ), status=200)
            return None
        return session

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            referer = self.headers.get("Referer", "")
            origin = referer.split("/", 3)[0] + "//" + referer.split("/", 3)[2] if referer.startswith("http") else ""
        if not origin:
            return True     # a same-origin fetch from the page always sends one; curl does not
        return origin.rstrip("/") == self.scheme_host.rstrip("/")

    # ---------------------------------------------------------------- verbs
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/login":
            # The notice is only shown when there is genuinely no password to try:
            # telling an operator who already set the variable to set it is the kind of
            # instruction that sends them looking for a problem they do not have.
            self._html(dashboard_ui.render_login(
                notice="" if self.auth.configured() else
                f"Imposta {self.settings.dashboard.password_env} nell'ambiente e riavvia.",
            ))
            return
        if path == "/logout":
            token = self._cookie_token()
            if self.auth:
                self.auth.end_session(token)
            self._send(302, b"", "text/plain",
                       {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
                        "Location": "/login"})
            return

        session = self._require_auth()
        if session is None:
            return

        if path in ("/", "/index.html"):
            self._html(dashboard_ui.render_page({
                "csrf": session.csrf,
                "version": __version__,
                "mode": str(self.settings.mode()),
                "authenticated": True,
            }))
            return
        if path.startswith("/media/"):
            self._serve_media(path[len("/media/"):])
            return
        if path.startswith(API_PREFIX):
            try:
                payload, status = self._api_get(path, query)
            except Exception as exc:  # noqa: BLE001 - one panel must not drop the socket
                obs.warning("dashboard.api_failed",
                            detail={"path": path, "error": type(exc).__name__})
                self._failed(exc)
                return
            self._json(payload, status=status)
            return
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            self._handle_login()
            return
        session = self._require_auth()
        if session is None:
            return
        if not self._same_origin():
            self._error(403, "cross-origin request refused")
            return
        token = self.headers.get("X-CSRF-Token", "")
        if self.settings.dashboard.auth and not hmac.compare_digest(str(token), session.csrf):
            self._error(403, "invalid or missing CSRF token")
            return
        if not path.startswith(API_PREFIX):
            self._error(404, "not found")
            return
        body = self._read_body()
        if body is None:
            self._error(400, "invalid JSON body")
            return
        try:
            payload, status = self._api_post(path, body)
        except Exception as exc:  # noqa: BLE001 - an action reports, it never drops
            obs.warning("dashboard.api_failed",
                        detail={"path": path, "error": type(exc).__name__})
            self._failed(exc)
            return
        self._json(payload, status=status)

    def _cookie_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return None

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > 1_000_000:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else {}

    def _handle_login(self) -> None:
        if self.auth is None:
            self._error(500, "authentication is not configured")
            return
        address = self.client_address[0] if self.client_address else "unknown"
        if self.auth._rate_limited(address):
            ctx = self.orchestrator
            if ctx is not None:
                ctx.db.audit("auth.rate_limited", actor="anonymous", subject=address)
            self._error(429, "too many attempts, wait a few minutes",
                        extra={"Retry-After": str(LOGIN_WINDOW_S)})
            return
        if not self.auth.configured():
            self._error(503, f"no password is configured: set "
                             f"{self.settings.dashboard.password_env} and restart")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        fields = parse_qs(raw.decode("utf-8", "replace"))
        password = (fields.get("password") or [""])[0]
        if not self.auth.verify(password):
            self.auth.note_attempt(address)
            if self.orchestrator is not None:
                self.orchestrator.db.audit("auth.login_failed", actor="anonymous",
                                           subject=address)
            self._html(dashboard_ui.render_login(error="Password errata."), status=401)
            return
        session = self.auth.start_session(address)
        if self.orchestrator is not None:
            self.orchestrator.db.audit("auth.login", actor="operator", subject=address)
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self._send(302, b"", "text/plain", {
            "Set-Cookie": (f"{COOKIE_NAME}={session.token}; Path=/; HttpOnly; SameSite=Lax;"
                           f"{secure}"),
            "Location": "/",
        })

    # ------------------------------------------------------------- the media
    def _serve_media(self, name: str) -> None:
        """Serve generated assets - and only generated assets, from one directory."""
        media_root = Path(self.settings.instagram.media_dir).resolve()
        candidate = (media_root / Path(name).name).resolve()
        if media_root not in candidate.parents and candidate != media_root:
            self._error(403, "path outside the media directory")
            return
        if candidate.suffix.lower() not in {".png", ".svg", ".jpg", ".jpeg", ".webp"}:
            self._error(415, "unsupported media type")
            return
        if not candidate.is_file():
            self._error(404, "asset not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send(200, candidate.read_bytes(), content_type,
                   {"Cache-Control": "private, max-age=60"})

    # ---------------------------------------------------------------- GET API
    def _api_get(self, path: str, query: dict[str, list[str]]) -> tuple[Any, int]:
        ctx = self.orchestrator
        if ctx is None:
            return {"error": "server is not wired to an orchestrator"}, 500
        limit = _int_arg(query, "limit", 50)
        days = _int_arg(query, "days", 7)

        if path == "/api/status":
            return ctx.status(), 200
        if path == "/api/health":
            return {"ok": True, "version": __version__, "database": ctx.db.health(),
                    "kill_switch": kill_switch_engaged(ctx.db, ctx.settings),
                    "checked_at": to_ts()}, 200
        if path == "/api/overview":
            return self._overview(), 200
        if path == "/api/guide":
            return self._guide(), 200
        if path == "/api/calendar":
            planner = ctx.planner()
            calendar = planner.calendar(start=_str_arg(query, "week"), days=days)
            calendar["budget"] = planner.budget()["platforms"]
            calendar["initial_proposal"] = planner.initial_calendar(days=min(14, days * 2))
            return calendar, 200
        if path == "/api/content":
            return {"items": ctx.db.items(
                state=_str_arg(query, "state"), category=_str_arg(query, "category"),
                platform=_str_arg(query, "platform"), limit=limit)}, 200
        if path.startswith("/api/content/") and path.endswith("/previews"):
            # How each variant would look on its platform, and what would refuse
            # it.  A read path: it measures and judges, it never writes.
            item_id = _int_path(path, "/api/content/", suffix="/previews")
            item = ctx.db.item(item_id) if item_id else None
            if item is None:
                return {"error": "item not found"}, 404
            return preview.preview_item(item, settings=ctx.settings,
                                        accounts=ctx.db.accounts()), 200
        if path.startswith("/api/content/"):
            item_id = _int_path(path, "/api/content/")
            item = ctx.db.item(item_id) if item_id else None
            if item is None:
                return {"error": "item not found"}, 404
            return item, 200
        if path == "/api/approvals":
            return {"approvals": ctx.db.approvals(state=_str_arg(query, "state"), limit=limit),
                    "counts": ctx.approvals().counts()}, 200
        if path == "/api/published":
            posts = ctx.db.published_posts(limit=min(500, limit))
            return {"posts": posts, "totals": {
                "published": len([p for p in posts if p["state"] == "published"]),
                "dry_run": len([p for p in posts if p["state"] == "dry_run"]),
                "unknown": len([p for p in posts if p["state"] == "publishing"]),
                "failed": len([p for p in posts if p["state"] == "failed"]),
            }}, 200
        if path == "/api/community":
            return {"feedback": ctx.db.feedback(limit=limit),
                    "clusters": ctx.db.feedback_clusters(min_size=2)}, 200
        if path == "/api/feedback":
            from .community import CommunityAgent

            return CommunityAgent(ctx.settings, ctx.db).report(days=max(1, days)), 200
        if path == "/api/analytics":
            from .analytics import AnalyticsAgent

            return AnalyticsAgent(ctx.settings, ctx.db).report(days=max(1, days)), 200
        if path == "/api/analytics/export":
            from .analytics import AnalyticsAgent

            fmt = _str_arg(query, "fmt") or "json"
            text = AnalyticsAgent(ctx.settings, ctx.db).export(days=max(1, days), fmt=fmt)
            return {"content": text, "format": fmt}, 200
        if path == "/api/accounts":
            return {"accounts": ctx.db.accounts(),
                    "connectors": {name: ctx.connector_state(name)
                                   for name in ("github", "x", "instagram")}}, 200
        if path == "/api/connectors":
            return {name: ctx.connector_state(name)
                    for name in ("github", "x", "instagram")}, 200
        if path == "/api/agents":
            return {"runs": ctx.db.runs(limit=limit), "api_calls": ctx.db.api_calls(limit=limit)}, 200
        if path == "/api/logs":
            return {"entries": obs.tail(limit=min(500, limit), level=_str_arg(query, "level"))}, 200
        if path == "/api/audit":
            return {"events": ctx.db.audit_tail(limit=limit), "verify": ctx.db.verify_audit()}, 200
        if path == "/api/audit/verify":
            return ctx.db.verify_audit(), 200
        if path == "/api/settings":
            return {
                "stored": ctx.db.settings(),
                "effective_yaml": _effective_yaml(ctx),
                "autonomy": ctx.settings.to_dict()["autonomy"],
                "editorial": ctx.settings.to_dict()["editorial"],
                "kill_switch": kill_switch_engaged(ctx.db, ctx.settings),
                "env_present": {name: secrets.present(name)
                                for name in ctx.settings.secret_names()},
                "notifications": ctx.notifier.readiness(),
            }, 200
        return {"error": f"unknown endpoint {path}"}, 404

    # --------------------------------------------------------------- POST API
    def _api_post(self, path: str, body: dict[str, Any]) -> tuple[Any, int]:
        ctx = self.orchestrator
        actor = "dashboard"
        if ctx is None:
            return {"error": "server is not wired to an orchestrator"}, 500

        if path == "/api/actions/sync":
            result = ctx.sync(since_days=body.get("since_days"))
            return result.to_dict(), 200
        if path == "/api/actions/produce":
            result = ctx.produce(limit=max(1, min(20, int(body.get("limit") or 4))))
            return result.to_dict(), 200
        if path == "/api/actions/plan":
            result = ctx.plan(schedule=bool(body.get("schedule")), week_start=body.get("week"))
            return result.to_dict(), 200
        if path == "/api/actions/tick":
            result = ctx.tick(simulate=not bool(body.get("live")),
                              limit=max(1, min(20, int(body.get("limit") or 5))),
                              worker="dashboard")
            return result.to_dict(), 200
        if path == "/api/actions/feedback":
            return ctx.feedback().to_dict(), 200
        if path == "/api/actions/analytics":
            return ctx.analytics(days=max(1, int(body.get("days") or 30))).to_dict(), 200
        if path == "/api/actions/accounts":
            ctx.refresh_accounts()
            return {"ok": True, "accounts": ctx.db.accounts()}, 200
        if path == "/api/actions/killswitch":
            engaged = bool(body.get("engaged"))
            result = ctx.set_kill_switch(engaged, actor=actor)
            result["message"] = ("pubblicazione fermata" if engaged
                                 else "pubblicazione di nuovo consentita dalla policy")
            return result, 200
        if path == "/api/actions/publish":
            publisher = ctx.publisher()
            payloads = []
            live = bool(body.get("live"))
            format_report: dict[str, Any] | None = None
            if body.get("variant_id"):
                variant_id = int(body["variant_id"])
                variant = ctx.db.variant(variant_id)
                if variant is not None:
                    format_report = preview.preview_variant(
                        variant, item=variant.get("item"), settings=ctx.settings,
                    )["verify"]
                    # The gate stands in front of a real send only.  A simulation
                    # still runs and carries the findings back: seeing them is the
                    # point of a dry run.
                    if live and not format_report["ok"]:
                        errors = [f for f in format_report["findings"]
                                  if f["severity"] == preview.ERROR]
                        reason = "; ".join(f"{f['field']}: {f['message']}" for f in errors[:3])
                        ctx.db.audit("publish.refused", actor=actor,
                                     subject=f"content_variant:{variant_id}",
                                     detail={"errors": len(errors), "reason": reason})
                        return {"error": f"variante {variant_id} non valida per l'invio live "
                                          f"({reason})", "findings": errors}, 409
                payloads.append(publisher.publish_variant(
                    variant_id, simulate=not live, actor=actor))
            elif body.get("item_id"):
                payloads.extend(publisher.publish_item(
                    int(body["item_id"]), simulate=not live, actor=actor))
            else:
                payloads.extend(publisher.run_due(
                    limit=5, worker="dashboard", simulate=not live))
            return {"results": [p.to_dict() for p in payloads],
                    "format": format_report}, 200
        if path == "/api/approvals":
            manager = ctx.approvals()
            decision = str(body.get("decision") or "")
            note = str(body.get("note") or "")
            if decision not in {"approved", "rejected"}:
                return {"error": "decision must be 'approved' or 'rejected'"}, 400
            # Approving is a decision about content that can go out, so the format
            # gate runs first and refuses with the field-level reason.  Both entry
            # points are covered: the queue decides by ``approval_id`` and the batch
            # button loops over exactly that call, so a gate on one of them alone
            # would be bypassable from the page.
            target = None
            if body.get("approval_id"):
                approval = ctx.db.get("approval", int(body["approval_id"]))
                if approval is not None and str(approval.get("subject_type")) == "content_item":
                    target = ctx.db.item(int(approval.get("subject_id") or 0))
            elif body.get("item_id"):
                target = ctx.db.item(int(body["item_id"]))
            if decision == "approved" and target is not None:
                gate = preview.preview_item(target, settings=ctx.settings,
                                            accounts=ctx.db.accounts())
                if not gate["approvable"]:
                    reason = preview.refusal_message(gate)
                    ctx.db.audit("approval.refused", actor=actor,
                                 subject=f"content_item:{target.get('id')}",
                                 detail={"reason": reason, "blocked": gate["blocked"]})
                    return {"error": reason, "blocked": gate["blocked"],
                            "ready": gate["ready"]}, 409
            if body.get("approval_id"):
                row = manager.decide(int(body["approval_id"]), decision,
                                     decided_by=actor, note=note)
            elif body.get("item_id"):
                row = (manager.approve(int(body["item_id"]), note=note, actor=actor)
                       if decision == "approved"
                       else manager.reject(int(body["item_id"]), note=note, actor=actor))
            else:
                return {"error": "approval_id or item_id is required"}, 400
            return {"ok": True, "state": row.get("state", decision),
                    "message": f"item {row.get('subject_id')} → {row.get('state', decision)}"}, 200
        if path.startswith("/api/content/variant/"):
            variant_id = _int_path(path, "/api/content/variant/")
            variant = ctx.db.variant(variant_id) if variant_id else None
            if variant is None:
                return {"error": "variant not found"}, 404
            new_body = body.get("body")
            if not isinstance(new_body, str) or not new_body.strip():
                return {"error": "body must be a non-empty string"}, 400
            ctx.db.upsert_variant(
                int(variant["item_id"]), variant["platform"], variant["lang"], new_body,
                hashtags=str(body.get("hashtags", variant.get("hashtags") or "")),
                media=variant.get("media") or [], alt_text=variant.get("alt_text") or "",
                generator=f"manual:{actor}", risk=variant.get("risk") or "low",
            )
            ctx.db.audit("content.edited", actor=actor,
                         subject=f"content_variant:{variant_id}",
                         detail={"before": len(variant.get("body") or ""),
                                 "after": len(new_body)})
            report = ctx.policy().evaluate_and_store(variant_id, action="draft")
            return {"ok": True, "policy": report.to_dict(),
                    "message": f"variante {variant_id} salvata (policy: {report.outcome})"}, 200
        if path.startswith("/api/published/") and path.endswith("/resolve"):
            post_id = _int_path(path, "/api/published/", suffix="/resolve")
            try:
                row = ctx.publisher().resolve_claim(
                    post_id, str(body.get("outcome") or "published"),
                    external_id=str(body.get("external_id") or ""),
                    url=str(body.get("url") or ""), actor=actor)
            except (KeyError, ValueError) as exc:
                return {"error": str(exc)}, 400
            return {"ok": True, "state": row.get("state"), "message": "claim risolto"}, 200
        if path.startswith("/api/community/") and path.endswith("/reply"):
            feedback_id = _int_path(path, "/api/community/", suffix="/reply")
            from .community import CommunityAgent

            agent = CommunityAgent(ctx.settings, ctx.db)
            return agent.reply_draft(feedback_id, lang=body.get("lang")), 200
        if path == "/api/settings":
            key = str(body.get("key") or "").strip()
            if not key:
                return {"error": "key is required"}, 400
            if key in SENSITIVE_SETTING_KEYS:
                result = ctx.set_setting(key, body.get("value"), actor=actor,
                                         require_approval=True)
                result["message"] = ("modifica registrata come proposta: serve una "
                                     "approvazione umana")
                return result, 200
            result = ctx.set_setting(key, body.get("value"), actor=actor)
            result["message"] = f"{key} aggiornato"
            return result, 200
        return {"error": f"unknown endpoint {path}"}, 404

    # ------------------------------------------------------------- overview
    def _overview(self) -> dict[str, Any]:
        ctx = self.orchestrator
        status = ctx.status()
        runs = ctx.db.runs(limit=8)
        sources = ctx.db.sources(limit=12)
        pipeline = []
        for run in runs[:6]:
            pipeline.append({
                "step": f"{run['agent']} · {run['command']}",
                "state": "published" if run["state"] == "ok" else run["state"],
                "detail": run["summary"] or "in corso",
                "at": run["finished_at"] or run["started_at"],
            })
        if not pipeline:
            pipeline = [
                {"step": "sync (GitHub, read-only)", "state": "pending",
                 "detail": "mai eseguito", "at": ""},
                {"step": "produce (bozze bilingui)", "state": "pending",
                 "detail": "mai eseguito", "at": ""},
                {"step": "plan (calendario)", "state": "pending", "detail": "mai eseguito", "at": ""},
                {"step": "publish (policy + approvazioni)", "state": "pending",
                 "detail": "mai eseguito", "at": ""},
            ]
        last_sync = ctx.db.scalar(
            "SELECT MAX(finished_at) FROM agent_run WHERE command = 'sync'", (), None)
        return {"status": status, "pipeline": pipeline, "sources": [
            {k: row.get(k) for k in ("kind", "external_ref", "title", "url",
                                     "published_at", "fetched_at")} for row in sources],
            "last_sync": last_sync, "connectors": status.get("connectors", {})}


    # ------------------------------------------------------------------ guide
    def _guide(self) -> dict[str, Any]:
        """The daily cycle, thirteen steps, told with the numbers that are actually stored.

        Not a manual: a checklist whose every sentence carries a real measurement, so an
        operator can see what to do *next* without cross-reading six screens.  Where a
        value does not exist the step says so - "mai eseguita", "nessuna fonte" - rather
        than showing a plausible-looking example.  The *state* of a step is derived from
        the same readings, never asserted.
        """
        ctx = self.orchestrator
        status = ctx.status()
        stats = status["stats"]
        approvals = status["approvals"]
        budget = ctx.planner().budget()
        health = status.get("database") or {}
        audit = health.get("audit_chain") or {}
        last_sync = ctx.db.scalar(
            "SELECT MAX(finished_at) FROM agent_run WHERE command = 'sync'", (), None)
        pending = ctx.db.approvals(state="pending", limit=500)
        risk: dict[str, int] = {}
        for row in pending:
            key = str(row.get("risk") or "unknown")
            risk[key] = risk.get(key, 0) + 1
        needing_a_human = int(risk.get("high", 0)) + int(risk.get("sensitive", 0))
        unknown_posts = int(ctx.db.scalar(
            "SELECT COUNT(*) FROM published_post WHERE state = 'publishing'", (), 0))
        rows = budget.get("platforms") or []
        planned = sum(int(r.get("planned") or 0) for r in rows)
        targets = sum(int(r.get("target") or 0) for r in rows)
        gaps = [r for r in rows if int(r.get("target") or 0) > int(r.get("planned") or 0)]
        unscheduled = int(budget.get("unscheduled_drafts") or 0)
        connectors = status.get("connectors") or {}
        not_ready = {name: state for name, state in connectors.items()
                     if not state.get("ready")}
        first_gap = (f"{not_ready and list(not_ready)[0]}: "
                     + "; ".join((not_ready[list(not_ready)[0]].get("missing") or ["—"])[:1])) \
            if not_ready else ""

        steps: list[dict[str, Any]] = []

        def step(order: int, title: str, page: str, action: str, detail: str,
                 state: str = "todo") -> None:
            steps.append({"n": order, "title": title, "page": page, "action": action,
                          "detail": detail, "state": state})

        step(1, "Guarda la modalità, non i numeri", "overview",
             f"Modalità {status['mode_label']} ({status['mode']}): "
             + ("kill switch ENGAGED, nulla può uscire." if status["kill_switch"]
                else "kill switch rilasciato, decide la policy."),
             f"{stats['items']} item · {stats['variants']} varianti · "
             f"audit {'integra su ' + str(audit.get('checked')) + ' eventi' if audit.get('ok') else 'DA VERIFICARE'}",
             "blocked" if status["kill_switch"] else ("ok" if audit.get("ok") else "attention"))

        step(2, "Aggiorna le fonti", "overview",
             "Sync repo: legge GitHub in sola lettura, non scrive nulla di là.",
             f"fonti in archivio: {stats['sources']} · ultima sync "
             + (str(last_sync) if last_sync else "mai eseguita"),
             "ok" if last_sync else "todo")

        step(3, "Genera le bozze dal materiale nuovo", "studio",
             "Genera bozze: varianti bilingui dai fatti e dalle fonti già indicizzate.",
             f"{stats['items']} item ({', '.join(f'{k} {v}' for k, v in (stats.get('items_by_state') or {}).items()) or 'nessuno'}) · "
             f"{unscheduled} bozze non pianificate",
             "attention" if stats["items"] == 0 else "ok")

        step(4, "Pianifica la settimana", "calendar",
             "Pianifica e metti in coda: riempie gli slot rispettando frequenza e varietà.",
             f"{planned}/{targets} slot pianificati"
             + (f" · scoperti: {', '.join(str(r['platform']) for r in gaps)}" if gaps
                else " · nessuno scoperto")
             + ". Se un giorno resta vuoto, non c'è materiale reale da mettere: non viene inventato.",
             "ok" if rows and not gaps else "attention" if rows else "todo")

        step(5, "Decidi tu: la coda delle approvazioni", "approvals",
             "Filtra per rischio, seleziona e approva o rifiuta in blocco; la nota finisce nell'audit.",
             (f"{approvals.get('pending', 0)} in attesa"
              + (f" · {needing_a_human} ad alto rischio o sensibili" if needing_a_human else "")
              + f" · {approvals.get('approved', 0)} già approvate, {approvals.get('rejected', 0)} rifiutate")
             if approvals.get("pending") else "nessuna richiesta in attesa",
             "attention" if approvals.get("pending") else "ok")

        step(6, "Pubblica, sapendo cosa hai pubblicato", "published",
             "Prima Simula invio, poi Pubblica (live): la conferma dice piattaforma e conseguenza.",
             f"{stats['published']} pubblicati davvero · {stats['dry_run_posts']} simulati · "
             f"{unknown_posts} da verificare · {stats['scheduled_pending']} in coda",
             "attention" if unknown_posts else "ok")

        step(7, "Ascolta la community", "inbox",
             "Rileggi feedback dalle API, poi Bozza risposta: nessuna risposta parte da sola.",
             f"{stats['feedback']} feedback ({stats['feedback_new']} nuovi)",
             "attention" if stats["feedback_new"] else "ok")

        step(8, "Misura, senza inventare", "analytics",
             "Raccogli metriche ora: quello che la piattaforma non restituisce resta 'non disponibile'.",
             f"{stats['api_calls']} chiamate API registrate, {stats['api_errors']} con errore",
             "attention" if stats["api_errors"] else "ok")

        step(9, "Sistema gli account", "accounts",
             "Aggiorna stato: mostra per nome di variabile e impronta, mai il token.",
             (f"non pronti: {', '.join(not_ready)} · {first_gap}" if not_ready
              else "tutti i connettori sono pronti"),
             "attention" if not_ready else "ok")

        stored_settings = ctx.db.settings() or {}
        env_present = [name for name in ctx.settings.secret_names() if secrets.present(name)]
        step(10, "Rivedi autonomia e credenziali", "settings",
             "I limiti di autonomia si cambiano da qui, ma una modifica sensibile diventa una "
             "proposta che aspetta un'approvazione umana; le variabili sono mostrate solo per nome.",
             f"modalità {status['mode']} · {len(stored_settings)} impostazioni memorizzate · "
             f"{len(env_present)}/{len(ctx.settings.secret_names())} variabili d'ambiente presenti",
             "ok")

        failed_runs = int(ctx.db.scalar(
            "SELECT COUNT(*) FROM agent_run WHERE state <> 'ok'", (), 0))
        step(11, "Controlla gli agenti e le chiamate reali", "agents",
             "Qui si vede cosa è stato eseguito e quali chiamate HTTP sono davvero uscite: i "
             "connettori in simulazione non chiamano nulla.",
             f"{stats['runs']} esecuzioni, {failed_runs} non riuscite · "
             f"{stats['api_calls']} chiamate API ({stats['api_errors']} con errore)",
             "attention" if failed_runs or stats["api_errors"] else "ok")

        clusters = ctx.db.feedback_clusters(min_size=2)
        step(12, "Trasforma il feedback ricorrente in proposte", "intelligence",
             "I problemi che tornano due volte o più diventano proposte di issue: nessuna issue "
             "viene aperta senza una decisione umana.",
             f"{stats['feedback']} feedback ({stats['feedback_new']} nuovi) · "
             f"{len(clusters)} problemi ricorrenti",
             "attention" if clusters else "ok")

        step(13, "Chiudi la giornata", "logs",
             "Verifica catena: l'audit è append-only, i trigger rifiutano UPDATE e DELETE.",
             f"{stats['audit_events']} eventi di audit · "
             + (f"catena integra su {audit.get('checked')}" if audit.get("ok")
                else "CATENA DA VERIFICARE"),
             "ok" if audit.get("ok") else "attention")

        return {"mode": status["mode"], "mode_label": status["mode_label"],
                "kill_switch": status["kill_switch"], "platforms_allowed":
                status.get("platforms_allowed") or [], "languages": status.get("languages") or [],
                "steps": steps, "generated_at": status.get("generated_at")}


def _int_arg(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default


def _str_arg(query: dict[str, list[str]], name: str) -> str | None:
    value = (query.get(name) or [None])[0]
    return str(value) if value else None


def _int_path(path: str, prefix: str, suffix: str = "") -> int:
    text = path[len(prefix):]
    if suffix and text.endswith(suffix):
        text = text[: -len(suffix)]
    try:
        return int(text.strip("/"))
    except ValueError:
        return 0


def _effective_yaml(ctx: Any) -> str:
    from . import config as config_mod

    return config_mod.dumps(ctx.settings)


# ---------------------------------------------------------------- lifecycle
def bind_refusal(settings: Any, auth: Auth, host: str) -> str | None:
    """Why this bind must be refused - or ``None`` when it is safe."""
    if host in {"127.0.0.1", "localhost", "::1"}:
        return None
    if not settings.dashboard.auth:
        return ("a non-loopback bind with dashboard.auth=false is refused: the admin "
                "panel may not be reachable without a password")
    if not auth.configured():
        return (f"a non-loopback bind needs a password: set "
                f"{settings.dashboard.password_env} or change the stored one first")
    return None


def make_server(orchestrator: Any, host: str | None = None, port: int | None = None,
                no_auth: bool = False) -> ThreadingHTTPServer:
    settings = orchestrator.settings
    auth = Auth(settings, orchestrator.db)
    if no_auth:
        settings.dashboard.auth = False
    bind = host or settings.dashboard.host
    refusal = bind_refusal(settings, auth, bind)
    if refusal:
        raise SystemExit(f"refusing to start: {refusal}")
    handler = type("BoundDashboardHandler", (DashboardHandler,), {
        "orchestrator": orchestrator, "auth": auth, "settings": settings,
    })
    # ``port is None`` rather than ``port or ...``: port 0 is a legitimate (and
    # useful) request for any free port, and silently substituting the
    # configured one would make two servers collide.
    httpd = ThreadingHTTPServer(
        (bind, settings.dashboard.port if port is None else int(port)), handler
    )
    httpd.daemon_threads = True
    return httpd


def serve(orchestrator: Any, host: str | None = None, port: int | None = None,
          no_auth: bool = False) -> int:
    httpd = make_server(orchestrator, host, port, no_auth)
    settings = orchestrator.settings
    address = httpd.server_address
    auth = httpd.RequestHandlerClass.auth  # type: ignore[attr-defined]
    print(f"Social Manager dashboard: http://{address[0]}:{address[1]}")
    print(f"  auth        : {'on' if settings.dashboard.auth else 'OFF (loopback only)'} "
          f"(password source: {auth.source()})")
    print(f"  mode        : {settings.mode()}")
    print(f"  kill switch : {'ENGAGED' if kill_switch_engaged(orchestrator.db, settings) else 'released'}")
    if settings.dashboard.auth and not auth.configured():
        print(f"  NOTE: no password is set. Export {settings.dashboard.password_env} "
              "before opening the page.")
    print("  press Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0
