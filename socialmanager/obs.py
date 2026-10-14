"""Structured logs with redaction and a bounded ring buffer the dashboard reads.

Every line is one JSON object, written to stderr and kept in memory.  Two
properties matter more than the format:

* **no secret reaches the sink.**  The formatter runs every message through
  :func:`socialmanager.secrets.redact`, so an API error body that echoes an
  ``Authorization`` header cannot end up in a log file;
* **a run is followable.**  :func:`bind_run` puts a run id on every record
  emitted inside a pipeline, which is how "which run published this?" is
  answered from the Logs page instead of by guessing.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import secrets

LOGGER_NAME = "socialmanager"

#: How many records the dashboard can page back through without reading a file.
RING_SIZE = 500

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "social_run_id", default=None
)
_secret_names: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "social_secret_names", default=()
)

_RING: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
_LOG_FILE: Path | None = None


def bind_run(run_id: str | None):
    """Bind a run id for the current context.  Returns the reset token."""
    return _run_id.set(run_id)


def reset_run(token) -> None:  # pragma: no cover - trivial
    _run_id.reset(token)


def bind_secret_names(names: list[str] | tuple[str, ...]):
    """Tell the redactor which environment variables hold credentials."""
    return _secret_names.set(tuple(names))


def current_run() -> str | None:
    return _run_id.get()


def set_log_file(path: str | Path | None) -> None:
    """Also append the JSON lines to a file (used by the cloud worker)."""
    global _LOG_FILE
    _LOG_FILE = Path(path) if path else None
    if _LOG_FILE is not None:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage() % () if record.args else record.getMessage(),
        }
        run = _run_id.get()
        if run:
            payload["run"] = run
        for key in ("event", "platform", "subject", "detail", "outcome"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        line = secrets.redact(json.dumps(payload, ensure_ascii=False, default=str))
        _RING.append(json.loads(line))
        if _LOG_FILE is not None:  # pragma: no cover - depends on the filesystem
            try:
                with _LOG_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        return line


_configured = False


def configure(level: str = "INFO", log_file: str | Path | None = None) -> logging.Logger:
    """Idempotently wire the root logger of this package to the JSON sink."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.handlers = [handler]
        _configured = True
    if log_file is not None:
        set_log_file(log_file)
    return logger


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """The package logger, configured on first use.

    Logging inside a library should not depend on the caller having called
    :func:`configure` first, or the dashboard's Logs page would be empty whenever
    a component runs outside the CLI (tests included).
    """
    _ensure_configured()
    return logging.getLogger(name if name.startswith(LOGGER_NAME) else f"{LOGGER_NAME}.{name}")


def _ensure_configured() -> None:
    if not _configured:
        configure()


def tail(limit: int = 200, level: str | None = None, contains: str | None = None) -> list[dict[str, Any]]:
    """The in-memory log, newest last, filtered - what the Logs page renders."""
    rows = list(_RING)
    if level:
        wanted = str(level).lower()
        rows = [r for r in rows if str(r.get("level", "")).lower() == wanted]
    if contains:
        needle = contains.lower()
        rows = [r for r in rows if needle in json.dumps(r, ensure_ascii=False).lower()]
    return rows[-max(1, int(limit)) :]


def clear_ring() -> None:  # pragma: no cover - test helper
    _RING.clear()


def info(event: str, **fields: Any) -> None:
    get_logger().info(event, extra={"event": event, **fields})


def warning(event: str, **fields: Any) -> None:
    get_logger().warning(event, extra={"event": event, **fields})


def error(event: str, **fields: Any) -> None:
    get_logger().error(event, extra={"event": event, **fields})
