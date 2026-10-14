"""Credentials by *name* only: the project never stores, prints or returns a value.

The rules this module exists to enforce:

1. configuration holds the **name** of an environment variable, never a token
   (a config value that looks like a token is rejected by :func:`check_env_name`);
2. a value is read at the moment of use and never written to the database, a
   log line, a report or an HTTP response;
3. anything that *is* stored for correlation is a **fingerprint** - a short
   SHA-256 of the value with a fixed prefix - so an operator can tell which
   credential is loaded without ever seeing it.

The last point is what makes rotation auditable: two fingerprints that differ
mean the credential changed, and neither tells you what it changed to.
"""

from __future__ import annotations

import hashlib
import os
import re

#: A plausible environment variable name.  Configuration must pass this and
#: nothing else: an operator who pastes a token into ``social.yaml`` gets a
#: config error instead of a token in version control.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

#: Names that are obviously *not* variable names but are often pasted anyway.
_LOOKS_LIKE_TOKEN = re.compile(
    r"^(gh[pousr]_|github_pat_|xox[baprs]-|sk-|EAA|ya29\.|IGQV)" ,
)


class SecretError(ValueError):
    """Raised when configuration tries to hold a secret value."""


def check_env_name(name: str | None) -> str | None:
    """Return an error string for a bad *name*, or ``None`` when it is fine.

    ``None``/empty means "not configured", which is legal and keeps the
    connector in draft mode.
    """
    if name is None or not str(name).strip():
        return None
    text = str(name).strip()
    if _LOOKS_LIKE_TOKEN.match(text):
        return (
            f"{text[:8]}... looks like a token, not an environment variable name; "
            "put the value in the environment instead"
        )
    if not _ENV_NAME.match(text):
        return (
            f"{text!r} is not a valid environment variable name "
            "(expected e.g. SOCIAL_GITHUB_TOKEN)"
        )
    return None


def read(name: str | None) -> str | None:
    """The value of the named variable, or ``None``.  Never logged by callers."""
    if not name:
        return None
    value = os.environ.get(str(name).strip())
    return value.strip() if value and value.strip() else None


def present(name: str | None) -> bool:
    return read(name) is not None


def fingerprint(value: str | None) -> str | None:
    """A short, one-way tag that identifies a credential without revealing it."""
    if not value:
        return None
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def fingerprint_of_env(name: str | None) -> str | None:
    return fingerprint(read(name))


#: Values injected by tests and the CLI when they must prove redaction works.
_EXTRA_SENSITIVE: set[str] = set()


def remember_sensitive(value: str | None) -> None:
    """Register a value that must never appear in a log line (used by tests)."""
    if value and len(value) >= 6:
        _EXTRA_SENSITIVE.add(value)


def forget_sensitive(value: str | None) -> None:
    _EXTRA_SENSITIVE.discard(value or "")


def sensitive_values(names: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Every value that :func:`redact` must mask: the named env vars plus extras."""
    out: list[str] = []
    for name in names:
        value = read(name)
        if value:
            out.append(value)
    out.extend(sorted(_EXTRA_SENSITIVE))
    return out


def redact(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """Mask anything that looks like a credential.

    Two passes: the concrete values we know about (each replaced by its
    fingerprint, so a log stays useful for correlation), then generic patterns
    for tokens the process was never handed - an API error body can echo a
    header, and a `Bearer` token in a traceback is still a leak.
    """
    if text is None:
        return ""
    out = str(text)
    for value in sensitive_values(names):
        if value and len(value) >= 6 and value in out:
            out = out.replace(value, f"[redacted:{fingerprint(value)}]")
    out = _BEARER.sub(r"\1[redacted]", out)
    # The whole match goes, and its fingerprint goes in its place: enough to tell
    # two leaked values apart in a log, useless for using either of them.
    out = _GENERIC_TOKEN.sub(
        lambda m: "[redacted:sha256:" + hashlib.sha256(m.group(1).encode()).hexdigest()[:16] + "]",
        out,
    )
    return out


_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-+/=]{12,}")
_GENERIC_TOKEN = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|sk-[A-Za-z0-9]{16,}|EAA[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,})"
)


def mask_handle(handle: str) -> str:
    """A public handle is public; this only normalises it for grouping."""
    return (handle or "").strip().lstrip("@")[:64]
