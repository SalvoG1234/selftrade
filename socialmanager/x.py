"""The X connector, written against the endpoints that are current in late 2026.

What this file deliberately does **not** do: v1.1 ``statuses/update``, the
command-style ``POST /2/media/upload`` with ``command=INIT/APPEND/FINALIZE``, the
deprecated ``statuses/filter`` stream, or OAuth 1.0a.  Those are all still
mentioned somewhere in the wild and all still wrong for a new integration; the
current surfaces are listed in :data:`CURRENT_ENDPOINTS` and every request goes
through :meth:`XConnector._assert_current`, so a future edit cannot quietly
reintroduce one.

Authentication is **OAuth 2.0 Authorization Code with PKCE, user context**
(``tweet.read``, ``tweet.write``, ``users.read``, ``media.write``,
``offline.access``).  The app-only bearer token is read-only and cannot post, so
the connector never pretends it can.

Costs are real and are recorded: X's current self-serve pricing is pay-per-use
(about **$0.015 per post**, **$0.20 per post containing a URL**), so a post with
a link is roughly thirteen times a plain one.  :func:`estimate_cost` is used in
the API log and in the readiness report, because "post more often" has a price.

This connector has **never been exercised against a live account from this
repository**.  Until a token is configured it reports ``ready: false`` and the
publisher keeps it in simulation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import obs, secrets
from .http import HttpClient, Transport
from .publisher import ExternalPost

#: The v2 surface this connector uses.  Adding to this set is an intentional act.
CURRENT_ENDPOINTS = frozenset({
    "POST /2/tweets",
    "POST /2/media/upload",
    "POST /2/media/upload/initialize",
    "POST /2/media/upload/{id}/append",
    "POST /2/media/upload/{id}/finalize",
    "GET /2/media/upload?command=STATUS",
    "GET /2/tweets/{id}",
    "GET /2/users/me",
    "GET /2/users/{id}/mentions",
    "GET /2/tweets/search/recent?query=conversation_id:{id}",
})

#: Documented rate limits (per app / per user), for the readiness report.
RATE_LIMITS = {
    "POST /2/tweets": {"app_24h": 10_000, "user_15min": 100},
    "POST /2/media/upload": {"app_24h": 50_000, "user_15min": 500},
    "media initialize/append/finalize": {"app_24h": 180_000, "user_15min": 1_875},
}

PRICE_PER_POST_USD = 0.015
PRICE_PER_POST_WITH_URL_USD = 0.20
PRICE_PER_OWN_READ_USD = 0.001

METRIC_FIELDS = ("public_metrics", "non_public_metrics", "organic_metrics")
#: Metrics are only available for 30 days after a post is created.
METRIC_WINDOW_DAYS = 30

SIMPLE_UPLOAD_MAX_BYTES = 5 * 1024 * 1024


class ConnectorNotReady(RuntimeError):
    """Raised when a write is attempted while the connector is not ready."""


def estimate_cost(text: str) -> float:
    """What one post is expected to cost, from the current published price list."""
    has_link = "http://" in text or "https://" in text
    return PRICE_PER_POST_WITH_URL_USD if has_link else PRICE_PER_POST_USD


@dataclass
class TweetMetrics:
    external_id: str
    metrics: dict[str, Any]
    available: bool
    reason: str = ""
    age_days: float | None = None


class XConnector:
    def __init__(self, settings: Any, db: Any = None, transport: Transport | None = None,
                 run_id: str | None = None, sleep=None):
        self.settings = settings
        self.cfg = settings.x
        self.db = db
        self.run_id = run_id
        kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
        self.http = HttpClient(
            connector="x",
            db=db,
            transport=transport,
            timeout=self.cfg.timeout,
            max_retries=2,
            min_interval_s=0.0,
            secret_names=(self.cfg.access_token_env, self.cfg.client_secret_env,
                          self.cfg.client_id_env),
            run_id=run_id,
            **kwargs,
        )

    # ------------------------------------------------------------------ status
    @property
    def token(self) -> str | None:
        return secrets.read(self.cfg.access_token_env)

    def readiness(self) -> dict[str, Any]:
        token = self.token is not None
        account = self.db.account_for("x") if self.db is not None else None
        scopes = {str(s) for s in ((account or {}).get("scopes") or self.cfg.scopes)}
        has_write_scope = "tweet.write" in scopes
        notes: list[str] = []
        if not self.cfg.enabled:
            notes.append("connector disabled in social.yaml (connectors.x.enabled)")
        if not token:
            notes.append(
                f"{self.cfg.access_token_env} is not set: authorise the account in the X "
                "developer portal with the PKCE flow and export the user-context token "
                "yourself - the exchange is a manual step this CLI does not perform "
                "(docs/SOCIAL-CONNECTORS.md, section X)"
            )
        if token and not has_write_scope:
            notes.append("the stored scope list has no tweet.write: the token can only read")
        ready = bool(self.cfg.enabled and token and has_write_scope and not self.cfg.dry_run)
        if self.cfg.dry_run:
            notes.append("connectors.x.dry_run is true: publication is simulated")
        return {
            "platform": "x",
            "enabled": bool(self.cfg.enabled),
            "ready": ready,
            "dry_run": bool(self.cfg.dry_run),
            "handle": self.cfg.account_handle,
            "token_env": self.cfg.access_token_env,
            "token_present": token,
            "token_fingerprint": secrets.fingerprint_of_env(self.cfg.access_token_env),
            "scopes": sorted(scopes),
            "auth_flow": "OAuth 2.0 Authorization Code with PKCE (user context)",
            "api_base": self.cfg.api_base,
            "endpoints": sorted(CURRENT_ENDPOINTS),
            "rate_limits": RATE_LIMITS,
            "pricing": {
                "per_post_usd": PRICE_PER_POST_USD,
                "per_post_with_url_usd": PRICE_PER_POST_WITH_URL_USD,
                "per_own_read_usd": PRICE_PER_OWN_READ_USD,
                "model": "pay per use (credits); check the Developer Console for this app",
            },
            "daily_post_limit": self.cfg.daily_post_limit,
            "metric_window_days": METRIC_WINDOW_DAYS,
            "missing": notes,
            "never_used_live": "no live call has been made from this repository",
        }

    def health(self) -> dict[str, Any]:
        """Cheap, read-only: who does this token belong to?"""
        if not self.token:
            return {"ok": False, "status": 0, "error": "no access token configured"}
        response = self.http.get(
            f"{self.cfg.api_base}/2/users/me",
            headers=self._headers(), params={"user.fields": "username,name"},
        )
        data = (response.json() or {}).get("data") or {}
        return {
            "ok": response.ok,
            "status": response.status,
            "user_id": data.get("id"),
            "username": data.get("username"),
            "error": response.error,
        }

    # --------------------------------------------------------------- publishing
    def publish(self, variant: dict[str, Any], account: dict[str, Any] | None = None) -> ExternalPost:
        state = self.readiness()
        if not state["ready"]:
            raise ConnectorNotReady(
                "x connector is not ready: " + "; ".join(state["missing"][:2] or ["unconfigured"])
            )
        text = str(variant.get("body") or "").strip()
        hashtags = str(variant.get("hashtags") or "").strip()
        if hashtags and len(text) + len(hashtags) + 2 <= 280:
            text = f"{text}\n\n{hashtags}"
        if len(text) > 280:
            raise ValueError(f"X post would be {len(text)} characters (limit 280)")

        media_ids: list[str] = []
        for path in variant.get("media") or []:
            media_ids.append(self._upload_media(str(path), alt_text=str(variant.get("alt_text") or "")))

        payload: dict[str, Any] = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        response = self._post("/2/tweets", json_body=payload)
        if not response.ok:
            raise RuntimeError(f"X rejected the post: {response.status} {response.error}")
        data = (response.json() or {}).get("data") or {}
        tweet_id = str(data.get("id") or "")
        handle = self.cfg.account_handle.lstrip("@") or "i"
        return ExternalPost(
            external_id=tweet_id,
            url=f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else "",
            meta={
                "estimated_cost_usd": estimate_cost(text),
                "media_count": len(media_ids),
                "char_count": len(text),
            },
        )

    def _upload_media(self, path: str, *, alt_text: str = "") -> str:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"media file not found: {path}")
        size = file_path.stat().st_size
        data = file_path.read_bytes()
        if size <= SIMPLE_UPLOAD_MAX_BYTES:
            response = self.http.post(
                f"{self.cfg.api_base}/2/media/upload",
                data={"media_category": "tweet_image"},
                files={"media": (file_path.name, data)},
                headers=self._headers(json_body=False),
            )
            if not response.ok:
                raise RuntimeError(f"media upload failed: {response.status} {response.error}")
            media_id = str(((response.json() or {}).get("data") or {}).get("id") or "")
            if media_id and alt_text:
                # Alt text is optional; a failure here must not lose the media.
                self.http.post(
                    f"{self.cfg.api_base}/2/media/metadata",
                    json_body={"id": media_id, "metadata": {"alt_text": {"text": alt_text[:1000]}}},
                    headers=self._headers(),
                )
            return media_id

        init = self._post("/2/media/upload/initialize", json_body={
            "media_type": "image/png", "total_bytes": size, "media_category": "tweet_image",
        })
        media_id = str(((init.json() or {}).get("data") or {}).get("id") or "")
        if not media_id:
            raise RuntimeError("chunked media upload: no media id returned")
        chunk = 4 * 1024 * 1024
        for index in range(0, size, chunk):
            part = data[index:index + chunk]
            append = self.http.post(
                f"{self.cfg.api_base}/2/media/upload/{media_id}/append",
                data={"segment_index": index // chunk},
                files={"media": ("chunk", part)},
                headers=self._headers(json_body=False),
            )
            if not append.ok:
                raise RuntimeError(f"chunked append failed: {append.status} {append.error}")
        finalize = self.http.post(
            f"{self.cfg.api_base}/2/media/upload/{media_id}/finalize",
            headers=self._headers(),
        )
        if not finalize.ok:
            raise RuntimeError(f"chunked finalize failed: {finalize.status} {finalize.error}")
        return media_id

    # ------------------------------------------------------------------ reading
    def metrics(self, external_id: str) -> TweetMetrics:
        response = self.http.get(
            f"{self.cfg.api_base}/2/tweets/{external_id}",
            params={"tweet.fields": ",".join(METRIC_FIELDS)},
            headers=self._headers(),
        )
        if not response.ok:
            return TweetMetrics(external_id, {}, False, reason=response.error or "request failed")
        data = (response.json() or {}).get("data") or {}
        values: dict[str, Any] = {}
        for field in METRIC_FIELDS:
            block = data.get(field)
            if isinstance(block, dict):
                values.update({f"{field}.{k}": v for k, v in block.items()})
        return TweetMetrics(external_id, values, bool(values),
                            reason="" if values else "no metric fields returned")

    def mentions(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{self.cfg.api_base}/2/users/{user_id}/mentions",
            params={"max_results": max(5, min(100, limit)), "tweet.fields": "created_at,author_id,conversation_id"},
            headers=self._headers(),
        )
        if not response.ok:
            return []
        return list(((response.json() or {}).get("data") or []))

    def replies(self, conversation_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Replies are found through recent search on ``conversation_id``."""
        response = self.http.get(
            f"{self.cfg.api_base}/2/tweets/search/recent",
            params={
                "query": f"conversation_id:{conversation_id}",
                "max_results": max(10, min(100, limit)),
                "tweet.fields": "created_at,author_id,in_reply_to_user_id",
            },
            headers=self._headers(),
        )
        if not response.ok:
            return []
        return list(((response.json() or {}).get("data") or []))

    def reply(self, text: str, *, in_reply_to: str) -> ExternalPost:
        """Posting replies is off by default and always held for a human."""
        payload = {"text": text[:280], "reply": {"in_reply_to_tweet_id": in_reply_to}}
        response = self._post("/2/tweets", json_body=payload)
        if not response.ok:
            raise RuntimeError(f"X rejected the reply: {response.status} {response.error}")
        data = (response.json() or {}).get("data") or {}
        return ExternalPost(external_id=str(data.get("id") or ""), url="", meta={"reply": True})

    # ----------------------------------------------------------------- helpers
    def _headers(self, json_body: bool = True) -> dict[str, str]:
        headers = self.http.bearer(self.cfg.access_token_env)
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _post(self, path: str, *, json_body: dict[str, Any]) -> Any:
        self._assert_current(f"POST {path}")
        return self.http.post(f"{self.cfg.api_base}{path}", json_body=json_body,
                              headers=self._headers())

    def _assert_current(self, signature: str) -> None:
        """Refuse an endpoint that is not on the current list - including by request."""
        if signature not in CURRENT_ENDPOINTS:
            raise ValueError(
                f"{signature!r} is not a current X endpoint for this project; "
                "v1.1 and the command-style media upload are deprecated"
            )
        if self.cfg.api_base.rstrip("/").endswith("api.twitter.com"):
            obs.warning("x.legacy_host", detail={"api_base": self.cfg.api_base})


def supports(platform: str) -> bool:
    return str(platform) == "x"
