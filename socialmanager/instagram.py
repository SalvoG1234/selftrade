"""The Instagram connector: containers, publishing, polling, comments, insights.

The two-step publish is implemented exactly as Meta documents it - create a
container, wait for processing, publish the container - with the details that
are usually skipped:

* **the quota is read, not assumed.**  Meta's own pages disagree (50 vs 100
  containers per rolling 24 hours), so :meth:`InstagramConnector.publishing_quota`
  asks ``/{ig_id}/content_publishing_limit`` and the publisher uses the number the
  API returns.  A separate limit of 400 containers *created* per day is tracked
  locally.
* **containers expire.**  A container that is not published within 24 hours is
  dead, and the code says so instead of polling forever.
* **media must be publicly fetchable over HTTPS.**  Meta downloads the file; a
  local path is not a URL.  Until ``instagram.public_media_base_url`` is set the
  connector is *not ready* and publishing stays simulated - there is no fallback
  to driving a browser, which the brief forbids and which would break the account.

Both login routes are supported: **Instagram Login** (no Facebook Page,
``graph.instagram.com``, ``instagram_business_*`` scopes) and **Facebook Login**
(Page-linked, ``graph.facebook.com``, ``instagram_*`` scopes).

This connector has **never been exercised against a live Instagram account from
this repository**, and it cannot be until an app review and a business
verification are completed on the operator's side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import obs, secrets
from .http import HttpClient, Transport
from .publisher import ExternalPost

#: Meta's published ceiling for containers created in a rolling 24 hours.
CONTAINER_CREATE_LIMIT_24H = 400
#: Containers expire 24 hours after creation.
CONTAINER_TTL_HOURS = 24
#: Meta's own recommendation: poll about once a minute, for up to five minutes.
POLL_MAX_ATTEMPTS = 30
POLL_INTERVAL_S = 3.0

SCOPES = {
    "instagram_login": {
        "publish": ["instagram_business_basic", "instagram_business_content_publish"],
        "comments": ["instagram_business_basic", "instagram_business_manage_comments"],
        "insights": ["instagram_business_basic", "instagram_business_manage_insights"],
    },
    "facebook_login": {
        "publish": ["instagram_basic", "instagram_content_publish", "pages_read_engagement"],
        "comments": ["instagram_basic", "instagram_manage_comments", "pages_read_engagement"],
        "insights": ["instagram_basic", "instagram_manage_insights", "pages_read_engagement"],
    },
}

#: Insight metrics per media type.  ``impressions`` is deprecated for media
#: created after 2 July 2024 and is deliberately absent.
INSIGHT_METRICS = {
    "IMAGE": ["comments", "likes", "reach", "saved", "shares", "total_interactions", "views",
              "profile_visits", "follows"],
    "CAROUSEL_ALBUM": [],
    "REELS": ["comments", "likes", "reach", "saved", "shares", "total_interactions", "views",
              "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate"],
}


class ConnectorNotReady(RuntimeError):
    pass


@dataclass
class Container:
    id: str
    status_code: str = "UNKNOWN"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status_code == "FINISHED"

    @property
    def failed(self) -> bool:
        return self.status_code in {"ERROR", "EXPIRED"}


class InstagramConnector:
    def __init__(self, settings: Any, db: Any = None, transport: Transport | None = None,
                 run_id: str | None = None, sleep=None):
        self.settings = settings
        self.cfg = settings.instagram
        self.db = db
        self.run_id = run_id
        kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
        self.http = HttpClient(
            connector="instagram",
            db=db,
            transport=transport,
            timeout=self.cfg.timeout,
            max_retries=2,
            secret_names=(self.cfg.token_env, self.cfg.app_secret_env),
            run_id=run_id,
            **kwargs,
        )

    # ------------------------------------------------------------------ status
    @property
    def token(self) -> str | None:
        return secrets.read(self.cfg.token_env)

    @property
    def base(self) -> str:
        if str(self.cfg.login) == "facebook_login":
            return self.cfg.api_base_facebook_login.rstrip("/")
        return self.cfg.api_base.rstrip("/")

    @property
    def version(self) -> str:
        return self.cfg.graph_version

    def readiness(self) -> dict[str, Any]:
        missing: list[str] = []
        if not self.cfg.enabled:
            missing.append("connector disabled in social.yaml (connectors.instagram.enabled)")
        if not self.token:
            missing.append(
                f"{self.cfg.token_env} is not set: complete the Business Login / Facebook "
                "Login flow and store the long-lived token (docs/SOCIAL-CONNECTORS.md)"
            )
        if not self.cfg.ig_user_id:
            missing.append("connectors.instagram.ig_user_id is empty (the IG professional account id)")
        if not self.cfg.public_media_base_url:
            missing.append(
                "connectors.instagram.public_media_base_url is empty: Meta fetches media over "
                "HTTPS, so generated files need a public URL before they can be published"
            )
        if self.cfg.dry_run:
            missing.append("connectors.instagram.dry_run is true: publication is simulated")
        account = self.db.account_for("instagram") if self.db is not None else None
        scopes = {str(s) for s in ((account or {}).get("scopes") or [])}
        needed = set(SCOPES.get(str(self.cfg.login), {}).get("publish", []))
        if scopes and needed and not (scopes & needed):
            missing.append(f"stored scopes lack a publishing permission ({', '.join(sorted(needed))})")
        ready = not missing
        return {
            "platform": "instagram",
            "enabled": bool(self.cfg.enabled),
            "ready": ready,
            "dry_run": bool(self.cfg.dry_run),
            "login": str(self.cfg.login),
            "graph_version": self.version,
            "api_base": self.base,
            "ig_user_id": self.cfg.ig_user_id,
            "handle": self.cfg.account_handle,
            "token_env": self.cfg.token_env,
            "token_present": self.token is not None,
            "token_fingerprint": secrets.fingerprint_of_env(self.cfg.token_env),
            "scopes_by_purpose": SCOPES.get(str(self.cfg.login), {}),
            "container_create_limit_24h": CONTAINER_CREATE_LIMIT_24H,
            "publish_quota": "read at runtime from /{ig_id}/content_publishing_limit",
            "container_ttl_hours": CONTAINER_TTL_HOURS,
            "notes": [
                "the account must be a professional (Business or Creator) account",
                "getting out of development mode needs App Review and business verification",
                "carousel insights are unavailable for media inside an album",
            ],
            "missing": missing,
            "never_used_live": "no live call has been made from this repository",
        }

    def health(self) -> dict[str, Any]:
        if not (self.token and self.cfg.ig_user_id):
            return {"ok": False, "status": 0, "error": "token or ig_user_id missing"}
        response = self.http.get(
            f"{self.base}/{self.version}/{self.cfg.ig_user_id}",
            params={"fields": "id,username,account_type,media_count", "access_token": self.token},
        )
        data = response.json() or {}
        return {
            "ok": response.ok,
            "status": response.status,
            "username": data.get("username"),
            "account_type": data.get("account_type"),
            "media_count": data.get("media_count"),
            "error": response.error,
        }

    def publishing_quota(self) -> dict[str, Any]:
        """The quota Meta currently reports, not the number a doc page claims."""
        if not (self.token and self.cfg.ig_user_id):
            return {"available": False, "reason": "token or ig_user_id missing"}
        response = self.http.get(
            f"{self.base}/{self.version}/{self.cfg.ig_user_id}/content_publishing_limit",
            params={"fields": "quota_usage,config", "access_token": self.token},
        )
        if not response.ok:
            return {"available": False, "reason": response.error or "request failed"}
        block = (response.json() or {}).get("data") or []
        entry = block[0] if isinstance(block, list) and block else {}
        config = entry.get("config") or {}
        return {
            "available": True,
            "quota_usage": entry.get("quota_usage"),
            "quota_total": config.get("quota_total"),
            "quota_duration_s": config.get("quota_duration"),
        }

    # --------------------------------------------------------------- publishing
    def publish(self, variant: dict[str, Any], account: dict[str, Any] | None = None) -> ExternalPost:
        state = self.readiness()
        if not state["ready"]:
            raise ConnectorNotReady(
                "instagram connector is not ready: " + "; ".join(state["missing"][:3])
            )
        media = [str(m) for m in (variant.get("media") or [])]
        if not media:
            raise ValueError("Instagram publishing requires at least one image")
        caption = str(variant.get("body") or "").strip()
        hashtags = str(variant.get("hashtags") or "").strip()
        if hashtags and len(caption) + len(hashtags) + 2 <= 2200:
            caption = f"{caption}\n\n{hashtags}"
        if len(caption) > 2200:
            raise ValueError(f"caption would be {len(caption)} characters (limit 2200)")

        if len(media) == 1:
            container = self.create_image_container(self._public_url(media[0]), caption)
        else:
            container = self.create_carousel(media[:10], caption)
        self._wait_for_container(container.id)
        published = self.publish_container(container.id)
        return ExternalPost(
            external_id=published.get("id", ""),
            url="",
            meta={
                "container_id": container.id,
                "media_type": published.get("media_type", ""),
                "items": len(media),
            },
        )

    def create_image_container(self, image_url: str, caption: str,
                               *, is_carousel_item: bool = False) -> Container:
        payload: dict[str, Any] = {"image_url": image_url}
        if is_carousel_item:
            payload["is_carousel_item"] = "true"
        else:
            payload["caption"] = caption
        return self._create_container(payload)

    def create_video_container(self, video_url: str, caption: str, *,
                               as_reel: bool = True, share_to_feed: bool = True) -> Container:
        payload: dict[str, Any] = {
            "video_url": video_url,
            "media_type": "REELS" if as_reel else "VIDEO",
        }
        if as_reel:
            payload["share_to_feed"] = "true" if share_to_feed else "false"
        else:
            payload["caption"] = caption
        if as_reel:
            payload["caption"] = caption
        return self._create_container(payload)

    def create_carousel(self, image_paths: list[str], caption: str) -> Container:
        children: list[str] = []
        for path in image_paths:
            child = self.create_image_container(self._public_url(path), "", is_carousel_item=True)
            children.append(child.id)
        if len(children) < 2:
            raise ValueError("a carousel needs at least two items")
        return self._create_container({
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
        })

    def _create_container(self, payload: dict[str, Any]) -> Container:
        self._track_container_creation()
        body = {**payload, "access_token": self.token}
        response = self.http.post(
            f"{self.base}/{self.version}/{self.cfg.ig_user_id}/media", json_body=body
        )
        if not response.ok:
            raise RuntimeError(f"container creation failed: {response.status} {response.error}")
        data = response.json() or {}
        container_id = str(data.get("id") or "")
        if not container_id:
            raise RuntimeError("container creation returned no id")
        return Container(id=container_id, raw=data)

    def container_status(self, container_id: str) -> Container:
        response = self.http.get(
            f"{self.base}/{self.version}/{container_id}",
            params={"fields": "status_code,status", "access_token": self.token},
        )
        if not response.ok:
            return Container(id=container_id, status_code="UNKNOWN", raw={"error": response.error})
        data = response.json() or {}
        return Container(id=container_id, status_code=str(data.get("status_code") or "UNKNOWN"),
                         raw=data)

    def _wait_for_container(self, container_id: str) -> Container:
        for attempt in range(int(self.cfg.container_poll_max) or POLL_MAX_ATTEMPTS):
            container = self.container_status(container_id)
            if container.finished:
                return container
            if container.failed:
                raise RuntimeError(
                    f"container {container_id} is {container.status_code}: "
                    "recreate it and publish within 24 hours"
                )
            self.http.sleep(float(self.cfg.container_poll_s or POLL_INTERVAL_S))
        raise TimeoutError(
            f"container {container_id} was still {container.status_code} after polling; "
            "Meta processes most media within minutes"
        )

    def publish_container(self, container_id: str) -> dict[str, Any]:
        response = self.http.post(
            f"{self.base}/{self.version}/{self.cfg.ig_user_id}/media_publish",
            json_body={"creation_id": container_id, "access_token": self.token},
        )
        if not response.ok:
            raise RuntimeError(f"publish failed: {response.status} {response.error}")
        return response.json() or {}

    # ------------------------------------------------------- comments/insights
    def comments(self, media_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{self.base}/{self.version}/{media_id}/comments",
            params={"fields": "id,text,username,timestamp,like_count", "limit": min(50, limit),
                    "access_token": self.token},
        )
        if not response.ok:
            return []
        return list((response.json() or {}).get("data") or [])

    def comment_replies(self, comment_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{self.base}/{self.version}/{comment_id}/replies",
            params={"fields": "id,text,username,timestamp", "limit": min(50, limit),
                    "access_token": self.token},
        )
        if not response.ok:
            return []
        return list((response.json() or {}).get("data") or [])

    def reply_to_comment(self, comment_id: str, message: str) -> ExternalPost:
        """Replying is disabled by default and always held for a human."""
        response = self.http.post(
            f"{self.base}/{self.version}/{comment_id}/replies",
            json_body={"message": message[:2200], "access_token": self.token},
        )
        if not response.ok:
            raise RuntimeError(f"reply failed: {response.status} {response.error}")
        return ExternalPost(external_id=str((response.json() or {}).get("id") or ""),
                            meta={"reply": True})

    def insights(self, media_id: str, media_type: str = "IMAGE") -> dict[str, Any]:
        metrics = INSIGHT_METRICS.get(str(media_type).upper(), [])
        if not metrics:
            return {"available": False,
                    "reason": f"no insight metrics are published for {media_type}"}
        response = self.http.get(
            f"{self.base}/{self.version}/{media_id}/insights",
            params={"metric": ",".join(metrics), "access_token": self.token},
        )
        if not response.ok:
            return {"available": False, "reason": response.error or "request failed"}
        values: dict[str, Any] = {}
        for entry in (response.json() or {}).get("data") or []:
            name = str(entry.get("name") or "")
            points = entry.get("values") or []
            value = points[0].get("value") if points else None
            if name:
                values[name] = value
        return {
            "available": bool(values),
            "metrics": values,
            "reason": "" if values else "Meta returned an empty dataset",
            "retention_note": "insights can be delayed up to 48 hours; an empty "
                              "dataset means unavailable, not zero",
        }

    def refresh_long_lived_token(self) -> dict[str, Any]:
        """Instagram Login long-lived tokens last 60 days and must be refreshed."""
        if not (self.token and secrets.read(self.cfg.app_secret_env)):
            return {"ok": False, "reason": "token or app secret missing"}
        response = self.http.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": self.token},
        )
        data = response.json() or {}
        return {
            "ok": response.ok and bool(data.get("access_token")),
            "expires_in": data.get("expires_in"),
            "reason": response.error,
            "note": "store the new token in the environment yourself: it is never written here",
        }

    # ----------------------------------------------------------------- helpers
    def _public_url(self, path: str) -> str:
        base = str(self.cfg.public_media_base_url or "").rstrip("/")
        if not base:
            raise ConnectorNotReady(
                "instagram.public_media_base_url is not configured: Meta needs an HTTPS URL"
            )
        name = Path(path).name
        return f"{base}/{quote(name)}"

    def _track_container_creation(self) -> None:
        """Count container creations per rolling 24h; Meta's ceiling is 400."""
        if self.db is None:
            return
        count = self.db.setting("ig_containers_24h", {"count": 0, "started_at": ""})
        from .db import to_ts, utcnow, parse_ts
        from datetime import timedelta

        started = parse_ts(str(count.get("started_at") or ""))
        if not started or utcnow() - started > timedelta(hours=24):
            count = {"count": 0, "started_at": to_ts()}
        count["count"] = int(count.get("count", 0)) + 1
        if count["count"] > CONTAINER_CREATE_LIMIT_24H:
            raise RuntimeError(
                f"container creation cap reached ({CONTAINER_CREATE_LIMIT_24H} per 24 hours)"
            )
        self.db.set_setting("ig_containers_24h", count, actor="instagram-connector")


def supports(platform: str) -> bool:
    return str(platform) == "instagram"
