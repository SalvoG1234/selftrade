"""Notifications: opt-in, secret-free, and never the only copy of an error.

Telegram is the channel, and it sends nothing at all unless ``notify.enabled`` is
true *and* both environment variables named in configuration are set.  The token
is read at the moment of sending, never stored, never returned and never logged -
:func:`socialmanager.secrets.redact` is applied to the message first, so a
notification that quotes an API error cannot carry a credential.

Every attempt is recorded in a bounded sent log (what went out, when, and why),
so the dashboard can show *undelivered* notices next to delivered ones.  A failed
send stays pending and is retried on the next call instead of being marked as
told - the same rule the bot's own alerting uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from . import obs, secrets
from .db import SocialDB, to_ts

TELEGRAM_API = "https://api.telegram.org"
SENT_LOG_KEY = "notify_sent_log"
SENT_LOG_LIMIT = 50


@dataclass
class Notice:
    kind: str
    text: str
    subject: str = ""
    delivered: bool = False
    reason: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "delivered": self.delivered,
                "reason": self.reason, "at": self.at}


class NotificationService:
    def __init__(self, settings: Any, db: SocialDB | None = None, *,
                 post: Callable[..., Any] | None = None):
        self.settings = settings
        self.cfg = settings.notify
        self.db = db
        self._post = post
        self._pending: list[Notice] = []

    # ------------------------------------------------------------------ status
    def readiness(self) -> dict[str, Any]:
        token_name, chat_name = self.cfg.telegram_token_env, self.cfg.telegram_chat_id_env
        return {
            "enabled": bool(self.cfg.enabled),
            "channel": "telegram",
            "token_env": token_name,
            "token_present": secrets.present(token_name),
            "chat_id_env": chat_name,
            "chat_id_present": secrets.present(chat_name),
            "token_fingerprint": secrets.fingerprint_of_env(token_name),
            "on_error": bool(self.cfg.on_error),
            "on_approval_request": bool(self.cfg.on_approval_request),
            "on_policy_block": bool(self.cfg.on_policy_block),
            "would_send": bool(self.cfg.enabled and secrets.present(token_name)
                               and secrets.present(chat_name)),
            "note": ("nothing is sent while enabled is false or either variable is missing; "
                     "the values are never read into configuration or logs"),
        }

    # ----------------------------------------------------------------- sending
    def send(self, text: str, *, kind: str = "info", subject: str = "") -> Notice:
        notice = Notice(kind=kind, text=secrets.redact(text, self.settings.secret_names()),
                        subject=subject, at=to_ts())
        token = secrets.read(self.cfg.telegram_token_env)
        chat_id = secrets.read(self.cfg.telegram_chat_id_env)
        if not (self.cfg.enabled and token and chat_id):
            notice.reason = "notifications are disabled or not configured"
            self._remember(notice)
            obs.info("notify.skipped", detail={"kind": kind, "reason": notice.reason})
            return notice
        try:
            poster = self._post
            if poster is None:  # pragma: no cover - network path
                import requests

                poster = requests.post
            response = poster(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": notice.text[:4000],
                      "disable_web_page_preview": True},
                timeout=15,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            notice.delivered = 200 <= status < 300
            if not notice.delivered:
                notice.reason = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - a failed notice must not raise
            notice.reason = f"{type(exc).__name__}"
        self._remember(notice)
        if notice.delivered:
            obs.info("notify.sent", detail={"kind": kind})
        else:
            obs.warning("notify.failed", detail={"kind": kind, "reason": notice.reason})
        return notice

    def _remember(self, notice: Notice) -> None:
        self._pending.append(notice)
        if self.db is None:
            return
        log = self.db.setting(SENT_LOG_KEY, []) or []
        if not isinstance(log, list):
            log = []
        log.append(notice.to_dict())
        self.db.set_setting(SENT_LOG_KEY, log[-SENT_LOG_LIMIT:], actor="notifier")

    def sent_log(self) -> list[dict[str, Any]]:
        if self.db is None:
            return [n.to_dict() for n in self._pending]
        log = self.db.setting(SENT_LOG_KEY, []) or []
        return list(log) if isinstance(log, list) else []

    def undelivered(self) -> list[dict[str, Any]]:
        return [row for row in self.sent_log() if not row.get("delivered")]

    # --------------------------------------------------------------- templates
    def approval_requested(self, item_id: int, reason: str, risk: str) -> Notice:
        if not self.cfg.on_approval_request:
            return Notice(kind="approval", text="", reason="disabled", at=to_ts())
        return self.send(
            f"Approval needed (risk: {risk})\nItem #{item_id}\n{reason[:400]}\n"
            f"Open the dashboard → Approval Queue.",
            kind="approval", subject=f"content_item:{item_id}",
        )

    def policy_blocked(self, variant_id: int, reasons: list[str]) -> Notice:
        if not self.cfg.on_policy_block:
            return Notice(kind="policy", text="", reason="disabled", at=to_ts())
        return self.send(
            f"Policy refused variant #{variant_id}\n" + "\n".join(f"- {r}" for r in reasons[:4]),
            kind="policy", subject=f"content_variant:{variant_id}",
        )

    def error(self, where: str, message: str) -> Notice:
        if not self.cfg.on_error:
            return Notice(kind="error", text="", reason="disabled", at=to_ts())
        return self.send(f"Error in {where}: {message[:400]}", kind="error", subject=where)

    def published(self, platform: str, reference: str) -> Notice:
        return self.send(f"Published on {platform}: {reference}", kind="published",
                         subject=f"{platform}:{reference}")

    def feedback_urgent(self, feedback_id: int, category: str, summary: str) -> Notice:
        return self.send(
            f"Urgent feedback #{feedback_id} ({category}): {summary[:300]}",
            kind="feedback", subject=f"feedback:{feedback_id}",
        )

    def kill_switch(self, engaged: bool, actor: str) -> Notice:
        state = "ENGAGED" if engaged else "released"
        return self.send(f"Kill switch {state} by {actor}: publishing is "
                         f"{'stopped' if engaged else 'allowed again'}.",
                         kind="kill_switch", subject="system")
