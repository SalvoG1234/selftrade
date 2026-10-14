"""Local dashboard: a stdlib ``http.server`` with a JSON API, a login and a live chart.

What the page is, in the order a new operator meets it:

1. **a login.**  ``dashboard.auth`` is on by default and is *mandatory* for any
   non-loopback bind (opening the loop controls on the LAN without a password is
   a mistake this code refuses to make).  The password comes from the environment
   variable named by ``dashboard.password_env`` when it is set - an operator
   override the page refuses to change - and otherwise from the **stored
   credential** in ``dashboard.password_file``, which starts as the bootstrap
   password ``0000`` and is changed by ``POST /api/password`` from the Settings
   card (a salted PBKDF2 hash on disk; see :mod:`selftrade.credentials`).  Without
   either, a hand-built ``Auth`` generates a random password and prints it once.
   The session is a signed, HttpOnly, SameSite=Strict cookie with a TTL - a
   per-process secret, no third-party auth service.  A password change rotates the
   signing key, so every other session is signed out at the same time.
2. **a live chart.**  ``/api/market`` returns recent candles and 24h stats from
   Binance **public** data (keyless, read-only, the same client ``data --fetch``
   uses) and the browser redraws them every ``dashboard.market_refresh_s``: real
   candles that move, not a static equity curve.
3. **the bot panels** the dashboard already had (overview, paper, equity,
   experiments, hall, trades, lessons, history) and the loop controls.
4. **a Binance key panel.**  ``/api/binance`` runs the same read-only probe as
   ``binance check`` - permissions, clock offset, signature, balances, open orders,
   the rate budget - on an **unarmed** broker, so nothing can be sent from the
   browser, and refreshes it every ``dashboard.key_refresh_s``.  When the rail is
   switched off the panel says so, with the hint that turns it on, instead of
   pretending there is nothing to see.

   Next to it, **the API keys page** is the one place a value can be *typed*:
   ``GET /api/keys`` reports, for the rail credential names the config names,
   where each effective value comes from - the **environment**, which always
   wins, or the local key file (``state/secrets.json`` by default) - plus a
   SHA-256 **fingerprint** so "did the key change?" is answerable, and
   ``POST /api/keys`` stores or forgets one of those names, always with an
   explicit ``confirm`` and filed as a ``key_store`` risk event naming the
   credential.  No method of this API ever returns a value, the names are the
   rails' own rather than free-form, and the page says plainly that the file is
   **clear text** and that a key in it arms nothing (:mod:`selftrade.secrets`).
6. **an emergency stop.**  ``POST /api/killswitch`` arms or resets the *same*
   file-backed kill switch the risk gate, the paper loop and the Binance adapter
   consult before every order, and ``GET /api/killswitch`` shows its state and audit
   trail, refreshed with the panels.  It is the one write action this otherwise
   read-only page offers, and it is offered precisely because of what it cannot do:
   arming *refuses* orders, it never places one.  Both directions need an explicit
   ``confirm`` in the body, so a stray POST cannot flip the switch.
7. **a risk-limits panel.**  ``GET /api/risk`` measures the active paper session
   against the sealed caps - drawdown, today's P&L and exposure - and reports the
   **headroom** left before each one trips, with the two limits that can halt trading
   called out from the two that only refuse an order.  The arithmetic is not repeated
   here: the panel hands the account to the pre-trade gate's own limit functions, so
   the page cannot show a limit the gate enforces differently.  Read-only, one DB
   read, no exchange and no rate budget.  It also says whether the optional alert
   channel is on and **what it still owes** (``pending``), because a red row nobody
   has been told about is the one row worth acting on.
8. **an alerts card.**  ``GET /api/alerts`` reads the alert channel's own ledger and
   lists what it has actually **sent** - when, which kind, which band, and the first line
   of what was said - newest first, with a halt the *program* armed marked as such.  Only
   deliveries are recorded there, so the page cannot show a message that never left the
   machine; read-only, one file read, no writes and no send.
9. **the capital account.**  ``GET /api/capital`` shows the simulated account the agents
   trade - the free balance, what has been credited and withdrawn, what is allocated to
   the active session, and the append-only ledger with each movement's reason - and
   ``POST /api/capital`` credits, withdraws, allocates or releases it, always with an
   explicit ``confirm`` and a reason.  It moves **simulated** capital between a local
   ledger and a local paper session: no exchange call, no key, no order, and no route
   here from the model side (see :mod:`selftrade.capital`).
10. **the loop controls.**  ``start`` **launches the tracked loop** - ``python -m
   selftrade evolve --session`` - as a detached child of this server, and the pid, the
   log (``state/loop.log``) and the generation count come back with it; ``pause``,
   ``resume`` and ``stop`` are delivered to that process through the session file, and
   are **refused when nothing is running** rather than written into a file no one reads
   (see :mod:`selftrade.loop`).  It is the one process this page can start, and it is
   the research loop: it evaluates genomes, it never places an order.

Endpoints: ``/api/whoami``, ``/api/login``, ``/api/logout``, ``/api/password``,
``/api/health``, ``/api/overview``, ``/api/equity``, ``/api/trades``,
``/api/experiments``, ``/api/hall``, ``/api/lessons``, ``/api/history``,
``/api/session``, ``/api/paper``, ``/api/market``, ``/api/binance``, ``/api/keys``,
``/api/risk``, ``/api/alerts``, ``/api/capital``, ``/api/lab``, ``/api/logs``,
``/api/settings``, ``GET /api/killswitch``, ``POST /api/killswitch``, ``POST /api/keys``,
``POST /api/capital``, ``POST /api/password`` and ``POST /api/control``.

The last three are read-only views for the AI Lab, the system logs and the settings
page: the lab joins the run history with the report the program wrote, the logs read
``risk_events``, ``api_calls`` and the tail of the loop log, and the settings page
renders the config with environment variable **names** only (never a value).

Security posture, stated plainly: this is a **local** dashboard.  It serves no
remote assets (everything is inline), the cookie is not sent cross-site, and a
POST whose ``Origin`` is not its own host is rejected.  It is not a hardened
internet-facing service - for that, put it behind a reverse proxy with TLS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from pathlib import Path

from . import __version__
from .binance import PRODUCTION_HOSTS, TESTNET_HOST, key_health
from .config import is_loopback
from .credentials import (
    DEFAULT_MIN_LENGTH,
    DEFAULT_PASSWORD,
    CredentialError,
    CredentialStore,
    check_new_password,
)
from .dashboard_ui import HTML_PAGE
from .data import DEFAULT_FETCH_BASE_URL, fetch_klines, fetch_ticker
from .execution import KillSwitch, kill_switch_path
from .loop import LoopRunner, loop_log_path
from .memory import Memory
from .repro import fingerprint as repro_fingerprint
from .risk_report import limits_report
from .risk_rules import LIMITS, SEALED_DIGEST, digest_of, is_sealed
from .secrets import DEFAULT_FILE as DEFAULT_SECRET_FILE
from .secrets import SecretError, SecretStore
from .session import Session

COOKIE_NAME = "st_session"
MAX_LOGIN_FAILURES = 8
LOGIN_FAILURE_WINDOW_S = 300.0

#: The kind of ``risk_events`` row a switch action is filed under.  A *blocked order*
#: is filed as ``ALERT_KIND``; a switch action is its own kind - the same one the
#: switch is given wherever else it stops something.
KILL_SWITCH_KIND = "kill_switch"

#: The kind of ``risk_events`` row a rail-credential change is filed under.  The row records
#: *which* name changed and who changed it - never a value, because a value never leaves the
#: store (:mod:`selftrade.secrets`).
KEY_STORE_KIND = "key_store"
MAX_REASON_CHARS = 300
HISTORY_TAIL = 5

#: The risk panel's thresholds and measurement live in ``selftrade.risk_report``,
#: because the CLI and the alert channel need the same answer (see ``GET /api/risk``).

#: The largest run report the lab panel will read.  A run report is a few hundred KiB;
#: this bounds a path that came out of the database, not out of this module.
MAX_REPORT_BYTES = 8_000_000

#: How many lines of the tracked loop's log the logs panel returns.
LOOP_LOG_TAIL = 200


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

# The page itself lives in :mod:`selftrade.dashboard_ui` (presentation split from
# this server logic); ``HTML_PAGE`` is imported at the top of this module.


# --------------------------------------------------------------------------
# auth: password login, signed cookie, no stored secret
# --------------------------------------------------------------------------

class Auth:
    """Password check + signed session cookies, all in one process.

    Where the password comes from, in order of precedence:

    1. the environment variable named by ``dashboard.password_env``, if it is set.  This is
       the operator's override - a machine that provisions dashboards from the environment
       must not be surprised by a password someone changed in a browser - so the page
       **refuses** to change it and says which variable is in charge instead of writing a
       hash nothing would ever read;
    2. the stored credential in ``dashboard.password_file`` (see
       :mod:`selftrade.credentials`), which starts as the bootstrap default ``0000`` and is
       what the Settings card changes.  Only a salted hash is written, never the password;
    3. with neither (only a hand-built ``Auth``): a random password for this run, as before.

    The cookie is ``issued.signature``, signed with a per-process key, so a restart
    invalidates every session.  Changing the password rotates that key, which signs every
    *other* session out; the response to the change carries a fresh cookie.
    """

    def __init__(
        self,
        enabled: bool,
        password: str | None,
        ttl_s: int = 86400,
        store: CredentialStore | None = None,
        min_length: int | None = None,
    ):
        self.enabled = bool(enabled)
        self.store = store
        self.min_length = int(min_length or DEFAULT_MIN_LENGTH)
        if password:
            self._password: str | None = str(password)
            self._source = "environment"
        elif store is not None:
            self._password = None  # resolved through the store on every check
            self._source = "stored"
        else:
            self._password = secrets.token_urlsafe(12)
            self._source = "generated"
        self.generated = self._source == "generated"
        self.ttl_s = int(ttl_s)
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    # -------------------------------------------------- where it comes from
    @property
    def source(self) -> str:
        """``environment`` | ``stored`` | ``generated``."""
        return self._source

    @property
    def from_environment(self) -> bool:
        return self._source == "environment"

    @property
    def changeable(self) -> bool:
        """False when an environment variable owns the password (and for a run-only one)."""
        return self._source == "stored" and self.store is not None

    def current_is_default(self) -> bool:
        """True while the login still accepts the bootstrap password."""
        return self.exposes_the_bootstrap_password()

    def exposes_the_bootstrap_password(self) -> bool:
        """True while the password anyone can read in the README is still accepted.

        :meth:`CredentialStore.is_default` asks this of the *stored* record; this asks it of
        the password actually **in force**, because that is what a network bind has to look
        at: an environment variable set to the bootstrap value is the same open door, and
        the stored record is not what the login reads once that variable exists.  A password
        generated for this run is random and printed once at startup, so it is not one.
        """
        if self._password is not None:
            return hmac.compare_digest(self._password, DEFAULT_PASSWORD)
        if self.store is not None:
            return self.store.is_default()
        return False

    def describe(self) -> dict[str, Any]:
        """What the Settings card renders: the source and the policy, never the hash."""
        detail = self.store.describe() if self.store is not None else {}
        if self._source == "environment":
            note = ("the password is taken from the environment variable in "
                    "`dashboard.password_env`, which wins over anything stored here: "
                    "unset it and restart to manage the password from this page")
            source = "environment"
        elif self._source == "generated":
            note = ("no environment variable and no store: this run generated a random "
                    "password and printed it once at startup")
            source = "generated"
        else:
            note, source = detail.get("note", ""), detail.get("source", "stored")
        return {
            **detail,
            "source": source,
            "is_default": self.current_is_default(),
            "changeable": self.changeable,
            "min_length": self.min_length,
            "environment_variable": None if not self.from_environment else "set",
            "sessions": ("changing the password signs out every other session "
                         "(the cookie is signed with a key this process rotates)"),
            "note": note,
        }

    def verify(self, password: str) -> bool:
        if self._password is not None:
            return hmac.compare_digest(str(password), self._password)
        if self.store is not None:
            return self.store.verify(password)
        return False

    def change(self, current: Any, new: Any, confirmed: bool) -> tuple[int, dict[str, Any]]:
        """Replace the stored password, or refuse and write nothing.

        Refusals are checked in this order: the confirmation, who owns the password, the
        *current* password (a session cookie is enough to use the dashboard, but not enough
        to lock its owner out), then the length and the difference from the current one.
        Every failure returns a code and a sentence, and touches no file.
        """
        if not confirmed:
            return 400, {
                "ok": False,
                "confirm_required": True,
                "error": ("changing the password signs out every other session: "
                          "send confirm=true to do it"),
            }
        if self.store is None:
            return 400, {"ok": False, "error": (
                "this dashboard keeps no password store: set `dashboard.password_env` instead"
            )}
        if not self.changeable:
            return 400, {"ok": False, "error": (
                "the password comes from the environment variable in `dashboard.password_env`, "
                "which wins over this store: unset it and restart to change the password here"
            )}
        if not self.verify(str(current or "")):
            return 401, {"ok": False, "error": "the current password is wrong"}
        refusal = check_new_password(new, current=str(current or ""), min_length=self.min_length)
        if refusal:
            return 400, {"ok": False, "error": refusal}
        try:
            self.store.set(str(new), by="dashboard", min_length=self.min_length)
        except CredentialError as exc:
            return 400, {"ok": False, "error": str(exc)}
        # Rotating the signing key invalidates every cookie issued so far - including the
        # one making the change, which is why the caller re-issues a fresh cookie in the
        # same response.  A password change that left old sessions alive would not be one.
        self._key = secrets.token_bytes(32)
        return 200, {
            "ok": True,
            "changed": True,
            "sessions_signed_out": True,
            "password": self.describe(),
        }

    def too_many_failures(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < LOGIN_FAILURE_WINDOW_S]
            self._failures[client] = recent
            return len(recent) >= MAX_LOGIN_FAILURES

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.monotonic())

    def clear_failures(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def issue(self) -> str:
        issued = str(int(time.time()))
        return f"{issued}.{self._sign(issued)}"

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def check(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        issued, _, signature = token.partition(".")
        try:
            age = time.time() - int(issued)
        except ValueError:
            return False
        if age < 0 or age > self.ttl_s:
            return False
        return hmac.compare_digest(self._sign(issued), signature)


# --------------------------------------------------------------------------
# the live market feed (Binance public data, no keys)
# --------------------------------------------------------------------------

class MarketFeed:
    """Candles + 24h stats from the public mirror, cached for the refresh window.

    The TTL keeps a room full of open tabs from turning into a request flood: at
    most one upstream call per (symbol, interval) per ``min(2s, refresh)``.  Any
    failure is returned as ``ok: false`` with the reason, never raised - a chart
    must not be able to break the dashboard.
    """

    def __init__(self, settings: Any):
        self.settings = settings
        self.base_url = getattr(settings.data, "fetch_base_url", DEFAULT_FETCH_BASE_URL)
        self.timeout = float(getattr(settings.data, "fetch_timeout", 20.0))
        self.ttl = min(2.0, float(settings.dashboard.market_refresh_s))
        self._cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def snapshot(self, symbol: str, interval: str, bars: int) -> dict[str, Any]:
        key = (symbol, interval, bars)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl:
                out = dict(hit[1])
                out["cached"] = True
                return out

        started = time.perf_counter()
        try:
            # Two independent calls: overlap them, so the latency is the slower one
            # instead of the sum (the mirror answers in ~2-3 s from here).
            with ThreadPoolExecutor(max_workers=2) as pool:
                klines_future = pool.submit(
                    fetch_klines, symbol, interval, bars,
                    base_url=self.base_url, timeout=self.timeout,
                )
                ticker_future = pool.submit(
                    fetch_ticker, symbol, base_url=self.base_url, timeout=self.timeout,
                )
                df = klines_future.result()
                ticker = ticker_future.result()
        except Exception as exc:  # noqa: BLE001 - surfaced in the payload, never raised
            return {
                "ok": False,
                "symbol": symbol,
                "interval": interval,
                "error": str(exc),
                "candles": [],
                "ticker": {},
                "source": self.base_url,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        candles = [
            {
                "t": int(ts.timestamp() * 1000),
                "o": float(o),
                "h": float(h),
                "l": float(l),
                "c": float(c),
                "v": float(v),
            }
            for ts, o, h, l, c, v in zip(
                df.index, df["open"], df["high"], df["low"], df["close"], df["volume"]
            )
        ]
        out = {
            "ok": True,
            "symbol": symbol,
            "interval": interval,
            "source": self.base_url,
            "candles": candles,
            "ticker": ticker,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "cached": False,
        }
        with self._lock:
            self._cache[key] = (time.monotonic(), out)
        return out


# --------------------------------------------------------------------------
# the Binance key panel: the ``binance check`` report, cached
# --------------------------------------------------------------------------

class KeysPanel:
    """The rail credentials: what is available, where it comes from, and nothing else.

    The dashboard has always been able to *report* a missing key (``/api/binance``) and never
    able to accept one, because the project reads secrets from the environment.  This panel is
    the deliberate exception the operator asked for, and it keeps every rule that made the
    read-only version safe:

    * the **environment wins** - a name exported in the shell is the value the rail uses, and
      the snapshot says so, so a machine that already exports the pair cannot be surprised by
      a file someone left lying around;
    * **no value is ever returned**, by any method: the snapshot carries the name, the source,
      whether it is set and a **fingerprint** (a SHA-256 prefix), which answers "did it change?"
      without answering "what is it?";
    * the names are a **fixed list** of what this machine's rails actually read, so this cannot
      become a place to park an arbitrary secret under an arbitrary name;
    * a write needs ``confirm: true``, exactly like the kill switch and a capital movement, and
      it is **audited** as a ``key_store`` risk event naming the credential - never the value;
    * storing a key **arms nothing**.  ``binance.enabled``, ``live`` and ``--live`` still gate
      every order, and the sealed risk gate and the kill switch still gate each one, which is
      why the snapshot reports the rail's own switches beside the credentials.
    """

    #: ``(label, attribute)`` on ``settings.binance`` - the only names this panel will touch.
    NAMES: tuple[tuple[str, str, str], ...] = (
        # (label, attribute on settings.binance, role the page pairs with its own field)
        ("Binance API key", "api_key_env", "key"),
        ("Binance API secret", "api_secret_env", "secret"),
    )

    def __init__(self, settings: Any):
        self.settings = settings
        self.path = str(
            getattr(settings.binance, "key_file", None) or DEFAULT_SECRET_FILE
        )

    def store(self) -> SecretStore:
        return SecretStore(self.path)

    def names(self) -> list[tuple[str, str, str]]:
        return [
            (label, str(getattr(self.settings.binance, attr, "")), role)
            for label, attr, role in self.NAMES
        ]

    def snapshot(self) -> dict[str, Any]:
        """Everything the page shows, and no value anywhere in it."""
        pairs = self.names()
        described = self.store().describe([name for _, name, _role in pairs])
        by_name = {entry["name"]: entry for entry in described["vars"]}
        rail = self.settings.binance
        base_url = str(getattr(rail, "base_url", "") or "")
        host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
        return {
            "ok": True,
            "file": described["file"],
            "exists": described["exists"],
            "corrupt": described["corrupt"],
            "material": described["material"],
            "environment_wins": True,
            "max_value_chars": described["max_value_chars"],
            "entries": [
                {
                    "label": label,
                    "variable": name,
                    "role": role,
                    **{
                        k: v
                        for k, v in by_name.get(
                            name, {"name": name, "set": False, "source": None}
                        ).items()
                        if k != "name"
                    },
                }
                for label, name, role in pairs
            ],
            "rail": {
                "name": "binance",
                "enabled": bool(getattr(rail, "enabled", False)),
                "live": bool(getattr(rail, "live", False)),
                "allow_production": bool(getattr(rail, "allow_production", False)),
                "base_url": base_url,
                "host_is_testnet": TESTNET_HOST in base_url,
                "production": host in PRODUCTION_HOSTS,
                "probe": "/api/binance",
            },
            "note": (
                described["note"]
                + " Storing a value here changes what this rail can sign with, never whether it"
                " sends anything: the key probe is what says whether a stored key actually"
                " works."
            ),
        }

    def act(self, action: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Store or forget one credential.  Confirmed, named, audited, never echoed."""
        if body.get("confirm") is not True:
            return 400, {
                "ok": False,
                "error": "confirmation required",
                "confirm_required": True,
                "hint": "storing or clearing a credential is a write: send confirm=true to do it",
            }
        known = {name: label for label, name, _role in self.names()}
        wanted = str(body.get("name") or "")
        if wanted not in known:
            return 400, {
                "ok": False,
                "error": f"{wanted or 'no name'} is not a credential this machine reads",
                "hint": "this page stores only: " + ", ".join(sorted(known)),
            }
        store = self.store()
        try:
            if action == "set":
                store.set(wanted, body.get("value"), by=str(body.get("by") or "dashboard"))
                result = "stored"
            elif action == "clear":
                result = "removed" if store.clear(wanted) else "was not stored"
            else:
                return 400, {
                    "ok": False,
                    "error": f"unknown action {action!r}",
                    "actions": ["set", "clear"],
                }
        except SecretError as exc:
            return 400, {"ok": False, "error": str(exc)}

        event_id, event_error = self._record(str(action), wanted, known[wanted], body)
        out = self.snapshot()
        out.update(
            action=str(action),
            name=wanted,
            label=known[wanted],
            result=result,
            event_id=event_id,
            event_error=event_error,
        )
        return 200, out

    def _record(
        self, action: str, name: str, label: str, body: dict[str, Any]
    ) -> tuple[int | None, str | None]:
        """File the change where the other security-relevant decisions are audited.

        Returns ``(row_id, error)`` so the page can say "recorded" and "could not record it"
        as two different things - and the row names the credential without holding a value.
        """
        try:
            row = Memory(self.settings.memory.db).add_risk_event(
                None,
                KEY_STORE_KIND,
                f"rail credential {action} from the dashboard: {label} ({name})",
                {
                    "action": action,
                    "name": name,
                    "label": label,
                    "by": str(body.get("by") or "dashboard"),
                    "file": self.path,
                },
            )
            return int(row), None
        except Exception as exc:  # noqa: BLE001 - reported to the operator, never hidden
            return None, str(exc)


class KeyHealth:
    """The state of the Binance key, cached for ``dashboard.key_refresh_s`` seconds.

    It runs exactly what ``binance check`` runs (``binance.key_health``) and nothing
    else: the broker behind that call is built **unarmed**, so a POST cannot come out
    of this path even if something tried.  Every refresh is a real signed request
    against the exchange's rate budget, which is why the TTL exists - it is both the
    cache window and the browser's polling period, so a roomful of tabs still costs
    one probe per window.

    A probe that cannot even be built (rail disabled, credentials missing, host
    refused) is rendered from the same shape with the reason attached, so the panel
    never has to guess between "nothing to report" and "could not ask".

    The probe is looked up as a module global at call time, like ``fetch_klines`` is
    for the market feed: a test patches ``dashboard.key_health`` and the whole panel
    runs with no key and no network.
    """

    def __init__(self, settings: Any):
        self.settings = settings
        self.ttl = float(settings.dashboard.key_refresh_s)
        self._cache: dict[str, Any] | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def snapshot(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache
            if not force and hit is not None and now - self._at < self.ttl:
                out = dict(hit)
                out["cached"] = True
                return out

        started = time.perf_counter()
        try:
            report = key_health(self.settings, memory=Memory(self.settings.memory.db))
        except Exception as exc:  # noqa: BLE001 - a panel must not be able to break the page
            rail = getattr(self.settings, "binance", None)
            report = {
                "ok": False,
                "verdict": "not checked",
                "base_url": str(getattr(rail, "base_url", "") or ""),
                "enabled": bool(getattr(rail, "enabled", False)),
                "problems": [f"the probe could not run: {exc}"],
                "hints": ["fix the configuration or the environment, then press recheck"],
            }
        out = dict(report)
        out.setdefault("ok", False)
        out.setdefault("verdict", "not checked")
        for key, empty in (
            ("steps", []),
            ("problems", []),
            ("warnings", []),
            ("hints", []),
            ("credentials", {}),
            ("clock", {}),
            ("signature", {}),
            ("permissions", {}),
            ("limits", {}),
            ("http", {"calls": 0, "posts": 0, "methods": [], "paths": [], "read_only": True}),
            ("account", {"balances": [], "balances_total": 0}),
            ("open_orders", {"count": 0, "orders": []}),
        ):
            if out.get(key) is None:
                out[key] = empty
        out["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        out["cached"] = False
        out["refresh_s"] = self.ttl
        with self._lock:
            self._cache = out
            self._at = time.monotonic()
        return dict(out)


# --------------------------------------------------------------------------
# the emergency stop: the kill switch, flipped from the page
# --------------------------------------------------------------------------

class KillSwitchPanel:
    """The emergency stop: the switch the gate and the loop obey, flipped here.

    This is **not** a dashboard-only flag.  ``kill_switch_path`` resolves to the file
    the risk gate, the paper loop and the Binance adapter read before every order, so
    one click stops new orders on every rail at once - and ``blocked`` survives a
    restart, which is the entire point of a switch.

    Every action is recorded twice, because "I pressed stop" has to be checkable
    later: in the switch's own persisted ``history`` (what the CLI's
    ``switch show|arm|reset`` writes) and as a ``risk_events`` row of kind
    ``kill_switch`` (the same table the gate's blocked orders land in).  The audit
    row is best-effort and its failure is *reported* rather than hidden - the switch
    state is the control, and a risk control must not be defeated by a locked
    database - but it is never silently missing either.

    A read is one JSON file: no network, no exchange, no rate budget, so the state
    can be polled as often as the panels are.  Note what this path *cannot* do: it
    can only refuse.  Arming a stop places no order, which is why it is the one
    write action a page that deliberately cannot trade is allowed to offer.
    """

    ACTIONS = ("arm", "reset")
    DEFAULT_ARM_REASON = "armed by the operator from the dashboard"
    DEFAULT_RESET_REASON = "reset by the operator from the dashboard"

    def __init__(self, settings: Any):
        self.settings = settings
        self.path = kill_switch_path(settings)
        # The switch file is a small read-modify-write with no cross-process lock, and
        # two tabs (or a double click) can land at the same instant.  This serialises
        # *this* server's writes so the history cannot lose one; a second process
        # editing the same file is still uncoordinated - arm from one place at a time.
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        """The switch's state, its trail, and when it was read (cheap, no network)."""
        switch = KillSwitch(self.path)
        info = switch.describe()
        history = list(switch.state().get("history") or [])
        return {
            "ok": True,
            "blocked": bool(info["blocked"]),
            "reason": info.get("reason", ""),
            "triggered_by": info.get("triggered_by", ""),
            "triggered_at": info.get("triggered_at"),
            "path": info.get("path", str(self.path)),
            "events": info.get("events", 0),
            # The numbers the *program* saw when it armed itself (see `notify`'s HALT
            # message): only the gate records them, and only at the refusal, so an empty
            # dict is the normal answer for an operator's own arm.
            "detail": info.get("detail") or {},
            "history": history[-HISTORY_TAIL:],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def act(self, action: Any, reason: Any = "", by: str = "dashboard") -> tuple[int, dict[str, Any]]:
        """Arm or reset, record it, and answer with the state *after* the write.

        The answer is a fresh ``snapshot``, so the page never has to guess whether the
        click took: it reports what the switch file says now.
        """
        if action not in self.ACTIONS:
            return 400, {
                "ok": False,
                "error": f"invalid action {action!r}; use arm|reset",
            }
        reason = " ".join(str(reason or "").split())[:MAX_REASON_CHARS]
        with self._lock:
            switch = KillSwitch(self.path)
            if action == "arm":
                state = switch.trigger(reason or self.DEFAULT_ARM_REASON, by=by)
            else:
                state = switch.reset(reason or self.DEFAULT_RESET_REASON, by=by)
            event_id, event_error = self._record(action, reason, by, state)
            out = self.snapshot()
        out.update({
            "action": action,
            "by": by,
            "event_id": event_id,
            "event_error": event_error,
        })
        return 200, out

    def _record(
        self, action: str, reason: str, by: str, state: dict[str, Any]
    ) -> tuple[int | None, str | None]:
        """File the action where every other risk decision is audited.

        Returns ``(row_id, error)``: the caller reports both, so "recorded" and "could
        not be recorded" are different answers instead of the same silence.
        """
        try:
            row = Memory(self.settings.memory.db).add_risk_event(
                None,
                KILL_SWITCH_KIND,
                f"kill switch {action} from the dashboard: {state.get('reason') or reason}",
                {
                    "action": action,
                    "by": by,
                    "reason": reason,
                    "blocked": bool(state.get("blocked")),
                    "path": str(self.path),
                },
            )
            return int(row), None
        except Exception as exc:  # noqa: BLE001 - reported to the operator, never hidden
            return None, str(exc)


class CapitalPanel:
    """The capital account the agents trade: what was credited, and what it funds.

    A thin wrapper over :mod:`selftrade.capital`, which is also what the ``capital`` CLI
    command and ``paper --fund`` use - one ledger, one set of rules, three ways in.  The
    page adds only what a *page* has to add: an explicit ``confirm`` (the same two-step
    the kill switch uses, because this is a write) and a refusal that comes back as a
    message instead of a stack trace.

    What it cannot do, and why that is the point: it moves **simulated** capital between
    a local ledger and a local paper session.  There is no exchange call anywhere in this
    path, no key is read, and no order is built.  The model side - the Evolver, the
    proposer, the evaluators, the critic - has no route here at all, so a strategy cannot
    fund itself; only an operator can, and every credit carries a reason and lands in the
    append-only ledger.
    """

    #: The movements a person may make from the page.  Reading is the default; these are
    #: the only writes, and each one needs ``confirm: true`` and a reason.
    ACTIONS = ("deposit", "withdraw", "allocate", "release")

    def __init__(self, settings: Any):
        self.settings = settings
        # Two clicks (or two tabs) landing together must not both read the same balance:
        # the ledger serialises its own writes, and this keeps this server's in order.
        self._lock = threading.Lock()

    def snapshot(self, limit: int = 50) -> dict[str, Any]:
        """The account, its policy and the newest ledger rows.  One DB read, no writes."""
        from . import capital as capital_mod

        out = capital_mod.summary(
            self.settings, Memory(self.settings.memory.db), limit=limit
        )
        out["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return out

    def act(self, action: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Credit, withdraw, allocate or release, and answer with the account after it.

        ``400`` for anything refused - an unknown action, a missing amount, no reason, a
        credit the policy caps, a session that has already traded - and the refusal's own
        sentence back to the page.  ``200`` only once the ledger row exists.
        """
        from . import capital as capital_mod

        name = str(action or "").strip().lower()
        if name not in self.ACTIONS:
            return 400, {
                "ok": False,
                "error": f"invalid action {action!r}; use " + "|".join(self.ACTIONS),
            }
        if body.get("confirm") is not True:
            return 400, {
                "ok": False,
                "confirm_required": True,
                "action": name,
                "error": (
                    "crediting, withdrawing or allocating capital changes what the agents "
                    "may trade: send confirm=true to do it"
                ),
            }
        amount = body.get("amount")
        reason = " ".join(str(body.get("reason") or "").split())[:MAX_REASON_CHARS]
        raw_session = body.get("session_id")
        try:
            session_id = None if raw_session in (None, "") else int(raw_session)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": f"session_id must be an integer, got {raw_session!r}"}
        currency = body.get("currency") or None
        store = Memory(self.settings.memory.db)
        try:
            with self._lock:
                if name == "deposit":
                    out = capital_mod.deposit(
                        self.settings, amount, reason=reason, memory=store,
                        currency=currency, source="dashboard",
                    )
                elif name == "withdraw":
                    out = capital_mod.withdraw(
                        self.settings, amount, reason=reason, memory=store,
                        currency=currency, source="dashboard",
                    )
                elif name == "allocate":
                    out = capital_mod.allocate(
                        self.settings, amount, reason=reason, memory=store,
                        session_id=session_id, source="dashboard",
                    )
                else:
                    out = capital_mod.release(
                        self.settings, reason=reason, memory=store,
                        session_id=session_id, source="dashboard", amount=amount,
                    )
        except capital_mod.CapitalError as exc:
            return 400, {"ok": False, "action": name, "error": str(exc)}
        except (TypeError, ValueError) as exc:
            return 400, {"ok": False, "action": name, "error": f"bad {name} request: {exc}"}
        return 200, {
            "ok": True,
            "action": name,
            "by": "dashboard",
            "entry": out.get("entry"),
            "capital": out.get("capital"),
            "session_id": out.get("session_id"),
            "account": self.snapshot(),
        }


class RiskLimitsPanel:
    """Drawdown, today's P&L and exposure, measured against the **sealed** caps.

    A thin wrapper: the measurement is :func:`selftrade.risk_report.limits_report`, which
    is also what ``python -m selftrade risk limits`` prints and what the alert channel
    reads when it has to explain a halt.  One implementation, three readers - a page that
    re-derived the numbers could show a limit the gate enforces differently, and an
    operator reading fiction is worse off than one reading nothing at all.

    Read-only: one database read, no exchange, no rate budget, no writes.
    """

    def __init__(self, settings: Any):
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        """The four limits with the live account against them (see :mod:`selftrade.risk_report`).

        Plus ``pending``, added for this page only: the notices the alert channel still
        owes (see :meth:`outstanding`).  The panel stays read-only - it answers "has
        anybody been told yet?" without leaving the page, which is the question that
        follows every red row.
        """
        report = limits_report(self.settings)
        report["pending"] = self.outstanding(report)
        return report

    def outstanding(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """What the alert channel would send next: pure, it sends and writes nothing."""
        from .notify import outstanding

        return outstanding(self.settings, report)


class AlertsPanel:
    """What the alert channel has actually **sent**: when, what, which band, and why.

    The limit panel answers "how close is the stop?"; this answers the question that
    follows it at three in the morning - *was anybody told, and what exactly did they
    read?*  It reads the channel's own ledger, the file the deduplication is decided on,
    so it cannot show a message that was never delivered: only a send that succeeded is
    written there at all.  Read-only, like every other panel on this page.
    """

    def __init__(self, settings: Any):
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        """The history (newest first), the channel's readiness and the ledger's path."""
        from .notify import SENT_LOG_LIMIT, readiness, sent_log

        info = readiness(self.settings)
        items = sent_log(self.settings)
        return {
            "ok": True,
            "channel": info,
            "state_file": info["state_file"],
            "limit": SENT_LOG_LIMIT,
            "items": items,
            "sent": len(items),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


# --------------------------------------------------------------------------
# the bot panels
# --------------------------------------------------------------------------

def _overview(memory: Memory) -> dict[str, Any]:
    champion = memory.champion()
    stats = memory.stats()
    last = memory.last_run()
    return {
        "stats": stats,
        "champion": (
            {"family": champion["family"], "fitness": champion["fitness"], "description": _describe(champion["genome"])}
            if champion
            else None
        ),
        "last_run": {"id": last["id"], "outcome": last["outcome"], "note": last["note"]} if last else None,
    }


def _describe(genome: dict[str, Any]) -> str:
    from .genome import describe

    try:
        return describe(genome)
    except Exception:  # pragma: no cover
        return str(genome.get("family"))


#: How many equity points a requested backtest sends to the chart.  The window can be
#: 18 000 bars; ~700 points draw the same line at a fraction of the payload, and the
#: response always names the window it was drawn over.
BACKTEST_EQUITY_POINTS = 700


class BacktestRunner:
    """Run a backtest from the page: a real evaluation, and a read-only one.

    The Backtesting view must be able to answer "what would *this* strategy do on
    *this* window" instead of only re-reading what a past run left behind.  A run here
    means one evaluation of one genome through the same engine the CLI uses
    (:func:`evaluate.evaluate_folds` for the walk-forward window, ``holdout_eval`` for
    the untouched tail, ``backtest.simulate`` for the whole series).

    It **writes nothing**: no row, no lesson, no report, no order, no arm.  The last
    result is kept in memory so the page can re-read it without paying for the compute
    twice, and the loaded dataset is cached against the file's size+mtime so a refresh
    does not re-read 18 000 bars from disk.
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._frame: Any = None
        self._frame_key: tuple[Any, ...] | None = None
        #: The last answer, kept so a page that only redraws can re-read it for free.
        self.last: dict[str, Any] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- the dataset
    def _load(self) -> tuple[Any, dict[str, Any]]:
        """The dataframe, cached against the file it came from."""
        from .data import load_dataframe

        src = self.settings.data
        key = (src.source, str(getattr(src, "csv", "")), src.symbol, src.timeframe)
        stamp = None
        path = getattr(src, "csv", None)
        try:
            if path and Path(str(path)).is_file():
                st = Path(str(path)).stat()
                stamp = (int(st.st_size), int(st.st_mtime))
        except OSError:  # pragma: no cover - unreadable path
            stamp = None
        full_key = (*key, stamp)
        if self._frame is not None and self._frame_key == full_key:
            return self._frame, {"cached": True}
        df = load_dataframe(self.settings)
        self._frame, self._frame_key = df, full_key
        return df, {"cached": False}

    # ------------------------------------------------------------------ shapes
    @staticmethod
    def _downsample(values: list[float], limit: int) -> tuple[list[float], int]:
        """Every n-th value, always keeping the last one (the final equity matters)."""
        n = len(values)
        if n <= limit:
            return list(values), 1
        step = int(math.ceil(n / float(limit)))
        out = [values[i] for i in range(0, n, step)]
        if out[-1] != values[-1]:
            out.append(values[-1])
        return out, step

    def _window_frame(self, df: Any, window: str) -> tuple[Any, dict[str, Any]]:
        """Which slice a window means, named for the reader."""
        from .data import split_holdout

        holdout_frac = float(self.settings.evaluation.holdout_frac)
        if window == "full":
            return df, {
                "name": "full",
                "label": "the whole series (in-sample: the search also saw it)",
                "in_sample": True,
            }
        dev, hold = split_holdout(df, holdout_frac)
        if window == "holdout":
            return hold, {
                "name": "holdout",
                "label": "the untouched tail (out of sample)",
                "in_sample": False,
            }
        return dev, {
            "name": "folds",
            "label": "the development slice, walk-forward folds (the objective the search optimises)",
            "in_sample": False,
        }

    # ------------------------------------------------------------------- run
    @staticmethod
    def _costs(settings: Any) -> dict[str, Any]:
        bt = settings.backtest
        return {
            "fee_bps": float(bt.fee_bps),
            "slippage_bps": float(bt.slippage_bps),
            "round_trip_bps": (float(bt.fee_bps) + float(bt.slippage_bps)) * 2.0,
            "initial_cash": float(bt.initial_cash),
            "position_size": float(bt.position_size),
            "allow_short": bool(bt.allow_short),
        }

    def run(
        self,
        *,
        genome: dict[str, Any] | None = None,
        version: dict[str, Any] | None = None,
        run_row: dict[str, Any] | None = None,
        hall: dict[str, Any] | None = None,
        window: str = "folds",
    ) -> tuple[int, dict[str, Any]]:
        """Evaluate one genome and answer with the numbers, honestly labelled."""
        from .data import DataError
        from .genome import GenomeError, describe, genome_diff, genome_key, validate_genome

        requested: dict[str, Any] = {"window": window}
        source = genome
        if version is not None:
            requested["from"] = f"recorded version v{version.get('version')} (run #{version.get('run_id')}, id {version.get('id')})"
            source = version.get("genome")
        elif hall is not None:
            requested["from"] = f"hall of fame entry #{hall.get('id')} ({hall.get('family')}, fitness {hall.get('fitness')})"
            source = hall.get("genome")
        else:
            requested["from"] = "a genome sent by the page"
        if not isinstance(source, dict):
            return 400, {"ok": False, "error": "no genome to evaluate"}
        try:
            genome = validate_genome(source)
        except (GenomeError, TypeError, ValueError) as exc:
            return 400, {"ok": False, "error": f"that genome is not valid: {exc}"}

        try:
            df, load_info = self._load()
        except DataError as exc:
            return 502, {"ok": False, "error": f"the dataset could not be loaded: {exc}"}
        except OSError as exc:
            return 502, {"ok": False, "error": f"the dataset could not be read: {exc}"}

        started = time.perf_counter()
        try:
            slice_df, window_info = self._window_frame(df, window)
            if len(slice_df) < 50:
                return 400, {
                    "ok": False,
                    "error": f"the {window} window is too short ({len(slice_df)} bars)",
                }
            from .evaluate import evaluate_folds, holdout_eval

            holdout_metrics = None
            if window == "folds":
                rep = evaluate_folds(slice_df, genome, self.settings)
                folds = rep["folds"]
                fitness = rep["fitness"]
                n_trades, max_dd = rep["n_trades"], rep["max_drawdown"]
                passed, gate_reason = bool(rep["passed"]), rep["gate_reason"]
                equity = self._equity_of_folds(slice_df, genome)
            else:
                single = holdout_eval(slice_df, genome, self.settings)
                metrics = single["metrics"]
                holdout_metrics = metrics
                folds = []
                fitness = float(metrics.get(self.settings.evaluation.metric) or 0.0)
                n_trades = int(single["n_trades"])
                max_dd = float(metrics.get("max_drawdown") or 0.0)
                passed = None
                gate_reason = "not applied (the gate is a walk-forward verdict)"
                equity = self._equity_of(single)
        except Exception as exc:  # noqa: BLE001 - a panel reports, it never 500s
            return 500, {"ok": False, "error": f"the backtest failed: {exc}"}
        runtime_ms = (time.perf_counter() - started) * 1000.0

        values, step = self._downsample(list(equity or []), BACKTEST_EQUITY_POINTS)
        provenance = repro_fingerprint(self.settings, df=df)
        result: dict[str, Any] = {
            "ok": True,
            "requested": requested,
            "genome": genome,
            "description": describe(genome),
            "genome_hash": genome_key(genome),
            "metric": self.settings.evaluation.metric,
            "fitness": fitness,
            "folds": folds,
            "holdout_metrics": holdout_metrics,
            "n_trades": n_trades,
            "max_drawdown": max_dd,
            "passed": passed,
            "gate_reason": gate_reason,
            "window": {
                **window_info,
                "bars": int(len(slice_df)),
                "first": str(slice_df.index[0]),
                "last": str(slice_df.index[-1]),
                "symbol": self.settings.data.symbol,
                "timeframe": self.settings.data.timeframe,
                "source": self.settings.data.source,
            },
            "costs": self._costs(self.settings),
            "provenance": provenance,
            "equity": values,
            "equity_step": step,
            "runtime_ms": round(runtime_ms, 1),
            "dataset_cached": load_info.get("cached", False),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if version is not None:
            result["comparison"] = self._compare(
                version, run_row or {}, genome, fitness, provenance
            )
        with self._lock:
            self.last = result
        return 200, result

    @staticmethod
    def _equity_of(single: dict[str, Any]) -> list[float]:
        series = single.get("equity")
        if series is None:
            return []
        try:
            return [float(v) for v in list(series)]
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return []

    def _equity_of_folds(self, dev: Any, genome: dict[str, Any]) -> list[float]:
        """One equity line across the walk-forward folds, each fold rebased to the
        initial cash: a fold is evaluated on its own slice with its own context, so
        chaining raw equity from one slice into the next would draw a curve nobody ran.
        The engine and the cost model are the ones :func:`evaluate.evaluate_folds` uses.
        """
        import numpy as np

        from .backtest import simulate
        from .evaluate import make_folds

        bt = self.settings.backtest
        ev = self.settings.evaluation
        out: list[float] = []
        try:
            for fold in make_folds(dev, ev.folds, ev.train_frac):
                res = simulate(
                    fold["chunk"],
                    genome,
                    initial_cash=bt.initial_cash,
                    fee_bps=bt.fee_bps,
                    slippage_bps=bt.slippage_bps,
                    position_size=bt.position_size,
                    allow_short=bt.allow_short,
                    timeframe=self.settings.data.timeframe,
                )
                eq = np.asarray(res.equity, dtype=float)
                if eq.size and float(eq[0]) > 0:
                    out.extend((eq / float(eq[0]) * float(bt.initial_cash)).tolist())
        except Exception:  # noqa: BLE001 - the curve is a nicety, the metrics are not
            return []
        return out

    @staticmethod
    def _compare(
        version: dict[str, Any],
        run_row: dict[str, Any],
        genome: dict[str, Any],
        fitness: float,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        """Does this genome still produce the number the run recorded for it?

        This is the check the whole provenance idea exists for: the same genome over the
        same dataset must land on the stored fitness.  When it does not, the page says
        so and names the likeliest cause (a different dataset digest, or different code).
        """
        from .genome import genome_key

        stored = version.get("fitness")
        delta = None if stored is None else float(fitness) - float(stored)
        same_genome = version.get("genome_hash") == genome_key(genome)
        # The dataset and the id belong to the *run*, not to the version row.
        run_dataset = run_row.get("dataset_sha")
        current = provenance.get("dataset", {}).get("sha256")
        same_data = None if not run_dataset or not current else bool(run_dataset == current)
        matches = bool(delta is not None and abs(delta) < 1e-9 and same_genome)
        if matches:
            note = "the stored fitness reproduces exactly on this genome and dataset"
        elif delta is None:
            note = "no fitness was recorded for that version, so there is nothing to compare"
        elif not same_genome:
            note = "the genome differs from the one recorded for that version"
        elif same_data is False:
            note = "the dataset on disk differs from the one that produced that version: the numbers are expected to differ"
        else:
            note = "the stored fitness does not reproduce: the code changed since that run"
        return {
            "stored_fitness": stored,
            "delta": delta,
            "matches": matches,
            "same_genome": same_genome,
            "dataset_matches": same_data,
            "stored_dataset": run_dataset,
            "current_dataset": current,
            "stored_repro_id": run_row.get("repro_id"),
            "current_repro_id": provenance.get("repro_id"),
            "note": note,
        }


def _paper_block(memory: Memory) -> dict[str, Any]:
    session = memory.active_paper_session()
    if session is None:
        return {"session": None}
    trades = memory.paper_trades(session["id"])
    equity = memory.paper_equity(session["id"])
    initial = float(session["initial_cash"]) or 1.0
    # Equity is what the session is worth; `cash` alone would ignore an open position.
    final = float(equity[-1]["equity"]) if equity else float(session["cash"])
    # Where this capital came from: credited through the ledger, or the config default.
    # The page names it, so a funded session is never confused with an unfunded one.
    funded = float(memory.session_allocations().get(session["id"], 0.0))
    return {
        "session": {
            "session_id": session["id"],
            "name": session["name"],
            "status": session["status"],
            "window": session.get("window"),
            "cursor": session["cursor"],
            "total_bars": session["total_bars"],
            "n_trades": len([t for t in trades]),
            "initial_cash": session["initial_cash"],
            "funded_capital": funded,
            "capital_source": "ledger" if funded > 0 else "config",
            "cash": session["cash"],
            "equity": final,
            "total_return": final / initial - 1.0,
            "genome": session["genome"],
            # The cost model the session was run with (config-derived): the backtesting
            # view shows it next to the measured metrics, because a return without the
            # fees it was charged is not a return worth reading.
            "settings": session.get("settings") or {},
        }
    }


# --------------------------------------------------------------------------
# the lab, the logs and the settings: read-only views over what was recorded
# --------------------------------------------------------------------------

def _read_json_file(path: Any) -> Any:
    """Read a JSON file the program itself wrote, or ``None``.

    The path comes from a run's own ``summary`` row, so this only ever reads what
    :func:`selftrade.agent.write_report` produced - but it is still a path out of a
    database, so it is bounded (it must be a file, under ``MAX_REPORT_BYTES``) and
    every failure is ``None`` rather than an exception: a missing report is a fact the
    panel renders, not an error.
    """
    if not path:
        return None
    try:
        p = Path(str(path))
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file() or p.stat().st_size > MAX_REPORT_BYTES:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail_lines(path: Path, limit: int) -> tuple[list[str], bool]:
    """The last ``limit`` lines of a text file, and whether older ones were dropped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    lines = text.splitlines()
    return (lines[-limit:], len(lines) > limit)


def _count_rows(csv_path: str) -> int | None:
    """Line count of a CSV (minus its header), or ``None`` when it cannot be read.

    Cheap enough for a panel that refreshes every ~15 s; the file the project ships is
    a couple of MB.  Anything unreadable answers ``None`` rather than 0, so "no file"
    and "an empty file" stay different answers.
    """
    try:
        p = Path(csv_path)
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            n = sum(1 for _ in fh)
        return max(0, n - 1)
    except OSError:
        return None


def _genome_param_diff(genome: Any, base: Any) -> list[dict[str, Any]] | None:
    """Parameters that differ between a candidate and a baseline, for display.

    The diff itself is :func:`selftrade.genome.genome_diff` - the same primitive the
    search writes into ``strategy_versions`` - so a candidate derived from a recorded
    version and the version's own stored diff can never disagree.  A missing baseline
    answers ``None`` (the page shows N/D) rather than an empty list, which would read
    as "nothing changed" when the truth is "nothing to compare against".
    """
    if not isinstance(genome, dict) or not isinstance(base, dict):
        return None
    try:
        from .genome import genome_diff, validate_genome

        validate_genome(genome)
        validate_genome(base)
    except Exception:  # noqa: BLE001 - a diff must never break the panel
        return None
    return genome_diff(base, genome)


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    """A run row flattened for the run selector (all of it from the stored summary)."""
    s = run.get("summary") or {}
    return {
        "id": run.get("id"),
        "ts": run.get("ts"),
        "note": run.get("note"),
        "outcome": run.get("outcome"),
        "generations": s.get("generations"),
        "candidates": s.get("candidates"),
        "passed": s.get("passed"),
        "champion": s.get("champion"),
        "champion_fitness": s.get("champion_fitness"),
        "metric": s.get("metric"),
        "stopped": s.get("stopped"),
        "report_available": bool(s.get("report_json")),
        # Provenance, from the run row itself: the summary is the fallback for a run
        # recorded before the columns existed (which reads N/D, not a guess).
        "repro_id": run.get("repro_id") or s.get("repro_id"),
        "commit": run.get("code_version") or s.get("commit"),
        "commit_sha": run.get("commit_sha"),
        "dataset": run.get("dataset_sha") or s.get("dataset"),
        "reproducible": (
            run.get("reproducible")
            if run.get("reproducible") is not None
            else s.get("reproducible")
        ),
    }


def _repro_block(
    run: dict[str, Any] | None, report: Any, versions: list[dict[str, Any]]
) -> dict[str, Any]:
    """The provenance of one run, preferring the run row and falling back to the report.

    Either half can come from either source, which is what lets a run recorded before
    this feature (report on disk, no columns) still show its commit and dataset, and a
    run whose report was deleted still show what the database kept.
    """
    run = run or {}
    rep = (report or {}).get("repro") or {}
    ds = rep.get("dataset") or {}
    commit_sha = run.get("commit_sha") or rep.get("commit")
    dataset_sha = run.get("dataset_sha") or ds.get("sha256")
    rid = run.get("repro_id") or rep.get("repro_id")
    reproducible = run.get("reproducible")
    if reproducible is None:
        reproducible = rep.get("reproducible")
    return {
        "repro_id": rid,
        "reproducible": reproducible,
        "commit": commit_sha,
        "commit_short": (run.get("code_version") or rep.get("commit_short") or (commit_sha or "")[:12] or None),
        "branch": rep.get("branch"),
        "dirty": rep.get("dirty"),
        "dataset": dataset_sha,
        "dataset_short": (dataset_sha or "")[:12] or ds.get("short"),
        "dataset_rows": run.get("dataset_rows") or ds.get("rows"),
        "dataset_path": ds.get("path"),
        "dataset_method": ds.get("method"),
        "notes": rep.get("notes") or [],
        "versions_recorded": len(versions),
    }


def _version_rows(versions: list[dict[str, Any]], limit: int = 300) -> tuple[list[dict[str, Any]], bool]:
    """The recorded lineage, each row carrying its stored diff (never recomputed).

    Returns the rows and whether they were truncated: a run with more versions than the
    page renders says so instead of silently dropping the tail.
    """
    truncated = len(versions) > limit
    rows: list[dict[str, Any]] = []
    for v in versions[:limit]:
        parent = v.get("parent")
        rows.append(
            {
                "id": v.get("id"),
                "run_id": v.get("run_id"),
                "version": v.get("version"),
                "generation": v.get("generation"),
                "family": v.get("family"),
                "parent_family": v.get("parent_family"),
                "origin": v.get("origin"),
                "outcome": v.get("outcome"),
                "passed": v.get("passed"),
                "fitness": v.get("fitness"),
                "parent_fitness": v.get("parent_fitness"),
                "delta_fitness": v.get("delta_fitness"),
                "reason": v.get("reason"),
                "rationale": v.get("rationale"),
                "genome_hash": v.get("genome_hash"),
                "parent_hash": v.get("parent_hash"),
                "diff": v.get("diff") or [],
                "changed": len(v.get("diff") or []),
                # A flat, searchable rendering of the diff, so "which version touched
                # bb_std" is a search rather than a scan.  Built from the stored diff.
                "diff_keys": " ".join(
                    f"{d.get('key')} {d.get('from')} {d.get('to')}"
                    for d in (v.get("diff") or [])
                    if isinstance(d, dict)
                ),
                "has_parent": parent is not None,
                "genome": v.get("genome"),
                "parent_genome": parent,
                "created_at": v.get("created_at"),
            }
        )
    return rows, truncated


def _experiments(report: Any, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-candidate rows for the AI Lab, richest source first.

    A run report carries more per candidate than the database does (the hypothesis, the
    trade count, the drawdown, complexity); when it is missing the stored candidate rows
    are used instead, with the missing fields left ``None`` so the page shows N/D.  The
    "baseline" is the first candidate of the same family in the run - a real, derivable
    reference, labelled as such rather than pretending to a ground truth nobody recorded.
    """
    out: list[dict[str, Any]] = []
    if isinstance(report, dict) and report.get("generations"):
        try:
            from .genome import genome_key

            champ_key = genome_key(report["champion"]) if report.get("champion") else None
        except Exception:  # noqa: BLE001
            champ_key = None
        base_genome: dict[str, Any] = {}
        base_fit: dict[str, Any] = {}
        for gen in report.get("generations") or []:
            g = gen.get("generation")
            for c in gen.get("candidates") or []:
                fam = c.get("family")
                if fam not in base_genome:
                    base_genome[fam] = c.get("genome")
                    base_fit[fam] = c.get("fitness")
                fit, ref = c.get("fitness"), base_fit.get(fam)
                key = None
                try:
                    key = genome_key(c.get("genome")) if c.get("genome") else None
                except Exception:  # noqa: BLE001
                    key = None
                out.append({
                    "key": f"r{report.get('run_id')}:g{g}:{len(out)}",
                    "id": None,
                    "generation": g,
                    "family": fam,
                    "origin": c.get("origin"),
                    "fitness": fit,
                    "delta": (float(fit) - float(ref)) if fit is not None and ref is not None else None,
                    "passed": bool(c.get("passed")),
                    "reason": c.get("reason"),
                    "rationale": c.get("rationale"),
                    "n_trades": c.get("n_trades"),
                    "max_drawdown": c.get("max_drawdown"),
                    "metric_mean": c.get("metric_mean"),
                    "metric_std": c.get("metric_std"),
                    "complexity": c.get("complexity"),
                    "params_changed": _genome_param_diff(c.get("genome"), base_genome.get(fam)),
                    "is_champion": bool(key and champ_key and key == champ_key),
                })
        if out:
            return out
    base_genome = {}
    base_fit = {}
    for i, c in enumerate(candidates):
        fam = c.get("family")
        if fam not in base_genome:
            base_genome[fam] = c.get("genome")
            base_fit[fam] = c.get("fitness")
        fit, ref = c.get("fitness"), base_fit.get(fam)
        out.append({
            "key": f"c{c.get('id') if c.get('id') is not None else i}",
            "id": c.get("id"),
            "generation": c.get("generation"),
            "family": fam,
            "origin": c.get("origin"),
            "fitness": fit,
            "delta": (float(fit) - float(ref)) if fit is not None and ref is not None else None,
            "passed": bool(c.get("passed")),
            "reason": c.get("reason"),
            "rationale": None,
            "n_trades": None,
            "max_drawdown": None,
            "metric_mean": None,
            "metric_std": None,
            "complexity": None,
            "params_changed": _genome_param_diff(c.get("genome"), base_genome.get(fam)),
            "is_champion": False,
        })
    return out


def _cycle_payload(
    settings: Any, memory: Memory, run: dict[str, Any] | None, report: Any,
    candidates: list[dict[str, Any]], paper: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The Collect → … → Learn cycle, each stage from recorded evidence only.

    ``status`` is one of ``ok`` / ``active`` / ``warn`` / ``bad`` / ``unavailable``; a
    stage with nothing behind it says ``unavailable`` (the page renders it muted with
    N/D numbers) instead of inventing progress.
    """
    stages: list[dict[str, Any]] = []
    summary = (run or {}).get("summary") or {}
    report = report if isinstance(report, dict) else {}

    rows = _count_rows(str(getattr(settings.data, "csv", "")))
    stages.append({
        "key": "collect", "ord": "1", "label": "Collect",
        "status": "ok" if rows else "unavailable",
        "detail": [
            {"label": "source", "value": f"{settings.data.source} {settings.data.timeframe}"},
            {"label": "rows", "value": str(rows) if rows else "N/D"},
            {"label": "symbol", "value": str(settings.data.symbol)},
        ],
        "evidence": (str(getattr(settings.data, "csv", "")) or "no data file configured"),
    })

    origins: dict[str, int] = {}
    for c in candidates:
        name = c.get("origin") or "?"
        origins[name] = origins.get(name, 0) + 1
    mix = report.get("proposals") or origins
    has_mix = any((mix.get(k) or 0) for k in ("llm", "deterministic", "seed"))
    stages.append({
        "key": "hypothesize", "ord": "2", "label": "Hypothesize",
        "status": "ok" if has_mix else "unavailable",
        "detail": [
            {"label": "llm", "value": str(mix.get("llm", 0))},
            {"label": "deterministic", "value": str(mix.get("deterministic", 0))},
            {"label": "seed", "value": str(mix.get("seed", 0))},
        ],
        "evidence": "Ollama " + ("on" if report.get("llm_available") else "off") + ", bounded genome changes only",
    })

    fits = [c.get("fitness") for c in candidates if c.get("fitness") is not None]
    best = max(fits) if fits else summary.get("champion_fitness")
    n_cand = len(candidates) or summary.get("candidates")
    stages.append({
        "key": "backtest", "ord": "3", "label": "Backtest",
        "status": "ok" if n_cand else "unavailable",
        "detail": [
            {"label": "candidates", "value": str(n_cand or "N/D")},
            {"label": "best fitness", "value": f"{best:.3f}" if best is not None else "N/D"},
            {"label": "metric", "value": str(report.get("metric") or summary.get("metric") or "N/D")},
        ],
        "evidence": "walk-forward folds on the development series",
    })

    kept = sum(1 for c in candidates if c.get("passed")) or summary.get("passed")
    critic = report.get("critic") or {}
    hold = report.get("holdout") or {}
    if critic and not critic.get("approved"):
        vstat = "warn"
    elif kept:
        vstat = "ok"
    else:
        vstat = "unavailable"
    stages.append({
        "key": "validate", "ord": "4", "label": "Validate",
        "status": vstat,
        "detail": [
            {"label": "passed gate", "value": str(kept if kept is not None else "N/D")},
            {"label": "holdout", "value": (f"{hold.get('metric_value'):.3f}" if hold.get("metric_value") is not None else "N/D")},
            {"label": "critic", "value": ("approved" if critic.get("approved") else ("refused" if critic else "N/D"))},
        ],
        "evidence": "holdout read once + the sealed deployment gate",
    })

    p = (paper or {}).get("session") or {}
    stages.append({
        "key": "paper", "ord": "5", "label": "Paper trade",
        "status": ("active" if p.get("status") == "active" else ("ok" if p else "unavailable")),
        "detail": [
            {"label": "session", "value": ("#" + str(p.get("session_id"))) if p else "N/D"},
            {"label": "trades", "value": str(p.get("n_trades")) if p else "N/D"},
            {"label": "return", "value": (f"{p.get('total_return', 0) * 100:+.2f}%") if p else "N/D"},
        ],
        "evidence": "orders through the risk gate on the simulated rail",
    })

    try:
        n_lessons = int(memory.stats().get("lessons") or 0)
    except Exception:  # noqa: BLE001
        n_lessons = 0
    stages.append({
        "key": "learn", "ord": "6", "label": "Learn",
        "status": "ok" if n_lessons else "unavailable",
        "detail": [
            {"label": "lessons", "value": str(n_lessons)},
            {"label": "last run", "value": ("#" + str(run.get("id"))) if run else "N/D"},
        ],
        "evidence": "each run stores a lesson per generation (autopsy, critic, journal)",
    })
    return stages


def _lab_payload(settings: Any, memory: Memory, run_id: int | None = None) -> dict[str, Any]:
    """The AI Lab: the last improvement cycle and the experiments behind it.

    Built from the run history plus the run report the program wrote (when it is still
    on disk); nothing is recomputed into a different story than the CLI's ``report``.
    """
    try:
        runs = memory.history(100)
        selected = int(run_id) if run_id else (runs[0]["id"] if runs else None)
        run = memory.get_run(selected) if selected else None
        candidates = memory.candidates_for_run(selected) if selected else []
        paper = _paper_block(memory)
        version_rows, versions_truncated = _version_rows(
            memory.strategy_versions(selected) if selected else []
        )
    except Exception as exc:  # noqa: BLE001 - a panel reports, never raises
        return {"ok": False, "error": f"the lab could not be read: {exc}", "runs": [], "selected": None}

    summary = (run or {}).get("summary") or {}
    report = _read_json_file(summary.get("report_json"))
    champ = (report or {}).get("champion")
    champion = None
    if isinstance(champ, dict):
        champion = {"family": champ.get("family"), "fitness": (report or {}).get("champion_fitness"), "genome": champ}
    elif summary.get("champion"):
        champion = {"family": summary.get("champion"), "fitness": summary.get("champion_fitness"), "genome": None}
    return {
        "ok": True,
        "runs": [_run_summary(r) for r in runs],
        "selected": selected,
        "report_available": bool(report),
        "metric": (report or {}).get("metric") or summary.get("metric"),
        "llm_available": bool((report or {}).get("llm_available", summary.get("llm_available"))),
        "llm_enabled": (report or {}).get("llm_enabled"),
        "proposals": (report or {}).get("proposals") or summary.get("proposals"),
        "notes": (report or {}).get("notes") or [],
        "regime_luck_warning": (report or {}).get("regime_luck_warning"),
        "champion": champion,
        "holdout": (report or {}).get("holdout"),
        "critic": (report or {}).get("critic"),
        "autopsy": _autopsy_summary((report or {}).get("autopsy")),
        "generations": [
            {"generation": g.get("generation"), "best_fitness": g.get("best_fitness"), "kept": g.get("kept"), "lesson": g.get("lesson")}
            for g in ((report or {}).get("generations") or [])
        ],
        "cycle": _cycle_payload(settings, memory, run, report, candidates, paper),
        "experiments": _experiments(report, candidates),
        "versions": version_rows,
        "versions_truncated": versions_truncated,
        "repro": _repro_block(run, report, version_rows),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _autopsy_summary(autopsy: Any) -> dict[str, Any] | None:
    """Trim an autopsy report to what the lab card renders (it stays a real report)."""
    if not isinstance(autopsy, dict):
        return None
    return {
        "summary": autopsy.get("summary"),
        "n_losers": autopsy.get("n_losers"),
        "loser_share": autopsy.get("loser_share"),
        "worst_total": autopsy.get("worst_total"),
        "worst": autopsy.get("worst") or [],
        "modes": autopsy.get("modes") or [],
        "overfit": autopsy.get("overfit"),
        "window": autopsy.get("window"),
    }


def _logs_payload(settings: Any, memory: Memory, limit: int = 200) -> dict[str, Any]:
    """Risk events, provider calls and the tail of the tracked loop's log."""
    try:
        events = memory.risk_events(limit=limit)
        calls = memory.api_calls(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"logs could not be read: {exc}", "risk_events": [], "api_calls": []}
    log_path = loop_log_path(settings)
    tail, truncated = _tail_lines(log_path, LOOP_LOG_TAIL)
    return {
        "ok": True,
        "risk_events": events,
        "api_calls": calls,
        "loop_log": {"path": str(log_path), "exists": log_path.exists(), "tail": tail, "truncated": truncated},
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _settings_payload(settings: Any) -> dict[str, Any]:
    """A read-only view of the configuration - **names**, never secrets.

    The environment variable *names* are shown because an operator needs to know which
    one to set; the values are never read here, so they cannot be leaked by this page.
    """
    digest = digest_of(LIMITS)
    d = settings.dashboard
    return {
        "ok": True,
        "project": getattr(settings, "project", "selftrade"),
        "version": __version__,
        "config_path": str(getattr(settings, "config_path", None) or "config.yaml"),
        "paths": {
            "memory db": settings.memory.db,
            "session file": getattr(settings, "session_file", "state/session.json"),
            "loop log": str(loop_log_path(settings)),
            "reports dir": "reports",
        },
        "data": {
            "source": settings.data.source, "csv": settings.data.csv,
            "symbol": settings.data.symbol, "timeframe": settings.data.timeframe,
            "fetch base url": settings.data.fetch_base_url,
        },
        "backtest": {
            "initial cash": settings.backtest.initial_cash, "fee bps": settings.backtest.fee_bps,
            "slippage bps": settings.backtest.slippage_bps, "position size": settings.backtest.position_size,
            "allow short": settings.backtest.allow_short,
        },
        "evaluation": {
            "metric": settings.evaluation.metric, "folds": settings.evaluation.folds,
            "train frac": settings.evaluation.train_frac, "holdout frac": settings.evaluation.holdout_frac,
            "min trades": settings.evaluation.min_trades, "max drawdown": settings.evaluation.max_drawdown,
        },
        "evolution": {
            "generations": settings.evolution.generations, "population": settings.evolution.population,
            "elite": settings.evolution.elite, "seed": settings.evolution.seed,
            "mode": settings.evolution.mode, "mutation rate": settings.evolution.mutation_rate,
        },
        "paper": {"window": settings.paper.window, "window bars": settings.paper.window_bars, "step bars": settings.paper.step_bars},
        # The simulated capital account's policy: shown like every other section, so the
        # page never hides the caps a credit is checked against (`docs/CAPITAL.md`).
        "funding": {
            "enabled": settings.funding.enabled, "currency": settings.funding.currency,
            "max deposit": settings.funding.max_deposit,
            "max total capital": settings.funding.max_total_capital,
            "allow withdrawal": settings.funding.allow_withdrawal,
        },
        # The key is the *section name* the page looks up (`s.risk_gate`): with a space in
        # it the Settings page rendered `N/D` for the pre-trade gate, which is exactly the
        # kind of silent hole this page exists to avoid.
        "risk_gate": {
            "enabled": settings.risk_gate.enabled, "risk pct": settings.risk_gate.risk_pct,
            "max positions": settings.risk_gate.max_positions, "max leverage": settings.risk_gate.max_leverage,
            "require stop": settings.risk_gate.require_stop,
            "min seconds between orders": settings.risk_gate.min_seconds_between_orders,
        },
        "dashboard": {
            "host": d.host, "port": d.port, "auth": d.auth, "password env": d.password_env,
            "password file": d.password_file,
            "auto refresh s": d.auto_refresh_s, "market symbol": d.market_symbol,
            "market interval": d.market_interval, "key refresh s": d.key_refresh_s,
        },
        "llm": {"enabled": settings.llm.enabled, "base url": settings.llm.base_url, "model": settings.llm.model},
        "notify": {
            "enabled": settings.notify.enabled, "token env": settings.notify.telegram_token_env,
            "chat id env": settings.notify.telegram_chat_id_env, "warn below frac": settings.notify.warn_below_frac,
        },
        "binance": {
            "enabled": settings.binance.enabled, "live": settings.binance.live,
            "allow production": settings.binance.allow_production, "base url": settings.binance.base_url,
            "api key env": settings.binance.api_key_env, "api secret env": settings.binance.api_secret_env,
            "symbol": settings.binance.symbol,
        },
        "mt5": {
            "enabled": settings.mt5.enabled, "live": settings.mt5.live,
            "require demo": settings.mt5.require_demo, "require stop": settings.mt5.require_stop,
            "symbol": settings.mt5.symbol, "password env": settings.mt5.password_env,
        },
        "rea": {"enabled": settings.rea.enabled, "dry run": settings.rea.dry_run, "provider": settings.rea.provider, "targets": len(settings.rea.targets or [])},
        "sealed": {
            "limits": LIMITS.as_dict(),
            "digest": digest,
            "pinned": digest == SEALED_DIGEST,
            "sealed": is_sealed(LIMITS),
            "verification": _seal_verification(settings),
        },
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _seal_verification(settings: Any) -> dict[str, Any]:
    """Verify the sealed limits without touching them (the page's own check).

    Pure read on purpose: the CLI's ``risk verify`` *writes* the seal on first use, and a
    GET that creates a file would be a write path hiding in a read.  This one compares
    three things and reports each: the digest of the shipped values against the pin, the
    same digest against the seal file already on disk, and whether the model-side levers
    are closed.
    """
    from .risk_rules import SEAL_FILENAME, assert_no_levers, seal_path

    digest = digest_of(LIMITS)
    pinned = bool(SEALED_DIGEST) and digest == SEALED_DIGEST
    path = seal_path(settings)
    on_disk: dict[str, Any] = {"exists": False, "digest": None, "sealed_at": None}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            on_disk = {"exists": True, "digest": None, "sealed_at": None, "error": str(exc)}
        else:
            on_disk = {
                "exists": True,
                "digest": payload.get("digest"),
                "sealed_at": payload.get("sealed_at"),
            }
    matches_disk = (
        None if not on_disk.get("digest") else bool(on_disk["digest"] == digest)
    )
    levers_closed = True
    levers_error = None
    try:
        assert_no_levers(LIMITS)
    except Exception as exc:  # noqa: BLE001 - the check itself must not 500 the page
        levers_closed, levers_error = False, str(exc)
    ok = bool(pinned and levers_closed and matches_disk is not False)
    return {
        "ok": ok,
        "digest": digest,
        "pin": SEALED_DIGEST,
        "pinned": pinned,
        "path": str(path),
        "file": SEAL_FILENAME,
        "on_disk": on_disk,
        "matches_disk": matches_disk,
        "levers_closed": levers_closed,
        "levers_error": levers_error,
        "note": (
            "the shipped limits match the pin and nothing on the model side can move them"
            if ok
            else "the sealed limits do not verify: do not deploy until this is explained"
        ),
    }


def api_get(
    settings: Any,
    path: str,
    query: dict[str, list[str]] | None = None,
    feed: "MarketFeed | None" = None,
    keys: "KeyHealth | None" = None,
    switch: "KillSwitchPanel | None" = None,
    risk: "RiskLimitsPanel | None" = None,
    alerts: "AlertsPanel | None" = None,
    runner: "LoopRunner | None" = None,
    backtest: "BacktestRunner | None" = None,
    capital: "CapitalPanel | None" = None,
    auth: "Auth | None" = None,
    keys_panel: "KeysPanel | None" = None,
) -> tuple[int, Any]:
    memory = Memory(settings.memory.db)
    if path == "/api/health":
        try:
            Memory(settings.memory.db).stats()
            db_ok = True
        except Exception:  # pragma: no cover
            db_ok = False
        return 200, {"status": "ok", "version": __version__, "db": settings.memory.db, "db_ok": db_ok}
    if path == "/api/overview":
        return 200, _overview(memory)
    if path == "/api/equity":
        session = memory.active_paper_session()
        pts = memory.paper_equity(session["id"]) if session else []
        return 200, {"session_id": session["id"] if session else None, "points": pts}
    if path == "/api/trades":
        session = memory.active_paper_session()
        items = memory.paper_trades(session["id"]) if session else []
        return 200, {"items": items}
    if path == "/api/paper":
        return 200, _paper_block(memory)
    if path == "/api/experiments":
        return 200, {"items": memory.recent_candidates(100)}
    if path == "/api/hall":
        return 200, {"items": memory.hall_of_fame(50)}
    if path == "/api/lessons":
        return 200, {"items": memory.lessons(limit=100)}
    if path == "/api/history":
        return 200, {"items": memory.history(50)}
    if path == "/api/session":
        # The session file says what the loop was *asked* to do; the runner says whether
        # anything is actually there to do it.  A page that showed only the file would
        # call a crashed run "running", so the pid is part of the answer.
        state = Session(settings.session_file).status()
        loop = (runner or LoopRunner(settings)).status()
        return 200, {**state, "loop": loop}
    if path == "/api/market":
        q = query or {}
        symbol = (q.get("symbol") or [settings.dashboard.market_symbol])[0]
        interval = (q.get("interval") or [settings.dashboard.market_interval])[0]
        try:
            bars = int((q.get("bars") or [settings.dashboard.market_bars])[0])
        except (TypeError, ValueError):
            bars = int(settings.dashboard.market_bars)
        bars = max(10, min(1000, bars))
        handler = feed if feed is not None else MarketFeed(settings)
        return 200, handler.snapshot(symbol, interval, bars)
    if path == "/api/binance":
        # Read-only, and the only knob is a forced re-probe (the "recheck" button).
        q = query or {}
        force = str((q.get("force") or [""])[0]).lower() in ("1", "true", "yes")
        handler = keys if keys is not None else KeyHealth(settings)
        return 200, handler.snapshot(force=force)
    if path == "/api/keys":
        # What credentials this machine has, where each comes from, and nothing else: no
        # endpoint ever returns a value.  The names are the rails' own, from the config.
        panel = keys_panel if keys_panel is not None else KeysPanel(settings)
        return 200, panel.snapshot()
    if path == "/api/killswitch":
        panel = switch if switch is not None else KillSwitchPanel(settings)
        return 200, panel.snapshot()
    if path == "/api/risk":
        limits = risk if risk is not None else RiskLimitsPanel(settings)
        return 200, limits.snapshot()
    if path == "/api/alerts":
        panel = alerts if alerts is not None else AlertsPanel(settings)
        return 200, panel.snapshot()
    if path == "/api/capital":
        # The account the agents trade: the free balance, what was credited, what is
        # allocated to the active session, and the append-only ledger.  A read - the
        # movements are POSTs, and none of them touches an exchange.
        panel = capital if capital is not None else CapitalPanel(settings)
        try:
            limit = int((query or {}).get("limit", ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        return 200, panel.snapshot(limit=max(5, min(500, limit)))
    if path == "/api/lab":
        q = query or {}
        raw = (q.get("run_id") or [None])[0]
        run_id = None
        if raw not in (None, "", "latest"):
            try:
                run_id = int(raw)
            except (TypeError, ValueError):
                run_id = None
        return 200, _lab_payload(settings, memory, run_id)
    if path == "/api/logs":
        try:
            limit = int((query or {}).get("limit", ["200"])[0])
        except (TypeError, ValueError):
            limit = 200
        return 200, _logs_payload(settings, memory, max(10, min(1000, limit)))
    if path == "/api/settings":
        payload = _settings_payload(settings)
        # The one secret in the project: the page gets *where* the password comes from and
        # the policy around it - the file name, the algorithm, the iteration count - and
        # never the hash, the salt or the password (see `selftrade.credentials`).
        payload["password"] = auth.describe() if auth is not None else {
            "source": "unknown", "changeable": False, "is_default": None,
            "min_length": DEFAULT_MIN_LENGTH,
            "note": "this server was built without a password store",
        }
        return 200, payload
    if path == "/api/backtest":
        # A GET that computes and writes nothing: no row, no report, no order, no arm.
        # It is the one endpoint here that costs real CPU, so the caller must say what
        # it wants evaluated - a recorded version, a hall entry, or an explicit genome.
        runner_bt = backtest if backtest is not None else BacktestRunner(settings)
        q = query or {}
        window = str((q.get("window") or ["folds"])[0]).lower()
        if window not in ("folds", "holdout", "full"):
            return 400, {"ok": False, "error": f"unknown window {window!r}; use folds|holdout|full"}
        raw_version = (q.get("version_id") or [""])[0]
        raw_hall = (q.get("hall_id") or [""])[0]
        raw_genome = (q.get("genome") or [""])[0]
        try:
            if raw_version not in ("", None):
                version_id = int(raw_version)
                rows = [v for v in memory.latest_strategy_versions(500) if v["id"] == version_id]
                if not rows:
                    return 404, {"ok": False, "error": f"no recorded version with id {version_id}"}
                version = rows[0]
                run_row = memory.get_run(version.get("run_id")) if version.get("run_id") else None
                return runner_bt.run(version=version, run_row=run_row, window=window)
            if raw_hall not in ("", None):
                hall_id = int(raw_hall)
                entry = next((h for h in memory.hall_of_fame(200) if h["id"] == hall_id), None)
                if entry is None:
                    return 404, {"ok": False, "error": f"no hall entry with id {hall_id}"}
                return runner_bt.run(hall=entry, window=window)
            if raw_genome not in ("", None):
                parsed = json.loads(raw_genome)
                return runner_bt.run(genome=parsed, window=window)
        except (TypeError, ValueError) as exc:
            return 400, {"ok": False, "error": f"bad backtest request: {exc}"}
        return 400, {
            "ok": False,
            "error": "say what to evaluate: version_id=N, hall_id=N or genome=<json>",
        }
    return 404, {"error": "not found", "path": path}


def api_control(
    settings: Any, body: dict[str, Any], runner: "LoopRunner | None" = None
) -> tuple[int, Any]:
    """The loop controls, wired to a process instead of to a wish.

    ``start`` runs the tracked loop and hands back its pid, its log and the count of
    generations it was asked for; ``pause``/``resume``/``stop`` are refused with 409 when
    no loop is running - see :mod:`selftrade.loop` for why that refusal is the feature.
    """
    action = (body or {}).get("action")
    if action not in ("start", "pause", "resume", "stop"):
        return 400, {"error": f"invalid action {action!r}; use start|pause|resume|stop"}
    loop = runner if runner is not None else LoopRunner(settings)
    if action == "start":
        code, obj = loop.start(body.get("generations") or settings.evolution.generations)
    else:
        code, obj = loop.request(action)
    # ``session`` is kept in the answer whatever the outcome: it is what the loop was
    # asked to do, and it is the first thing worth reading when something looks stuck.
    if isinstance(obj, dict):
        obj.setdefault("session", Session(settings.session_file).read())
    return code, obj


# --------------------------------------------------------------------------
# the HTTP handler
# --------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    settings: Any = None
    auth: Auth = None
    feed: MarketFeed = None
    keys: KeyHealth = None
    switch: KillSwitchPanel = None
    risk: RiskLimitsPanel = None
    alerts: AlertsPanel = None
    capital: CapitalPanel = None
    keys_panel: KeysPanel = None
    runner: LoopRunner = None
    backtest: BacktestRunner = None
    server_version = "selftrade-dashboard"

    def log_message(self, *args):  # keep the console quiet
        pass

    # -- helpers ---------------------------------------------------------
    #: Set once a response has been written, so the failure guard never writes a second one
    #: on a socket that has already started answering.
    _responded: bool = False

    def _send(self, code: int, obj: Any, content_type: str = "application/json",
              headers: dict[str, str] | None = None) -> None:
        self._responded = True
        if isinstance(obj, str):
            payload = obj.encode("utf-8")
        else:
            payload = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _cookie(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:  # pragma: no cover - malformed header
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _authed(self) -> bool:
        if not self.auth.enabled:
            return True
        return self.auth.check(self._cookie())

    def _client(self) -> str:
        return str(self.client_address[0]) if self.client_address else "?"

    def _failed(self, exc: Exception) -> None:
        """Answer an unexpected failure as JSON, once.

        Without this a broken store (a database file that is not a database, a state file
        owned by another user) turns every panel into a dropped connection: the browser
        reports a network error, the page keeps its loading state and the server prints a
        traceback nobody reads.  The panels poll on timers, so it repeats forever.  The
        detail goes to an *authenticated* operator because every one of these endpoints is
        behind the login, and it names the command that reports the same thing on a console.
        """
        if self._responded:
            return
        self._send(503, {
            "error": "the backend could not answer",
            "detail": f"{type(exc).__name__}: {exc}",
            "hint": ("this dashboard reads the same store the CLI does; `python -m selftrade "
                     "doctor` reports what it can and cannot open"),
        })

    def _api(self, call: Any) -> None:
        """Run one API call, answering a failure instead of letting it close the socket."""
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - one panel must not take the server down
            self._failed(exc)

    def _same_origin(self) -> bool:
        """Reject a POST whose Origin is not our own host (belt to SameSite's braces)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin fetches may omit Origin
        host = (self.headers.get("Host") or "").lower()
        return origin.split("//", 1)[-1].lower() == host

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, self._page(), "text/html")
            return

        if path == "/api/whoami":
            self._send(200, {
                "authenticated": self._authed(),
                "auth_enabled": bool(self.auth.enabled),
                "version": __version__,
            })
            return
        if path.startswith("/api/") and not self._authed():
            self._send(401, {"error": "unauthorized"})
            return

        def respond() -> None:
            code, obj = api_get(
                self.settings, path, query, self.feed, self.keys, self.switch, self.risk,
                self.alerts, self.runner, self.backtest, self.capital, self.auth,
                self.keys_panel,
            )
            self._send(code, obj)

        self._api(respond)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON body"})
            return
        body = body if isinstance(body, dict) else {}

        if not self._same_origin():
            self._send(403, {"error": "cross-origin request refused"})
            return

        if path == "/api/login":
            self._login(body)
            return
        if path == "/api/logout":
            self._send(200, {"ok": True}, headers={
                "Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            })
            return

        if path.startswith("/api/") and not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        if path == "/api/control":
            def control() -> None:
                code, obj = api_control(self.settings, body, self.runner)
                self._send(code, obj)

            self._api(control)
            return
        if path == "/api/killswitch":
            self._api(lambda: self._killswitch(body))
            return
        if path == "/api/password":
            self._api(lambda: self._password(body))
            return
        if path == "/api/capital":
            self._api(lambda: self._capital(body))
            return
        if path == "/api/keys":
            self._api(lambda: self._keys(body))
            return
        self._send(404, {"error": "not found", "path": path})

    def _keys(self, body: dict[str, Any]) -> None:
        """Store or forget one rail credential.

        The confirmation is enforced here rather than only in the page, for the same reason the
        kill switch and a capital movement enforce it: a request that never said "yes" must not
        write a credential, whoever sent it.  The response is the same snapshot the GET returns,
        so it carries no value either.
        """
        panel = self.keys_panel if self.keys_panel is not None else KeysPanel(self.settings)
        code, obj = panel.act(body.get("action"), body)
        self._send(code, obj)

    def _capital(self, body: dict[str, Any]) -> None:
        """Credit, withdraw, allocate or release simulated capital.

        The confirmation is enforced here rather than only in the page for the same reason
        the kill switch enforces it: a request that never said "yes" must not move money
        - or capital - whoever sent it.
        """
        panel = self.capital if self.capital is not None else CapitalPanel(self.settings)
        code, obj = panel.act(body.get("action"), body)
        self._send(code, obj)

    def _killswitch(self, body: dict[str, Any]) -> None:
        """Arm or reset the stop.  Confirmation is enforced *here*, not only in the page:
        a request that did not say "yes, do it" must not start or stop trading, whoever
        sent it (a stray ``curl``, a form, a script that reused a cookie)."""
        if body.get("confirm") is not True:
            self._send(400, {
                "ok": False,
                "confirm_required": True,
                "action": body.get("action"),
                "error": ("arming or resetting the kill switch changes whether orders "
                          "are accepted: send confirm=true to do it"),
            })
            return
        code, obj = self.switch.act(body.get("action"), body.get("reason") or "", by="dashboard")
        self._send(code, obj)

    def _cookie_header(self) -> str:
        """The ``Set-Cookie`` value: HttpOnly, SameSite=Strict, and no path leak."""
        return (
            f"{COOKIE_NAME}={self.auth.issue()}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={self.auth.ttl_s}"
        )

    def _password(self, body: dict[str, Any]) -> None:
        """Change the dashboard password.

        Only reachable with a valid session (the route sits behind the auth check above),
        and still gated on the *current* password plus an explicit ``confirm``: a copied
        cookie can use the dashboard, but it must not be able to lock its owner out of it.

        A successful change rotates the signing key, so every cookie issued before it stops
        working; the response carries a fresh one for the session that made the change.
        """
        client = self._client()
        if not self.auth.enabled:
            self._send(400, {"ok": False, "error": (
                "the login is disabled on this dashboard: there is no password to change"
            )})
            return
        if self.auth.too_many_failures(client):
            self._send(429, {"error": "too many failed attempts; wait a few minutes"})
            return
        code, obj = self.auth.change(
            body.get("current"), body.get("new"), body.get("confirm") is True
        )
        if code == 200:
            self.auth.clear_failures(client)
            self._send(200, obj, headers={"Set-Cookie": self._cookie_header()})
            return
        if code == 401:
            self.auth.record_failure(client)
        self._send(code, obj)

    def _login(self, body: dict[str, Any]) -> None:
        client = self._client()
        if not self.auth.enabled:
            self._send(200, {"ok": True, "auth_enabled": False})
            return
        if self.auth.too_many_failures(client):
            self._send(429, {"error": "too many failed attempts; wait a few minutes"})
            return
        if not self.auth.verify(str(body.get("password") or "")):
            self.auth.record_failure(client)
            self._send(401, {"error": "wrong password"})
            return
        self.auth.clear_failures(client)
        self._send(200, {"ok": True, "auth_enabled": True}, headers={"Set-Cookie": self._cookie_header()})

    def _page(self) -> str:
        d = self.settings.dashboard
        boot = {
            "auth": bool(self.auth.enabled),
            "symbol": d.market_symbol,
            "interval": d.market_interval,
            "bars": int(d.market_bars),
        }
        return (
            HTML_PAGE.replace("__BOOT__", json.dumps(boot))
            .replace("__REFRESH__", str(int(d.auto_refresh_s)))
            .replace("__MK_REFRESH__", str(float(d.market_refresh_s)))
            .replace("__KEY_REFRESH__", str(float(d.key_refresh_s)))
        )


def bind_refusal(settings: Any, auth: Auth, bind: str) -> str | None:
    """Why this bind is refused, or ``None``.  Pure, so the policy is testable on its own.

    A **loopback** bind is the operator's own machine and needs no password of its own: the
    bootstrap value is deliberately short and printed because a secret nobody knows is a
    lockout, not a login.

    A **network** bind is different.  This page steers the loop, arms the emergency stop and
    moves simulated capital, so the two ways it must not open are a login that is switched
    off and a login whose password is published.  Both are checked here, and the second is
    checked against the password *in force*: the stored record is not what the login reads
    once ``dashboard.password_env`` exists, so checking the record alone would leave the door
    open on exactly the machines that provision a dashboard from the environment.
    """
    if is_loopback(bind):
        return None
    if not settings.dashboard.auth:
        return (
            f"refusing to bind {bind!r} without the dashboard login: set "
            "dashboard.auth=true (or bind 127.0.0.1). The loop controls must not be "
            "reachable without a password."
        )
    if auth.exposes_the_bootstrap_password():
        return (
            f"refusing to bind {bind!r} while the bootstrap password is still in force: a "
            "login whose password is printed in the README is not a login, and this one "
            "steers the loop, arms the emergency stop and moves simulated capital. Set a "
            "real one first (`python -m selftrade password --set`), or set the environment "
            "variable named by `dashboard.password_env`, or bind 127.0.0.1. Nothing was "
            "opened."
        )
    return None


def make_server(settings: Any, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    bind = host or settings.dashboard.host
    # The environment variable, when set, is the override; otherwise the stored credential
    # (which starts as the bootstrap default) is what the login accepts.  Neither is ever
    # printed: `Auth.describe` reports the source, not the secret.  The auth object is built
    # *before* the bind check because the check has to see which password is in force.
    password = os.environ.get(settings.dashboard.password_env) or None
    store = CredentialStore(settings.dashboard.password_file)
    auth = Auth(
        settings.dashboard.auth,
        password,
        settings.dashboard.session_ttl_s,
        store=store,
        min_length=settings.dashboard.password_min_length,
    )
    refusal = bind_refusal(settings, auth, bind)
    if refusal:
        raise ValueError(refusal)
    feed = MarketFeed(settings)
    keys = KeyHealth(settings)
    switch = KillSwitchPanel(settings)
    risk = RiskLimitsPanel(settings)
    alerts = AlertsPanel(settings)
    capital = CapitalPanel(settings)
    keys_panel = KeysPanel(settings)
    runner = LoopRunner(settings, config_path=getattr(settings, "config_path", None))
    backtest = BacktestRunner(settings)
    handler = type("BoundHandler", (DashboardHandler,), {
        "settings": settings, "auth": auth, "feed": feed, "keys": keys, "switch": switch,
        "risk": risk, "alerts": alerts, "capital": capital, "keys_panel": keys_panel,
        "runner": runner, "backtest": backtest,
    })
    # ``port=None`` -> the configured port; ``port=0`` -> an ephemeral port (tests).
    return ThreadingHTTPServer((bind, port if port is not None else settings.dashboard.port), handler)


def serve(settings: Any, host: str | None = None, port: int | None = None) -> None:
    httpd = make_server(settings, host, port)
    addr = httpd.server_address
    auth = httpd.RequestHandlerClass.auth
    print(f"selftrade dashboard on http://{addr[0]}:{addr[1]}  (Ctrl+C to stop)")
    if auth.enabled:
        env = settings.dashboard.password_env
        file = settings.dashboard.password_file
        if auth.generated:
            print(f"  login: password (generated for this run) = {auth._password}")
            print(f"  set {env} to choose your own and stop it changing on every restart")
        elif auth.from_environment:
            print(f"  login: password taken from {env} (it wins over any stored password)")
        elif auth.current_is_default():
            print(f"  login: the DEFAULT password is in use ({DEFAULT_PASSWORD}) - change it under "
                  f"Settings in the dashboard, or with `python -m selftrade password --set`")
            print(f"  (nothing is stored yet: it will be written to {file} as a salted hash)")
        else:
            print(f"  login: stored password from {file} (change it under Settings)")
        print(f"  sessions last {settings.dashboard.session_ttl_s} s; cookie is HttpOnly + SameSite=Strict")
    else:
        print("  login: DISABLED (loopback only)")
    print(f"  chart: {settings.dashboard.market_symbol} {settings.dashboard.market_interval} "
          f"from {settings.data.fetch_base_url} (public, keyless)")
    rail = "rail enabled" if settings.binance.enabled else "rail disabled (shows the hint)"
    print(f"  key panel: /api/binance every {settings.dashboard.key_refresh_s:g} s - {rail}, "
          f"read-only: it runs `binance check` and never sends an order")
    state = httpd.RequestHandlerClass.switch.snapshot()
    print(f"  emergency stop: {state['path']} - now "
          f"{'ENGAGED (new orders blocked)' if state['blocked'] else 'clear'}; "
          "the page can arm and reset it (`switch arm|reset` does the same)")
    runner = httpd.RequestHandlerClass.runner
    print(f"  loop controls: `start` runs the tracked loop (its pid comes back, its "
          f"output goes to {runner.log_path}); `pause`/`resume`/`stop` act on that "
          f"process and are refused while none is running")
    current = runner.status()
    if current["running"]:
        print(f"  a loop is already running: pid {current['pid']} "
              f"{current['generations_done'] or 0}/{current['generations_total'] or 0}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping dashboard")
    finally:
        httpd.server_close()
        runner.close()
        # The loop is detached on purpose: a run outlives the page, and a stopped
        # dashboard is not a reason to lose the generation it is in.  Say so instead of
        # leaving a process nobody remembers.
        after = runner.status()
        if after["running"]:
            print(f"the loop is still running (pid {after['pid']}, log {after['log']}); "
                  "`python -m selftrade session stop` stops it")
