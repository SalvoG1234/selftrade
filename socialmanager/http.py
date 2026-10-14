"""The one HTTP client: timeouts, retries, rate-limit awareness and an api_call log.

Every outbound request a **connector** makes goes through :class:`HttpClient`.  (The model
providers call their own configured endpoint directly, because their retry and timeout
behaviour is not the connectors' - they send no credential by default, and the one that does
enforce the same two rules itself.)  Going through this client buys three things that are
easy to get wrong per-connector:

* **a record.**  Each call writes a row in ``api_call`` - method, path (never a
  query string, which could carry a token), status, duration - so the Analytics
  page counts real traffic and the Logs page shows real failures;
* **bounded retries.**  429 and 5xx are retried with exponential backoff and, if
  the server sends ``Retry-After``, that is respected instead.  4xx is *not*
  retried: a 401 will not fix itself, and hammering it is how accounts get
  locked;
* **no secret in a log.**  Headers and bodies are redacted before they can reach
  an exception message.

Three refusals live here rather than in each connector, because they are the three ways a
credential leaves the machine without anyone deciding it should:

* **no redirect is followed.**  A ``3xx`` is returned to the caller, not chased.  Following
  one would re-send the ``Authorization`` header to whatever host the response names, which
  turns any compromised or misconfigured endpoint into a way to harvest the token;
* **no header with a control character.**  A value that carries a CR or LF - a token pasted
  with a trailing newline, a value read from a file - would inject a second header on the
  wire.  It is refused with a message naming the header;
* **no clear-text request off the machine.**  ``https`` is required for anything that is not
  loopback, so an ``http://`` base URL in configuration fails loudly instead of putting a
  bearer token on the network in the clear.

Tests drive the connectors through the same interface with a fake transport, so
no test in this repository can reach a real API by accident.
"""

from __future__ import annotations

import json as jsonlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from . import obs, secrets

#: Names that mean "this machine".  ``http`` is tolerated only here: a local proxy or a
#: development server is a legitimate target, and nothing crosses the network to reach it.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})

#: In a header name or value these are never legitimate: CR/LF end a header line, and NUL
#: truncates a C string inside some HTTP stacks.
_HEADER_FORBIDDEN = re.compile(r"[\r\n\x00]")


def require_safe_url(url: str) -> str:
    """Refuse a request whose transport would expose a credential in clear text.

    Returns the url unchanged when it is acceptable, and raises :class:`ValueError`
    otherwise - a configuration mistake is worth a loud failure, not a quiet leak.
    """
    parsed = urlparse(str(url))
    if parsed.scheme == "https":
        return str(url)
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in LOOPBACK_HOSTS:
        return str(url)
    raise ValueError(
        f"refusing to request {str(url)!r}: only https, or http on loopback, is allowed "
        "(an api_base of http:// would put the token on the wire in the clear)"
    )


def header_value(name: str, value: Any) -> str:
    """A header value that cannot inject a second header.  Raises on CR/LF or NUL."""
    text = str(value)
    if _HEADER_FORBIDDEN.search(text) or _HEADER_FORBIDDEN.search(str(name)):
        raise ValueError(
            f"header {str(name)!r} carries a control character (CR, LF or NUL): "
            "refusing to send it"
        )
    return text


class HttpError(RuntimeError):
    """A request that failed after retries (or failed in a way retrying cannot fix)."""

    def __init__(self, status: int, message: str, *, url: str = "", payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.url = url
        self.payload = payload


@dataclass
class Response:
    status: int
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status) < 300

    def header(self, name: str, default: str = "") -> str:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    def int_header(self, name: str, default: int = 0) -> int:
        try:
            return int(self.header(name) or default)
        except (TypeError, ValueError):
            return default

    def json(self) -> Any:
        if isinstance(self.payload, (dict, list)):
            return self.payload
        if self.text:
            try:
                return jsonlib.loads(self.text)
            except (TypeError, ValueError):
                return None
        return None

    def raise_for_status(self) -> Response:
        if not self.ok:
            raise HttpError(self.status, self.error or self.text[:300], url=self.url,
                            payload=self.payload)
        return self


class Transport(Protocol):
    """The seam the tests replace.  ``requests.Session`` satisfies it structurally."""

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class RequestsTransport:
    """Default transport: ``requests``, imported lazily so tests never need a socket."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = float(timeout)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:  # pragma: no cover - network
        import requests

        kwargs.setdefault("timeout", self.timeout)
        # The secure defaults are stated here as well as in the client, so a caller that
        # reaches a transport directly cannot accidentally opt out of them.
        kwargs.setdefault("allow_redirects", False)
        kwargs.setdefault("verify", True)
        return requests.request(method, url, **kwargs)


@dataclass
class RateLimit:
    """A local token timer, so a loop cannot exceed a documented window."""

    min_interval_s: float = 0.0
    _last: float = 0.0

    def wait(self, sleep: Callable[[float], None] = time.sleep) -> float:
        if self.min_interval_s <= 0:
            return 0.0
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval_s:
            pause = self.min_interval_s - delta
            sleep(pause)
            self._last = time.monotonic()
            return pause
        self._last = now
        return 0.0


class HttpClient:
    """A small, observable wrapper around a transport."""

    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        connector: str = "http",
        db: Any = None,
        transport: Transport | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        backoff_s: float = 0.6,
        max_backoff_s: float = 30.0,
        min_interval_s: float = 0.0,
        user_agent: str = "selftrade-social-manager",
        secret_names: tuple[str, ...] = (),
        sleep: Callable[[float], None] = time.sleep,
        run_id: str | None = None,
    ):
        self.connector = connector
        self.db = db
        self.transport = transport or RequestsTransport(timeout=timeout)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = float(backoff_s)
        self.max_backoff_s = float(max_backoff_s)
        self.rate = RateLimit(min_interval_s)
        self.user_agent = user_agent
        self.secret_names = tuple(secret_names)
        self.sleep = sleep
        self.run_id = run_id

    # ------------------------------------------------------------------ calls
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: float | None = None,
        purpose: str = "",
    ) -> Response:
        method = str(method).upper()
        url = require_safe_url(url)
        host = urlparse(url).netloc
        send_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        for key, value in (headers or {}).items():
            if value is None:
                continue
            send_headers[key] = header_value(key, value)

        attempt = 0
        last: Response | None = None
        while True:
            attempt += 1
            self.rate.wait(self.sleep)
            started = time.monotonic()
            try:
                raw = self.transport.request(
                    method,
                    url,
                    params=params,
                    headers=send_headers,
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=timeout or self.timeout,
                    allow_redirects=False,   # a 3xx is an answer, never a place to send the token
                )
                response = _to_response(raw, url)
            except Exception as exc:  # noqa: BLE001 - transport failures are data here
                message = secrets.redact(str(exc), self.secret_names)
                response = Response(status=0, url=url, error=f"{type(exc).__name__}: {message}")

            duration_ms = int((time.monotonic() - started) * 1000)
            self._record(method, url, response, duration_ms, purpose, attempt)
            last = response

            retryable = response.status == 0 or response.status in self.RETRY_STATUS
            if retryable and attempt <= self.max_retries:
                pause = self._pause_for(response, attempt)
                obs.warning(
                    "http.retry",
                    connector=self.connector,
                    detail={"method": method, "status": response.status, "attempt": attempt,
                            "pause_s": round(pause, 2)},
                )
                self.sleep(pause)
                continue
            return response

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def graphql(self, url: str, query: str, variables: dict[str, Any] | None = None,
                **kwargs: Any) -> Response:
        return self.request(
            "POST", url, json_body={"query": query, "variables": variables or {}},
            purpose="graphql-query", **kwargs,
        )

    # ---------------------------------------------------------------- details
    def _pause_for(self, response: Response, attempt: int) -> float:
        retry_after = response.header("Retry-After")
        if retry_after:
            try:
                return min(self.max_backoff_s, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(self.max_backoff_s, self.backoff_s * (2 ** (attempt - 1)))

    def _record(self, method: str, url: str, response: Response, duration_ms: int,
                purpose: str, attempt: int) -> None:
        if self.db is None:
            return
        try:
            self.db.record_api_call(
                self.connector,
                method,
                urlparse(url).path or "/",
                status=response.status,
                duration_ms=duration_ms,
                run_id=self.run_id,
                error=secrets.redact(response.error, self.secret_names),
                meta={
                    "host": urlparse(url).netloc,
                    "purpose": purpose,
                    "attempt": attempt,
                    "rate_remaining": response.int_header("x-ratelimit-remaining", -1),
                },
            )
        except Exception:  # pragma: no cover - telemetry must never break a call
            pass

    def bearer(self, token_env: str) -> dict[str, str]:
        """The Authorization header for a credential, or ``{}`` when unset."""
        token = secrets.read(token_env)
        return {"Authorization": f"Bearer {token}"} if token else {}


def _to_response(raw: Any, url: str) -> Response:
    if isinstance(raw, Response):
        return raw
    status = int(getattr(raw, "status_code", 0) or 0)
    headers = {str(k): str(v) for k, v in dict(getattr(raw, "headers", {}) or {}).items()}
    payload: Any = None
    text = ""
    try:
        payload = raw.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is normal for errors
        try:
            text = str(getattr(raw, "text", "") or "")
        except Exception:  # pragma: no cover
            text = ""
    error = ""
    if status >= 400:
        if isinstance(payload, dict):
            error = str(payload.get("message") or payload.get("error") or "")[:300]
        if not error:
            error = text[:300]
    return Response(status=status, payload=payload, headers=headers, url=url, text=text,
                    error=error)
