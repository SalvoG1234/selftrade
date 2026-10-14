"""Rail credentials: the environment wins, and a local file is the fallback.

Every secret in this project is addressed by the *name* of the environment variable that
holds it (``config.yaml`` carries the names, never the values), and for almost every one of
them that is the whole story.  The rail credentials are the exception: an exchange key pair
does have to be typed somewhere, so this module gives it a place.

* **The environment still wins.**  A value exported in the shell is the one the rail uses, and
  the page says so - a machine that already exports the pair cannot be surprised by a file
  someone left lying around.
* Otherwise the value comes from one JSON file (``state/secrets.json`` by default, which
  ``.gitignore`` keeps out of the repository), written atomically and owner-only (``0600``
  where the platform honours it) - the same discipline :mod:`selftrade.credentials` uses for
  the dashboard password.
* The value is stored **in clear text**, and this paragraph is here so nothing implies
  otherwise: an API secret has to be sent to the exchange, so it cannot be hashed the way a
  password can.  What that costs is stated where it matters: whoever can read that file can
  use the key, and whoever can read it can already rewrite this project's database, reports
  and the password record beside it.  A key that must not be readable by the account running
  this program does not belong in this file.
* :meth:`SecretStore.describe` never returns a value.  It reports the name, where the effective
  value came from, whether one is set, when it changed and a **fingerprint** - a SHA-256
  prefix that changes when the value changes and reveals nothing about it.
* A **corrupt** file resolves every name to ``None``, so the rail refuses to arm rather than
  arming with a key nobody chose; ``describe`` names the problem instead of failing quietly.

The one thing this module deliberately does not do is decide *whether* a rail may use a
credential.  ``binance.enabled``, ``live`` and ``--live`` gate that, and the sealed risk gate
and the kill switch gate every order; a key sitting in this file arms nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

#: Where the credentials are kept when ``binance.key_file`` does not say otherwise.  It lives
#: under ``state/``, which ``.gitignore`` already excludes, so a key can never be committed by
#: accident from the default location.
DEFAULT_FILE = "state/secrets.json"

FILE_VERSION = 1

#: An API secret is a few dozen characters.  The cap is not a format check (exchanges differ);
#: it is there so a paste of a whole file cannot silently become a "credential".
MAX_VALUE_CHARS = 512

#: Environment-variable shaped names only: these strings end up in a config file and in the
#: report, and nothing else should ever be storable under them.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class SecretError(ValueError):
    """Refused: the name or the value would leave the store in a state nobody chose."""


def check_name(name: Any) -> str | None:
    """Why this name is refused, or ``None`` when it is acceptable."""
    if not isinstance(name, str) or not name.strip():
        return "the variable name cannot be empty"
    if not _NAME_RE.match(name.strip()):
        return (
            "a variable name must look like an environment variable "
            "(letters, digits and underscores, not starting with a digit)"
        )
    return None


def check_value(value: Any) -> str | None:
    """Why this value is refused, or ``None`` when it is acceptable.

    Whitespace around a pasted key is stripped rather than refused (that is what a copy-paste
    produces); anything with a line break or a control character inside is refused, because a
    credential with one is never what the operator meant to store.
    """
    if not isinstance(value, str):
        return "the value must be text"
    text = value.strip()
    if not text:
        return "the value cannot be empty"
    if len(text) > MAX_VALUE_CHARS:
        return f"that is {len(text)} characters: longer than any API key ({MAX_VALUE_CHARS} max)"
    if any(ch.isspace() for ch in text) or any(ord(ch) < 32 for ch in text):
        return "the value contains whitespace or a control character in the middle"
    return None


def fingerprint(value: str) -> str:
    """A short digest of the value: the same value gives the same string, nothing else does.

    It is what makes "did the key change?" answerable without ever reading the key back.
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class SecretStore:
    """One JSON file of named credentials, with the environment taking precedence.

    ``environ`` is injectable so every rule here is testable without touching the real
    environment of the test process - the same reason the other loaders in this project take
    an injectable client.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_FILE,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        self.path = Path(path)
        self._environ = environ

    # ---------------------------------------------------------------- reading
    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _raw(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return b""  # unreadable: treated exactly like a corrupt record

    def _parse(self, raw: bytes) -> tuple[dict[str, dict[str, Any]], str | None]:
        """``(records, corrupt_reason)``.  A malformed file yields no records at all."""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - anything unreadable is "corrupt"
            return {}, f"not readable JSON ({exc})"
        if not isinstance(data, dict):
            return {}, f"the file holds a {type(data).__name__}, not an object"
        keys = data.get("keys")
        if keys is None:
            return {}, "no `keys` object in the file"
        if not isinstance(keys, dict):
            return {}, f"`keys` is a {type(keys).__name__}, not an object"
        out: dict[str, dict[str, Any]] = {}
        for name, record in keys.items():
            if check_name(name) or not isinstance(record, dict):
                continue
            value = record.get("value")
            if not isinstance(value, str) or not value:
                continue
            out[str(name)] = record
        return out, None

    def record(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """The stored records and, when the file is unreadable, why."""
        raw = self._raw()
        if raw is None:
            return {}, None
        return self._parse(raw)

    def exists(self) -> bool:
        return self._raw() is not None

    def stored_names(self) -> list[str]:
        return sorted(self.record()[0])

    def get(self, name: str) -> str | None:
        """The effective value: the environment first, then the file.  ``None`` if neither."""
        value, _ = self.resolve(name)
        return value

    def resolve(self, name: str) -> tuple[str | None, str | None]:
        """``(value, source)`` with source in ``"environment"``, ``"file"`` or ``None``."""
        env = self.environ.get(str(name))
        if env:
            return str(env), "environment"
        records, _ = self.record()
        record = records.get(str(name)) or {}
        value = record.get("value")
        if isinstance(value, str) and value:
            return value, "file"
        return None, None

    # ---------------------------------------------------------------- writing
    def set(self, name: str, value: str, *, by: str = "operator") -> dict[str, Any]:
        """Store ``value`` under ``name``.  Never returns the value.

        The write is atomic (temp file, then ``os.replace``) and owner-only before any content
        lands, so a reader never sees a half-written file and the file is never briefly
        world-readable.
        """
        refusal = check_name(name) or check_value(value)
        if refusal:
            raise SecretError(refusal)
        clean_name = str(name).strip()
        clean_value = str(value).strip()

        records, corrupt = self.record()
        if corrupt:
            # Refusing beats silently discarding whatever was there: the operator should
            # decide what to do with a file this module did not write.
            raise SecretError(
                f"{self.path} exists but is not a readable credential record ({corrupt}); "
                f"move it aside or repair it before storing anything else"
            )
        records[clean_name] = {
            "value": clean_value,
            "fingerprint": fingerprint(clean_value),
            "changed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "changed_by": str(by or "operator"),
        }
        self._write(records)
        return self.describe_one(clean_name)

    def clear(self, name: str) -> bool:
        """Forget one stored name.  True when something was actually removed."""
        records, corrupt = self.record()
        if corrupt:
            raise SecretError(f"{self.path} is not readable ({corrupt}); nothing was removed")
        clean_name = str(name).strip()
        if clean_name not in records:
            return False
        del records[clean_name]
        if records:
            self._write(records)
        else:
            # No names left: the file goes, so "there is a secrets file" stays meaningful.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover - a read-only state dir
                raise SecretError(f"could not remove {self.path}: {exc}") from exc
        return True

    def _write(self, records: dict[str, dict[str, Any]]) -> None:
        payload = {
            "version": FILE_VERSION,
            "note": (
                "rail credentials for this machine, in clear text (an API secret has to be "
                "sent, so it cannot be hashed): mode 0600 where the platform honours it, never "
                "returned by any API, and read only after the environment variable of the same "
                "name comes up empty. Nothing here arms a rail."
            ),
            "keys": records,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - a read-only state dir
            tmp.unlink(missing_ok=True)
            raise SecretError(f"could not write {self.path}: {exc}") from exc

    # ---------------------------------------------------------------- reporting
    def describe_one(self, name: str) -> dict[str, Any]:
        """Everything about one name except the value."""
        clean_name = str(name).strip()
        records, _ = self.record()
        record = records.get(clean_name) or {}
        value, source = self.resolve(clean_name)
        stored = isinstance(record.get("value"), str) and bool(record.get("value"))
        return {
            "name": clean_name,
            "set": value is not None,
            "source": source,
            "environment": bool(self.environ.get(clean_name)),
            "stored": stored,
            "fingerprint": fingerprint(value) if value else None,
            "changed_at": record.get("changed_at") if stored else None,
            "changed_by": record.get("changed_by") if stored else None,
        }

    def describe(self, names: Iterable[str]) -> dict[str, Any]:
        """The panel's whole answer: names, sources and fingerprints, never a value.

        The ``note`` is the sentence the page shows, and it is here rather than in the page so
        the CLI, the API and the documentation cannot drift apart on what this file is.
        """
        records, corrupt = self.record()
        listed = [self.describe_one(n) for n in names]
        return {
            "file": str(self.path),
            "exists": self.exists(),
            "stored_names": sorted(records),
            "corrupt": corrupt,
            "material": "clear text",
            "environment_wins": True,
            "max_value_chars": MAX_VALUE_CHARS,
            "vars": listed,
            "note": (
                f"Stored in clear text in {self.path}, mode 0600 where the platform honours it, "
                "never returned by this API. An environment variable of the same name wins over "
                "it. Whoever can read that file can use the key"
                + (
                    "; the file is unreadable right now, so no stored value is in use: "
                    + str(corrupt)
                    if corrupt
                    else ""
                )
                + ". Nothing here arms a rail: enabled, live and --live still gate every order, "
                "and the sealed risk gate and the kill switch gate each one."
            ),
        }
