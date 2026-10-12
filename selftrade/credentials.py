"""The dashboard login password: one salted hash in its own small file.

Every other secret in this project is read-only - an environment variable whose *name*
lives in ``config.yaml`` and whose value the program only ever reads.  This one is
different by necessity: the operator changes it from the dashboard, so it has to be
written somewhere.  It is written here, and only as a **hash**:

* ``pbkdf2_hmac`` SHA-256, :data:`ITERATIONS` rounds, with a fresh random salt per
  change - the file never contains the password, and two installs that pick the same
  password still store different bytes;
* the record is written atomically (temp file, then ``os.replace``) and made owner-only
  (``0600`` where the platform honours it), so a reader never sees a half-written hash;
* a **missing** file means "the operator has not chosen one yet": the login then accepts
  the bootstrap default :data:`DEFAULT_PASSWORD` (``0000``), and the dashboard says so
  until it is changed;
* a **corrupt** file fails closed - no password matches and the login refuses, instead of
  quietly falling back to the bootstrap default.  Recover with
  ``python -m selftrade password --reset``; a shell on this machine is full access
  anyway, since whoever can rewrite this file can rewrite the database beside it.

The hash and the salt are never returned by any API and never printed:
:meth:`CredentialStore.describe` reports *where* the password comes from and when it
changed, not what it is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The password a fresh install accepts, until someone changes it in Settings.
#: It is short and well known **on purpose**: it is a bootstrap value, and the dashboard
#: banners it until it is replaced.  A secret no one knows is a lockout, not a login.
DEFAULT_PASSWORD = "0000"

ALGORITHM = "pbkdf2_hmac_sha256"
ITERATIONS = 240_000
SALT_BYTES = 16
FILE_VERSION = 1
DEFAULT_MIN_LENGTH = 4


class CredentialError(ValueError):
    """Refused: the request would leave the login in an unusable state."""


def check_new_password(
    new: Any, current: str | None = None, min_length: int = DEFAULT_MIN_LENGTH
) -> str | None:
    """Why this new password is refused, or ``None`` when it is acceptable.

    One place for the rule, so the CLI, the login and the API cannot disagree about it:
    the sentence returned here is what the operator reads.
    """
    if not isinstance(new, str) or not new.strip():
        return "the password cannot be empty"
    if len(new) < int(min_length):
        return (f"at least {int(min_length)} characters are required "
                f"(this one has {len(new)})")
    if current is not None and new == current:
        return "the new password is the current one: pick something different"
    return None


@dataclass(frozen=True)
class Credential:
    """The record on disk.  No field here is the password."""

    salt: bytes
    digest: bytes
    iterations: int
    changed_at: str | None = None
    changed_by: str | None = None
    corrupt: bool = False
    reason: str | None = None


class CredentialStore:
    """Read, verify and replace the stored password hash.

    One file, one record.  ``verify`` is constant-time and never raises: a file that is
    unreadable, truncated or hand-edited to nonsense matches *nothing*.
    """

    def __init__(self, path: str | Path = "state/dashboard_auth.json"):
        self.path = Path(path)
        self._cache: tuple[bytes, bool] | None = None
        """``(raw file bytes, is_default)`` - re-deriving the hash on every settings tick
        would be a needless 60 ms; the cache is keyed by the file's own bytes, so a change
        on disk invalidates it by construction."""

    # ------------------------------------------------------------ hashing
    @staticmethod
    def derive(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
        """PBKDF2-HMAC-SHA256.  The only place a password is turned into bytes."""
        return hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, int(iterations))

    # ------------------------------------------------------------ reading
    def _raw(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return b""  # unreadable: treated exactly like a corrupt record (fail closed)

    @staticmethod
    def _parse(raw: bytes) -> Credential:
        try:
            data = json.loads(raw.decode("utf-8"))
            salt = bytes.fromhex(data["salt"])
            digest = bytes.fromhex(data["hash"])
            iterations = int(data.get("iterations") or ITERATIONS)
        except Exception as exc:  # noqa: BLE001 - any malformed record is "corrupt"
            return Credential(b"", b"", ITERATIONS, corrupt=True, reason=str(exc))
        if not salt or not digest or iterations <= 0:
            return Credential(b"", b"", ITERATIONS, corrupt=True, reason="empty salt or hash")
        return Credential(
            salt=salt,
            digest=digest,
            iterations=iterations,
            changed_at=data.get("changed_at"),
            changed_by=data.get("changed_by"),
        )

    def read(self) -> Credential | None:
        """The stored record, or ``None`` when there is no file at all."""
        raw = self._raw()
        if raw is None:
            return None
        return self._parse(raw)

    def exists(self) -> bool:
        return self._raw() is not None

    def record(self) -> Credential | None:
        """The record, whichever of the three states it is in (missing/corrupt/stored)."""
        return self.read()

    # ------------------------------------------------------------ checking
    def verify(self, password: Any) -> bool:
        """Is this the password?  Missing file -> the bootstrap default; corrupt -> never."""
        cred = self.read()
        if cred is None:
            return hmac.compare_digest(str(password), DEFAULT_PASSWORD)
        if cred.corrupt:
            return False
        return hmac.compare_digest(self.derive(str(password), cred.salt, cred.iterations), cred.digest)

    def is_default(self) -> bool:
        """True when the login still accepts :data:`DEFAULT_PASSWORD`.

        Reported as ``True`` both when nothing has been stored yet and when someone
        explicitly stored ``0000`` again - it is the *effective* password that matters for
        the warning, not the history of how it got there.
        """
        raw = self._raw()
        if raw is None:
            return True
        if self._cache is not None and self._cache[0] == raw:
            return self._cache[1]
        cred = self._parse(raw)
        default = (not cred.corrupt) and hmac.compare_digest(
            cred.digest, self.derive(DEFAULT_PASSWORD, cred.salt, cred.iterations)
        )
        self._cache = (raw, default)
        return default

    def check(self, new: Any, current: str | None = None, min_length: int = DEFAULT_MIN_LENGTH) -> str | None:
        return check_new_password(new, current=current, min_length=min_length)

    # ------------------------------------------------------------ writing
    def set(self, password: str, by: str = "operator", min_length: int = DEFAULT_MIN_LENGTH) -> Credential:
        """Replace the stored hash.  Refuses a password that fails :func:`check_new_password`.

        The write is atomic and owner-only, and the in-process cache is dropped, so the next
        ``verify`` reads what is actually on disk.
        """
        refusal = check_new_password(password, min_length=min_length)
        if refusal:
            raise CredentialError(refusal)

        salt = secrets.token_bytes(SALT_BYTES)
        payload = {
            "version": FILE_VERSION,
            "algorithm": ALGORITHM,
            "iterations": ITERATIONS,
            "salt": salt.hex(),
            "hash": self.derive(password, salt).hex(),
            "changed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "changed_by": str(by or "operator"),
            "note": ("a salted PBKDF2-HMAC-SHA256 hash, never the password; the dashboard "
                     "changes it in Settings, `python -m selftrade password --set` without a "
                     "dashboard, and deleting this file goes back to the bootstrap password"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            # Owner-only *before* the content lands, so the file is never briefly world-readable.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - a read-only state dir
            tmp.unlink(missing_ok=True)
            raise CredentialError(f"could not write {self.path}: {exc}") from exc
        self._cache = None
        return self.read() or Credential(salt, b"", ITERATIONS)

    def reset(self) -> bool:
        """Forget the stored password, going back to the bootstrap default.  True if a file was removed."""
        existed = self.exists()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CredentialError(f"could not remove {self.path}: {exc}") from exc
        self._cache = None
        return existed

    # ------------------------------------------------------------ reporting
    def describe(self) -> dict[str, Any]:
        """Where the password comes from - the file, never its contents."""
        cred = self.read()
        if cred is None:
            source, detail = "default", (
                f"no stored password yet: the login accepts the bootstrap password, "
                f"and Settings can set a real one (it will be written to {self.path})"
            )
        elif cred.corrupt:
            source, detail = "corrupt", (
                f"{self.path} exists but is not a readable credential record "
                f"({cred.reason}); the login refuses every password until it is replaced "
                f"with `python -m selftrade password --reset` or --set"
            )
        else:
            source, detail = "stored", f"the stored hash in {self.path}"
        return {
            "file": str(self.path),
            "exists": cred is not None,
            "source": source,
            "is_default": self.is_default(),
            "changeable": source != "corrupt",
            "algorithm": ALGORITHM,
            "iterations": cred.iterations if cred and not cred.corrupt else ITERATIONS,
            "changed_at": cred.changed_at if cred and not cred.corrupt else None,
            "changed_by": cred.changed_by if cred and not cred.corrupt else None,
            "note": detail,
        }
