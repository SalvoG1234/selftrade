"""Optional notifications (Telegram) via environment variables only.

The token and chat id are read from the environment variables named in config
(``notify.telegram_token_env`` / ``notify.telegram_chat_id_env``) and are never
printed, stored in memory, or written to reports.
"""

from __future__ import annotations

import os
from typing import Any

import requests

TELEGRAM_API = "https://api.telegram.org"


def configured(settings: Any) -> tuple[bool, str]:
    """Return (ready, message) without revealing any secret value."""
    cfg = settings.notify
    if not cfg.enabled:
        return False, "notify.disabled"
    token = os.environ.get(cfg.telegram_token_env)
    chat = os.environ.get(cfg.telegram_chat_id_env)
    if not token:
        return False, f"env {cfg.telegram_token_env} is not set"
    if not chat:
        return False, f"env {cfg.telegram_chat_id_env} is not set"
    return True, "ready"


def send(settings: Any, text: str, *, timeout: float = 10.0, session: Any = None) -> tuple[bool, str]:
    """Send a message.  Returns (sent, message); never raises for HTTP issues."""
    ready, msg = configured(settings)
    if not ready:
        return False, msg
    cfg = settings.notify
    token = os.environ[cfg.telegram_token_env]
    chat = os.environ[cfg.telegram_chat_id_env]
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        poster = session.post if session is not None else requests.post
        r = poster(url, json={"chat_id": chat, "text": text[:4000]}, timeout=timeout)
        if getattr(r, "status_code", 0) == 200:
            return True, "sent"
        return False, f"telegram returned status {getattr(r, 'status_code', '?')}"
    except requests.RequestException as exc:
        return False, f"telegram request failed: {type(exc).__name__}"


def paper_summary(stats: dict[str, Any]) -> str:
    return (
        f"selftrade paper {stats.get('name')}: status={stats.get('status')} "
        f"cursor={stats.get('cursor')}/{stats.get('total_bars')} "
        f"trades={stats.get('n_trades')} return={stats.get('total_return', 0)*100:.2f}%"
    )
