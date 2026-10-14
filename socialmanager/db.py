"""Persistence: SQLite in development, PostgreSQL-shaped SQL for the cloud.

One file, one connection per process, WAL enabled, foreign keys on.  The schema
mirrors the entities in the brief (``social_account`` ... ``system_setting``)
and adds the two things that make autonomous publishing safe:

* **an append-only audit log.**  ``audit_event`` carries a SHA-256 chain
  (``prev_hash`` -> ``hash``) and two triggers that refuse ``UPDATE`` and
  ``DELETE`` outright, so tampering with history is a database error rather than
  a policy the application merely promises to follow.
* **an idempotent publish queue.**  ``scheduled_post`` is leased, not marked;
  ``published_post.dedupe_key`` is unique, so a retry after a crash - or a
  second worker that read the same job - cannot post the same text twice.

Timestamps are ISO-8601 UTC strings (``...Z``), which sort correctly as text and
survive a round trip through JSON without losing the timezone.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import EditorialState, Platform, Risk, normalize_body

SCHEMA_VERSION = 1
GENESIS = "genesis"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_ts(dt: datetime | None = None) -> str:
    """ISO-8601 UTC, second precision, ``Z`` suffix."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def jloads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS social_account (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  handle TEXT NOT NULL,
  external_id TEXT,
  role TEXT NOT NULL DEFAULT 'managed',          -- managed | source
  status TEXT NOT NULL DEFAULT 'unconfigured',
  scopes TEXT NOT NULL DEFAULT '[]',
  token_env TEXT,                                 -- the NAME of a variable
  token_fingerprint TEXT,                         -- identifies, never reveals
  token_expires_at TEXT,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (platform, handle)
);

CREATE TABLE IF NOT EXISTS content_source (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                             -- release|commit|issue|discussion|readme|docs|run
  external_ref TEXT NOT NULL,                     -- tag, sha, "issue:12", "docs/LAUNCH.md"
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  digest TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  UNIQUE (kind, external_ref)
);

CREATE TABLE IF NOT EXISTS content_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  risk TEXT NOT NULL DEFAULT 'low',
  state TEXT NOT NULL DEFAULT 'draft',
  facts TEXT NOT NULL DEFAULT '[]',
  source_refs TEXT NOT NULL DEFAULT '[]',
  planned_for TEXT,
  origin TEXT NOT NULL DEFAULT 'agent',           -- agent | operator
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_variant (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES content_item(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  lang TEXT NOT NULL,
  body TEXT NOT NULL,
  hashtags TEXT NOT NULL DEFAULT '',
  media TEXT NOT NULL DEFAULT '[]',
  alt_text TEXT NOT NULL DEFAULT '',
  char_count INTEGER NOT NULL DEFAULT 0,
  generator TEXT NOT NULL DEFAULT 'template',
  risk TEXT NOT NULL DEFAULT 'low',
  policy_state TEXT NOT NULL DEFAULT 'unchecked',
  policy_report TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (item_id, platform, lang)
);
CREATE INDEX IF NOT EXISTS idx_variant_hash ON content_variant(content_hash);

CREATE TABLE IF NOT EXISTS approval (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_type TEXT NOT NULL,                     -- content_item | scheduled_post | feedback_reply | policy_change
  subject_id INTEGER NOT NULL,
  risk TEXT NOT NULL DEFAULT 'low',
  reason TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',          -- pending|approved|rejected|expired
  requested_by TEXT NOT NULL DEFAULT 'agent',
  requested_at TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  decision_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_approval_state ON approval(state, subject_type, subject_id);

CREATE TABLE IF NOT EXISTS scheduled_post (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  variant_id INTEGER NOT NULL REFERENCES content_variant(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  account_id INTEGER REFERENCES social_account(id),
  run_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',          -- pending|leased|published|failed|cancelled|blocked
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  leased_at TEXT,
  leased_by TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  dedupe_key TEXT NOT NULL UNIQUE,
  approval_id INTEGER REFERENCES approval(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_post(state, run_at);

CREATE TABLE IF NOT EXISTS published_post (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_id INTEGER,
  variant_id INTEGER,
  platform TEXT NOT NULL,
  account_id INTEGER,
  external_id TEXT,
  url TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'published',        -- published|failed|dry_run
  dedupe_key TEXT NOT NULL UNIQUE,                -- the idempotency key
  published_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_published_platform ON published_post(platform, published_at);

CREATE TABLE IF NOT EXISTS feedback_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  external_ref TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  lang TEXT,
  category TEXT NOT NULL DEFAULT 'unclassified',
  priority TEXT NOT NULL DEFAULT 'normal',
  sentiment TEXT,
  summary TEXT NOT NULL DEFAULT '',
  cluster_key TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'new',              -- new|triaged|answered|issue_proposed|ignored
  created_at_platform TEXT,
  ingested_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  UNIQUE (platform, external_ref)
);

CREATE TABLE IF NOT EXISTS agent_run (
  id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  command TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  state TEXT NOT NULL DEFAULT 'running',          -- running|ok|failed|partial
  summary TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  items INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_run_started ON agent_run(started_at);

CREATE TABLE IF NOT EXISTS api_call (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  connector TEXT NOT NULL,
  method TEXT NOT NULL,
  endpoint TEXT NOT NULL,                         -- path only, never a query with a token
  status INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  run_id TEXT,
  error TEXT NOT NULL DEFAULT '',
  called_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_api_call_time ON api_call(called_at);

CREATE TABLE IF NOT EXISTS analytics_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  captured_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'api',             -- api | manual | unavailable
  available INTEGER NOT NULL DEFAULT 1,
  meta TEXT NOT NULL DEFAULT '{}',
  UNIQUE (platform, subject_ref, metric, captured_at)
);

CREATE TABLE IF NOT EXISTS system_setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT 'null',
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS audit_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  run_id TEXT,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_event(action, ts);

CREATE TRIGGER IF NOT EXISTS audit_event_no_update
BEFORE UPDATE ON audit_event BEGIN
  SELECT RAISE(ABORT, 'audit_event is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_event_no_delete
BEFORE DELETE ON audit_event BEGIN
  SELECT RAISE(ABORT, 'audit_event is append-only');
END;
"""


def private_mask(mode: int) -> int | None:
    """The mode an owner-only store needs, or ``None`` when it is already that strict.

    Group and other bits are *removed*, never added: a store somebody deliberately made
    stricter keeps its mode, and one that is already ``0600`` is left untouched (so opening
    a database never rewrites permissions it did not have to).  Pure, so the rule is testable
    on a platform that has no POSIX mode bits.
    """
    if not mode & 0o077:
        return None
    return mode & 0o700


def touch_private(path: Path) -> None:
    """Create a file owner-only *before* SQLite opens it, where the platform has modes.

    Creating it first matters: SQLite would otherwise create it with the process umask, and
    on a shared machine that is routinely ``0644`` - readable by every other account.  On
    Windows there are no POSIX mode bits, so this does nothing and the file inherits the ACL
    of the profile it is created in.
    """
    if os.name != "posix" or path.exists():
        return
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600))
    except OSError:  # pragma: no cover - a read-only directory surfaces at connect time
        return


class DatabaseLocked(RuntimeError):
    """Raised when SQLite stayed busy past the timeout."""


class SocialDB:
    """A thin, explicit data-access layer.  No ORM, no implicit sessions."""

    def __init__(self, path: str | Path = "state/social.db", timeout: float = 30.0):
        self.path = str(path)
        self.timeout = float(timeout)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            touch_private(Path(self.path))
        self.conn = sqlite3.connect(
            self.path if self.path == ":memory:" else self.path,
            timeout=self.timeout,
            isolation_level=None,      # explicit transactions
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = %d" % int(self.timeout * 1000))
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self._tx_depth = 0
        #: Names of store files this process had to narrow on open (see tighten_permissions).
        self.tightened: list[str] = []

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def open(cls, path: str | Path = "state/social.db", timeout: float = 30.0) -> SocialDB:
        db = cls(path, timeout=timeout)
        db.migrate()
        db.tighten_permissions()
        return db

    # ------------------------------------------------------------ file permissions
    #: What the store is allowed to be readable and writable by: its owner, and nobody else.
    #: The database holds the audit log, the ingested feedback and the settings, so a
    #: group- or world-readable file is a leak rather than a preference.
    PRIVATE_MODE = 0o600

    def store_files(self) -> list[Path]:
        """The database and the WAL siblings SQLite creates beside it, which hold the same rows."""
        if self.path == ":memory:":
            return []
        base = Path(self.path)
        return [base, Path(str(base) + "-wal"), Path(str(base) + "-shm")]

    def _mode_of(self, path: Path) -> int | None:
        """The POSIX mode of one store file, or ``None`` when it cannot be read.

        A seam on purpose: it is the only part of the permission handling that needs a real
        filesystem, so the rest of the logic can be exercised where it is written.
        """
        try:
            return stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return None

    def file_mode(self) -> str:
        """The mode the store carries, so "owner-only" is a reading rather than an assumption."""
        if self.path == ":memory:" or os.name != "posix":
            return "n/a"
        mode = self._mode_of(Path(self.path))
        return "unknown" if mode is None else format(mode, "04o")

    def tighten_permissions(self) -> list[Path]:
        """Remove group and other bits from the store and its WAL siblings.

        Bits are only ever *removed*: a store somebody deliberately made stricter stays that
        way.  Nothing is written to the audit log, deliberately - opening a store must not
        append to it, which is an invariant the tests and an operator reading the chain both
        rely on; what happened is visible instead through :meth:`permissions_summary`.  The
        method is best-effort: a filesystem that refuses ``chmod`` (a mounted volume, a
        foreign ACL) must not stop the program.
        """
        if os.name != "posix":
            return []
        changed: list[Path] = []
        for candidate in self.store_files():
            mode = self._mode_of(candidate)
            if mode is None:
                continue
            wanted = private_mask(mode)
            if wanted is None:
                continue
            try:
                os.chmod(candidate, wanted)
            except OSError:
                continue
            changed.append(candidate)
        self.tightened = [path.name for path in changed]
        return changed

    def permissions_summary(self) -> dict[str, Any]:
        """Which store files are not owner-only, so the claim is a reading not a promise."""
        if self.path == ":memory:":
            return {"enforced": False, "ok": True, "files": {}, "mode": "n/a"}
        modes: dict[str, str] = {}
        for candidate in self.store_files():
            mode = self._mode_of(candidate)
            if mode is None:
                continue
            modes[candidate.name] = format(mode, "04o")
        if os.name != "posix":
            # No mode bits to read: the file inherits the ACL of the profile it lives in.
            return {"enforced": False, "ok": True, "files": modes,
                    "mode": self.file_mode()}
        loose = sorted(name for name, mode in modes.items() if int(mode, 8) & 0o077)
        return {"enforced": True, "ok": not loose, "loose": loose, "files": modes,
                "mode": self.file_mode()}

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - close is best-effort
            pass

    def __enter__(self) -> SocialDB:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def migrate(self) -> int:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            # ``executescript`` commits any open transaction before it runs, so the
            # schema is applied outside :meth:`transaction` on purpose.
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return SCHEMA_VERSION

    @contextmanager
    def transaction(self):
        """Nested-safe explicit transaction (``BEGIN IMMEDIATE``)."""
        outermost = self._tx_depth == 0
        if outermost:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:  # pragma: no cover
                raise DatabaseLocked(str(exc)) from exc
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._tx_depth -= 1
            if self._tx_depth == 0 and self.conn.in_transaction:
                self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- helpers
    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._exec(sql, params)

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self._exec(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._exec(sql, params).fetchall()]

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self._exec(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self._exec(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values())
        )
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        self._exec(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])

    def get(self, table: str, row_id: int) -> dict[str, Any] | None:
        return self.one(f"SELECT * FROM {table} WHERE id = ?", [row_id])

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        clause = f" WHERE {where}" if where else ""
        return int(self.scalar(f"SELECT COUNT(*) FROM {table}{clause}", params, 0))

    # ------------------------------------------------------------------ audit
    def _last_hash(self) -> str:
        row = self.one("SELECT hash FROM audit_event ORDER BY id DESC LIMIT 1")
        return str(row["hash"]) if row else GENESIS

    def audit(
        self,
        action: str,
        actor: str = "system",
        subject: str = "",
        detail: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> int:
        """Append one event to the hash-chained log.  Never updates, never deletes."""
        payload = {
            "ts": to_ts(),
            "actor": str(actor),
            "action": str(action),
            "subject": str(subject),
            "detail": detail or {},
        }
        prev = self._last_hash()
        digest = hashlib.sha256(
            (prev + jdumps(payload)).encode("utf-8")
        ).hexdigest()
        owned = self._tx_depth == 0
        with self.transaction():
            row_id = self.insert(
                "audit_event",
                {
                    "ts": payload["ts"],
                    "actor": payload["actor"],
                    "action": payload["action"],
                    "subject": payload["subject"],
                    "detail": jdumps(payload["detail"]),
                    "run_id": run_id,
                    "prev_hash": prev,
                    "hash": digest,
                },
            )
        return row_id  # transaction commit handled by the context manager

    def verify_audit(self) -> dict[str, Any]:
        """Recompute the chain.  Any edit, deletion or reorder is reported."""
        prev = GENESIS
        count = 0
        for row in self.all("SELECT * FROM audit_event ORDER BY id ASC"):
            payload = {
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "subject": row["subject"],
                "detail": jloads(row["detail"], {}),
            }
            expected = hashlib.sha256((prev + jdumps(payload)).encode("utf-8")).hexdigest()
            if row["prev_hash"] != prev:
                return {"ok": False, "at": row["id"], "reason": "prev_hash mismatch", "checked": count}
            if row["hash"] != expected:
                return {"ok": False, "at": row["id"], "reason": "hash mismatch", "checked": count}
            prev = row["hash"]
            count += 1
        return {"ok": True, "at": None, "reason": "", "checked": count}

    def audit_tail(self, limit: int = 100, action: str | None = None,
                   actor: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if action:
            where.append("action = ?")
            params.append(action)
        if actor:
            where.append("actor = ?")
            params.append(actor)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.all(
            f"SELECT * FROM audit_event {clause} ORDER BY id DESC LIMIT ?", [*params, limit]
        )
        for row in rows:
            row["detail"] = jloads(row["detail"], {})
        return rows

    # -------------------------------------------------------------- settings
    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM system_setting WHERE key = ?", [key])
        return default if row is None else jloads(row["value"], default)

    def set_setting(self, key: str, value: Any, actor: str = "system") -> None:
        with self.transaction():
            self._exec(
                """INSERT INTO system_setting (key, value, updated_at, updated_by)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                     updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                [key, jdumps(value), to_ts(), actor],
            )

    def settings(self) -> dict[str, Any]:
        return {
            row["key"]: jloads(row["value"], None)
            for row in self.all("SELECT key, value FROM system_setting ORDER BY key")
        }

    # --------------------------------------------------------------- accounts
    def upsert_account(
        self,
        platform: str | Platform,
        handle: str,
        *,
        external_id: str | None = None,
        role: str = "managed",
        status: str = "unconfigured",
        scopes: Iterable[str] = (),
        token_env: str | None = None,
        token_fingerprint: str | None = None,
        token_expires_at: str | None = None,
        notes: str = "",
    ) -> int:
        now = to_ts()
        platform = str(platform)
        existing = self.one(
            "SELECT id FROM social_account WHERE platform = ? AND handle = ?",
            [platform, handle],
        )
        if existing:
            self.update(
                "social_account",
                int(existing["id"]),
                {
                    "external_id": external_id,
                    "role": role,
                    "status": status,
                    "scopes": jdumps(sorted(set(scopes))),
                    "token_env": token_env,
                    "token_fingerprint": token_fingerprint,
                    "token_expires_at": token_expires_at,
                    "notes": notes,
                    "updated_at": now,
                },
            )
            return int(existing["id"])
        return self.insert(
            "social_account",
            {
                "platform": platform,
                "handle": handle,
                "external_id": external_id,
                "role": role,
                "status": status,
                "scopes": jdumps(sorted(set(scopes))),
                "token_env": token_env,
                "token_fingerprint": token_fingerprint,
                "token_expires_at": token_expires_at,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            },
        )

    def accounts(self, platform: str | None = None) -> list[dict[str, Any]]:
        if platform:
            rows = self.all(
                "SELECT * FROM social_account WHERE platform = ? ORDER BY handle", [str(platform)]
            )
        else:
            rows = self.all("SELECT * FROM social_account ORDER BY platform, handle")
        for row in rows:
            row["scopes"] = jloads(row["scopes"], [])
        return rows

    def account_for(self, platform: str | Platform) -> dict[str, Any] | None:
        row = self.one(
            "SELECT * FROM social_account WHERE platform = ? AND role = 'managed' "
            "ORDER BY id LIMIT 1",
            [str(platform)],
        )
        if row:
            row["scopes"] = jloads(row["scopes"], [])
        return row

    # ---------------------------------------------------------------- sources
    def upsert_source(
        self,
        kind: str,
        external_ref: str,
        *,
        url: str = "",
        title: str = "",
        author: str = "",
        body: str = "",
        published_at: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        """Store a repo artifact.  Returns ``(id, changed)`` - hashing the body."""
        digest = hashlib.sha256((body or "").encode("utf-8")).hexdigest()
        existing = self.one(
            "SELECT id, digest FROM content_source WHERE kind = ? AND external_ref = ?",
            [kind, external_ref],
        )
        now = to_ts()
        if existing:
            changed = existing["digest"] != digest
            self.update(
                "content_source",
                int(existing["id"]),
                {
                    "url": url,
                    "title": title,
                    "author": author,
                    "body": body,
                    "published_at": published_at,
                    "fetched_at": now,
                    "digest": digest,
                    "meta": jdumps(meta or {}),
                },
            )
            return int(existing["id"]), changed
        row_id = self.insert(
            "content_source",
            {
                "kind": kind,
                "external_ref": external_ref,
                "url": url,
                "title": title,
                "author": author,
                "body": body,
                "published_at": published_at,
                "fetched_at": now,
                "digest": digest,
                "meta": jdumps(meta or {}),
            },
        )
        return row_id, True

    def sources(self, kind: str | None = None, limit: int = 200,
                newest_first: bool = True) -> list[dict[str, Any]]:
        where = "WHERE kind = ?" if kind else ""
        params: list[Any] = [kind] if kind else []
        order = "DESC" if newest_first else "ASC"
        rows = self.all(
            f"SELECT * FROM content_source {where} ORDER BY COALESCE(published_at, fetched_at) {order} LIMIT ?",
            [*params, limit],
        )
        for row in rows:
            row["meta"] = jloads(row["meta"], {})
        return rows

    def source_by_ref(self, external_ref: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM content_source WHERE external_ref = ?", [external_ref])
        if row:
            row["meta"] = jloads(row["meta"], {})
        return row

    # ----------------------------------------------------------- content items
    def create_item(
        self,
        title: str,
        category: str,
        *,
        summary: str = "",
        risk: str | Risk = Risk.LOW,
        state: str | EditorialState = EditorialState.DRAFT,
        facts: list[dict[str, Any]] | None = None,
        source_refs: Iterable[str] = (),
        planned_for: str | None = None,
        origin: str = "agent",
        notes: str = "",
    ) -> int:
        now = to_ts()
        return self.insert(
            "content_item",
            {
                "title": title,
                "category": str(category),
                "summary": summary,
                "risk": str(risk),
                "state": str(state),
                "facts": jdumps(facts or []),
                "source_refs": jdumps(list(source_refs)),
                "planned_for": planned_for,
                "origin": origin,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            },
        )

    def item(self, item_id: int, with_variants: bool = True) -> dict[str, Any] | None:
        row = self.get("content_item", item_id)
        if row is None:
            return None
        row["facts"] = jloads(row["facts"], [])
        row["source_refs"] = jloads(row["source_refs"], [])
        if with_variants:
            row["variants"] = self.variants_for(item_id)
        approval = self.one(
            "SELECT * FROM approval WHERE subject_type = 'content_item' AND subject_id = ? "
            "ORDER BY id DESC LIMIT 1",
            [item_id],
        )
        row["approval"] = approval
        return row

    def items(
        self,
        state: str | None = None,
        category: str | None = None,
        platform: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if state:
            states = [s.strip() for s in str(state).split(",") if s.strip()]
            where.append("i.state IN (%s)" % ", ".join("?" for _ in states))
            params.extend(states)
        if category:
            where.append("i.category = ?")
            params.append(category)
        if platform:
            where.append(
                "EXISTS (SELECT 1 FROM content_variant v WHERE v.item_id = i.id AND v.platform = ?)"
            )
            params.append(str(platform))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.all(
            f"""SELECT i.*, (SELECT COUNT(*) FROM content_variant v WHERE v.item_id = i.id) AS variant_count
                FROM content_item i {clause}
                ORDER BY COALESCE(i.planned_for, '9999') ASC, i.id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        for row in rows:
            row["facts"] = jloads(row["facts"], [])
            row["source_refs"] = jloads(row["source_refs"], [])
        return rows

    def set_item_state(self, item_id: int, state: str | EditorialState,
                       notes: str | None = None) -> None:
        values: dict[str, Any] = {"state": str(state), "updated_at": to_ts()}
        if notes is not None:
            values["notes"] = notes
        self.update("content_item", item_id, values)

    # -------------------------------------------------------------- variants
    def upsert_variant(
        self,
        item_id: int,
        platform: str | Platform,
        lang: str,
        body: str,
        *,
        hashtags: str = "",
        media: Iterable[str] = (),
        alt_text: str = "",
        generator: str = "template",
        risk: str | Risk = Risk.LOW,
        content_hash: str | None = None,
    ) -> int:
        platform, lang = str(platform), str(lang)
        digest = content_hash or hashlib.sha256(
            normalize_body(body).encode("utf-8")
        ).hexdigest()
        now = to_ts()
        existing = self.one(
            "SELECT id FROM content_variant WHERE item_id = ? AND platform = ? AND lang = ?",
            [item_id, platform, lang],
        )
        values = {
            "body": body,
            "hashtags": hashtags,
            "media": jdumps(list(media)),
            "alt_text": alt_text,
            "char_count": len(body),
            "generator": generator,
            "risk": str(risk),
            "content_hash": digest,
            "updated_at": now,
        }
        if existing:
            self.update("content_variant", int(existing["id"]), values)
            return int(existing["id"])
        return self.insert(
            "content_variant",
            {
                "item_id": item_id,
                "platform": platform,
                "lang": lang,
                "created_at": now,
                **values,
            },
        )

    def set_variant_policy(self, variant_id: int, state: str, report: dict[str, Any]) -> None:
        self.update(
            "content_variant",
            variant_id,
            {"policy_state": str(state), "policy_report": jdumps(report), "updated_at": to_ts()},
        )

    def variant(self, variant_id: int) -> dict[str, Any] | None:
        row = self.get("content_variant", variant_id)
        if row is None:
            return None
        row["media"] = jloads(row["media"], [])
        row["policy_report"] = jloads(row["policy_report"], {})
        row["item"] = self.get("content_item", int(row["item_id"]))
        if row["item"]:
            row["item"]["facts"] = jloads(row["item"]["facts"], [])
            row["item"]["source_refs"] = jloads(row["item"]["source_refs"], [])
        return row

    def variants_for(self, item_id: int) -> list[dict[str, Any]]:
        rows = self.all(
            "SELECT * FROM content_variant WHERE item_id = ? ORDER BY platform, lang",
            [item_id],
        )
        for row in rows:
            row["media"] = jloads(row["media"], [])
            row["policy_report"] = jloads(row["policy_report"], {})
        return rows

    def variants(self, platform: str | None = None, lang: str | None = None,
                 limit: int = 200) -> list[dict[str, Any]]:
        where, params = [], []
        if platform:
            where.append("v.platform = ?")
            params.append(str(platform))
        if lang:
            where.append("v.lang = ?")
            params.append(lang)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.all(
            f"SELECT v.*, i.title, i.category, i.state AS item_state, i.planned_for "
            f"FROM content_variant v JOIN content_item i ON i.id = v.item_id {clause} "
            "ORDER BY v.id DESC LIMIT ?",
            [*params, limit],
        )
        for row in rows:
            row["media"] = jloads(row["media"], [])
            row["policy_report"] = jloads(row["policy_report"], {})
        return rows

    # -------------------------------------------------------------- approvals
    def request_approval(
        self,
        subject_type: str,
        subject_id: int,
        *,
        risk: str | Risk = Risk.LOW,
        reason: str = "",
        requested_by: str = "agent",
    ) -> int:
        existing = self.one(
            "SELECT id FROM approval WHERE subject_type = ? AND subject_id = ? AND state = 'pending'",
            [subject_type, subject_id],
        )
        if existing:
            return int(existing["id"])
        approval_id = self.insert(
            "approval",
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "risk": str(risk),
                "reason": reason,
                "state": "pending",
                "requested_by": requested_by,
                "requested_at": to_ts(),
            },
        )
        self.audit(
            "approval.requested",
            actor=requested_by,
            subject=f"{subject_type}:{subject_id}",
            detail={"risk": str(risk), "reason": reason},
        )
        return approval_id

    def decide_approval(
        self,
        approval_id: int,
        decision: str,
        *,
        decided_by: str = "operator",
        note: str = "",
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected", "expired"}:
            raise ValueError(f"unknown decision {decision!r}")
        row = self.get("approval", approval_id)
        if row is None:
            raise KeyError(f"approval {approval_id} not found")
        if row["state"] != "pending":
            return row | {"unchanged": True}
        self.update(
            "approval",
            approval_id,
            {
                "state": decision,
                "decided_by": decided_by,
                "decided_at": to_ts(),
                "decision_note": note,
            },
        )
        self.audit(
            f"approval.{decision}",
            actor=decided_by,
            subject=f"{row['subject_type']}:{row['subject_id']}",
            detail={"approval_id": approval_id, "note": note},
        )
        return self.get("approval", approval_id) or row

    def approvals(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE a.state = ?" if state else ""
        params: list[Any] = [state] if state else []
        rows = self.all(
            f"""SELECT a.*, i.title AS item_title, i.category AS item_category,
                       i.risk AS item_risk, i.state AS item_state
                FROM approval a LEFT JOIN content_item i
                  ON (a.subject_type = 'content_item' AND i.id = a.subject_id)
                {where} ORDER BY a.state = 'pending' DESC, a.id DESC LIMIT ?""",
            [*params, limit],
        )
        return rows

    def approval_for(self, subject_type: str, subject_id: int) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM approval WHERE subject_type = ? AND subject_id = ? ORDER BY id DESC LIMIT 1",
            [subject_type, subject_id],
        )

    def approval_state(self, subject_type: str, subject_id: int) -> str:
        row = self.approval_for(subject_type, subject_id)
        return str(row["state"]) if row else "none"

    # -------------------------------------------------------------- scheduling
    def schedule_post(
        self,
        variant_id: int,
        run_at: str,
        *,
        platform: str | None = None,
        account_id: int | None = None,
        approval_id: int | None = None,
        max_attempts: int = 3,
    ) -> int:
        variant = self.get("content_variant", variant_id)
        if variant is None:
            raise KeyError(f"variant {variant_id} not found")
        platform = str(platform or variant["platform"])
        dedupe = f"sched:{platform}:{account_id or 0}:{variant_id}:{run_at}"
        existing = self.one("SELECT id FROM scheduled_post WHERE dedupe_key = ?", [dedupe])
        now = to_ts()
        if existing:
            return int(existing["id"])
        return self.insert(
            "scheduled_post",
            {
                "variant_id": variant_id,
                "platform": platform,
                "account_id": account_id,
                "run_at": run_at,
                "state": "pending",
                "attempts": 0,
                "max_attempts": max_attempts,
                "dedupe_key": dedupe,
                "approval_id": approval_id,
                "created_at": now,
                "updated_at": now,
            },
        )

    def scheduled(
        self,
        state: str | None = None,
        platform: str | None = None,
        limit: int = 200,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if state:
            where.append("s.state = ?")
            params.append(state)
        if platform:
            where.append("s.platform = ?")
            params.append(str(platform))
        if since:
            where.append("s.run_at >= ?")
            params.append(since)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.all(
            f"""SELECT s.*, v.body, v.lang, v.item_id, i.title, i.category, i.state AS item_state
                FROM scheduled_post s
                JOIN content_variant v ON v.id = s.variant_id
                JOIN content_item i ON i.id = v.item_id
                {clause} ORDER BY s.run_at ASC, s.id ASC LIMIT ?""",
            [*params, limit],
        )

    def claim_due_posts(
        self,
        now: str | None = None,
        *,
        limit: int = 5,
        worker: str = "worker",
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Lease due jobs atomically.  A crashed worker's lease simply expires."""
        now = now or to_ts()
        with self.transaction():
            rows = self.all(
                """SELECT * FROM scheduled_post
                   WHERE state = 'pending' AND run_at <= ?
                   ORDER BY run_at ASC LIMIT ?""",
                [now, limit],
            )
            if not rows:
                return []
            ids = [int(r["id"]) for r in rows]
            marks = ", ".join("?" for _ in ids)
            self._exec(
                f"""UPDATE scheduled_post
                    SET state = 'leased', leased_at = ?, leased_by = ?, attempts = attempts + 1,
                        updated_at = ?
                    WHERE id IN ({marks}) AND state = 'pending'""",
                [now, worker, now, *ids],
            )
        claimed = []
        for row in rows:
            fresh = self.get("scheduled_post", int(row["id"]))
            if fresh and fresh["state"] == "leased" and fresh["leased_by"] == worker:
                claimed.append(fresh)
        return claimed

    def release_expired_leases(self, now: str | None = None, lease_seconds: int = 900) -> int:
        now_dt = parse_ts(now) or utcnow()
        cutoff = to_ts(now_dt - timedelta(seconds=lease_seconds))
        with self.transaction():
            cur = self._exec(
                """UPDATE scheduled_post SET state = 'pending', leased_at = NULL, leased_by = NULL,
                          updated_at = ?
                   WHERE state = 'leased' AND COALESCE(leased_at, created_at) < ?""",
                [to_ts(now_dt), cutoff],
            )
            return int(cur.rowcount or 0)

    def finish_post(
        self,
        scheduled_id: int,
        *,
        state: str,
        error: str = "",
    ) -> None:
        row = self.get("scheduled_post", scheduled_id)
        if row is None:
            return
        attempts = int(row["attempts"] or 0)
        max_attempts = int(row["max_attempts"] or 3)
        if state == "failed" and attempts < max_attempts:
            self.update(
                "scheduled_post",
                scheduled_id,
                {
                    "state": "pending",
                    "last_error": error[:500],
                    "leased_at": None,
                    "leased_by": None,
                    "updated_at": to_ts(),
                },
            )
            return
        self.update(
            "scheduled_post",
            scheduled_id,
            {
                "state": state,
                "last_error": error[:500],
                "leased_at": None,
                "leased_by": None,
                "updated_at": to_ts(),
            },
        )

    def published_posts(self, platform: str | None = None, limit: int = 100,
                        state: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if platform:
            where.append("p.platform = ?")
            params.append(str(platform))
        if state:
            where.append("p.state = ?")
            params.append(str(state))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.all(
            f"""SELECT p.*, v.body, v.lang, v.item_id, i.title, i.category
                FROM published_post p
                LEFT JOIN content_variant v ON v.id = p.variant_id
                LEFT JOIN content_item i ON i.id = v.item_id
                {clause} ORDER BY p.published_at DESC, p.id DESC LIMIT ?""",
            [*params, limit],
        )
        for row in rows:
            row["meta"] = jloads(row["meta"], {})
        return rows

    # --------------------------------------------------------------- feedback
    def upsert_feedback(
        self,
        platform: str | Platform,
        external_ref: str,
        body: str,
        *,
        url: str = "",
        author: str = "",
        lang: str | None = None,
        created_at_platform: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        existing = self.one(
            "SELECT id FROM feedback_item WHERE platform = ? AND external_ref = ?",
            [str(platform), external_ref],
        )
        if existing:
            return int(existing["id"]), False
        return (
            self.insert(
                "feedback_item",
                {
                    "platform": str(platform),
                    "external_ref": external_ref,
                    "url": url,
                    "author": author,
                    "body": body,
                    "lang": lang,
                    "created_at_platform": created_at_platform,
                    "ingested_at": to_ts(),
                    "meta": jdumps(meta or {}),
                },
            ),
            True,
        )

    def classify_feedback(
        self,
        feedback_id: int,
        *,
        category: str,
        priority: str,
        summary: str,
        sentiment: str | None = None,
        cluster_key: str = "",
        state: str = "triaged",
    ) -> None:
        self.update(
            "feedback_item",
            feedback_id,
            {
                "category": category,
                "priority": priority,
                "summary": summary,
                "sentiment": sentiment,
                "cluster_key": cluster_key,
                "state": state,
            },
        )

    def feedback(self, category: str | None = None, state: str | None = None,
                 limit: int = 200) -> list[dict[str, Any]]:
        where, params = [], []
        if category:
            where.append("category = ?")
            params.append(category)
        if state:
            where.append("state = ?")
            params.append(state)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.all(
            f"SELECT * FROM feedback_item {clause} ORDER BY id DESC LIMIT ?", [*params, limit]
        )
        for row in rows:
            row["meta"] = jloads(row["meta"], {})
        return rows

    def feedback_clusters(self, min_size: int = 2) -> list[dict[str, Any]]:
        return self.all(
            """SELECT cluster_key, category, COUNT(*) AS n,
                      MIN(id) AS first_id, MAX(id) AS last_id
               FROM feedback_item WHERE cluster_key <> ''
               GROUP BY cluster_key HAVING COUNT(*) >= ?
               ORDER BY n DESC""",
            [max(1, min_size)],
        )

    # ------------------------------------------------------------- telemetry
    def start_run(self, run_id: str, agent: str, command: str = "") -> None:
        self.insert(
            "agent_run",
            {
                "id": run_id,
                "agent": agent,
                "command": command,
                "started_at": to_ts(),
                "state": "running",
            },
        )

    def finish_run(self, run_id: str, *, state: str = "ok", summary: str = "",
                   detail: dict[str, Any] | None = None, items: int = 0) -> None:
        self._exec(
            """UPDATE agent_run SET finished_at = ?, state = ?, summary = ?, detail = ?, items = ?
               WHERE id = ?""",
            [to_ts(), state, summary, jdumps(detail or {}), int(items), run_id],
        )

    def runs(self, limit: int = 50, agent: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE agent = ?" if agent else ""
        params: list[Any] = [agent] if agent else []
        rows = self.all(
            f"SELECT * FROM agent_run {where} ORDER BY started_at DESC LIMIT ?",
            [*params, limit],
        )
        for row in rows:
            row["detail"] = jloads(row["detail"], {})
        return rows

    def record_api_call(
        self,
        connector: str,
        method: str,
        endpoint: str,
        *,
        status: int = 0,
        duration_ms: int = 0,
        run_id: str | None = None,
        error: str = "",
        meta: dict[str, Any] | None = None,
    ) -> int:
        return self.insert(
            "api_call",
            {
                "connector": connector,
                "method": method.upper(),
                "endpoint": endpoint.split("?")[0][:300],
                "status": int(status),
                "ok": 1 if 200 <= int(status) < 300 else 0,
                "duration_ms": int(duration_ms),
                "run_id": run_id,
                "error": error[:300],
                "called_at": to_ts(),
                "meta": jdumps(meta or {}),
            },
        )

    def api_calls(self, limit: int = 100, connector: str | None = None,
                  only_errors: bool = False) -> list[dict[str, Any]]:
        where, params = [], []
        if connector:
            where.append("connector = ?")
            params.append(str(connector))
        if only_errors:
            where.append("ok = 0")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.all(
            f"SELECT * FROM api_call {clause} ORDER BY id DESC LIMIT ?", [*params, limit]
        )

    def record_metric(
        self,
        platform: str | Platform,
        subject_ref: str,
        metric: str,
        value: float,
        *,
        captured_at: str | None = None,
        source: str = "api",
        available: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._exec(
            """INSERT INTO analytics_snapshot
                 (platform, subject_ref, metric, value, captured_at, source, available, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, subject_ref, metric, captured_at)
               DO UPDATE SET value = excluded.value, source = excluded.source,
                             available = excluded.available, meta = excluded.meta""",
            [
                str(platform),
                subject_ref,
                metric,
                float(value),
                captured_at or to_ts(),
                source,
                1 if available else 0,
                jdumps(meta or {}),
            ],
        )

    def metrics(self, platform: str | None = None, subject_ref: str | None = None,
                limit: int = 500) -> list[dict[str, Any]]:
        where, params = [], []
        if platform:
            where.append("platform = ?")
            params.append(str(platform))
        if subject_ref:
            where.append("subject_ref = ?")
            params.append(subject_ref)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.all(
            f"SELECT * FROM analytics_snapshot {clause} ORDER BY captured_at DESC LIMIT ?",
            [*params, limit],
        )
        for row in rows:
            row["meta"] = jloads(row["meta"], {})
        return rows

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        """Counters the Overview page shows - and nothing that is not stored."""
        by_state = {
            row["state"]: row["n"]
            for row in self.all("SELECT state, COUNT(*) AS n FROM content_item GROUP BY state")
        }
        by_platform = {
            row["platform"]: row["n"]
            for row in self.all(
                "SELECT platform, COUNT(*) AS n FROM published_post WHERE state <> 'dry_run' "
                "GROUP BY platform"
            )
        }
        dry_run = int(self.scalar(
            "SELECT COUNT(*) FROM published_post WHERE state = 'dry_run'", (), 0))
        return {
            "items": sum(by_state.values()),
            "items_by_state": by_state,
            "variants": self.count("content_variant"),
            "pending_approvals": self.count("approval", "state = 'pending'"),
            "scheduled_pending": self.count("scheduled_post", "state IN ('pending','leased')"),
            "published": sum(by_platform.values()),
            "published_by_platform": by_platform,
            "dry_run_posts": dry_run,
            "sources": self.count("content_source"),
            "feedback": self.count("feedback_item"),
            "feedback_new": self.count("feedback_item", "state = 'new'"),
            "runs": self.count("agent_run"),
            "api_calls": self.count("api_call"),
            "api_errors": self.count("api_call", "ok = 0"),
            "audit_events": self.count("audit_event"),
            "generated_at": to_ts(),
        }

    def published_today(self, platform: str | Platform, day_start: str) -> int:
        return int(self.scalar(
            """SELECT COUNT(*) FROM published_post
               WHERE platform = ? AND state <> 'dry_run' AND published_at >= ?""",
            [str(platform), day_start],
            0,
        ))

    def last_published_at(self, platform: str | Platform) -> str | None:
        return self.scalar(
            """SELECT MAX(published_at) FROM published_post
               WHERE platform = ? AND state <> 'dry_run'""",
            [str(platform)],
        )

    def recent_variant_hashes(self, platform: str, days: int = 30) -> set[str]:
        cutoff = to_ts(utcnow() - timedelta(days=days))
        rows = self.all(
            """SELECT DISTINCT p.content_hash AS h FROM published_post p
               WHERE p.platform = ? AND p.published_at >= ?
               UNION
               SELECT DISTINCT v.content_hash AS h FROM content_variant v
               WHERE v.platform = ? AND v.updated_at >= ?""",
            [str(platform), cutoff, str(platform), cutoff],
        )
        return {str(r["h"]) for r in rows}

    # ---------------------------------------------------------------- upkeep
    def prune(self, retention_days: dict[str, int] | None = None) -> dict[str, int]:
        """Delete rows past their retention.  ``audit_event`` is never pruned."""
        retention = retention_days or {}
        removed: dict[str, int] = {}
        with self.transaction():
            for table, column in (("api_call", "called_at"), ("agent_run", "started_at")):
                days = int(retention.get(table, 0) or 0)
                if days <= 0:
                    continue
                cutoff = to_ts(utcnow() - timedelta(days=days))
                cur = self._exec(f"DELETE FROM {table} WHERE {column} < ?", [cutoff])
                removed[table] = int(cur.rowcount or 0)
        return removed

    def backup(self, directory: str | Path | None = None) -> Path:
        """A consistent copy via SQLite's own backup API (safe while running)."""
        if self.path == ":memory:":
            raise ValueError("cannot back up an in-memory database")
        target_dir = Path(directory or (Path(self.path).parent / "backups"))
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        target = target_dir / f"{Path(self.path).stem}-{stamp}.db"
        # The copy holds exactly what the store holds, so it gets the same owner-only mode.
        touch_private(target)
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        self.audit("db.backup", actor="system", subject=str(target), detail={"path": str(target)})
        return target

    def health(self) -> dict[str, Any]:
        audit = self.verify_audit()
        return {
            "database": self.path,
            "schema_version": int(self.conn.execute("PRAGMA user_version").fetchone()[0]),
            "wal": self.scalar("PRAGMA journal_mode", (), "unknown"),
            "file_mode": self.file_mode(),
            "permissions": self.permissions_summary(),
            "audit_chain": audit,
            "tables": int(self.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'", (), 0)),
            "checked_at": to_ts(),
        }


def open_default(path: str | Path = "state/social.db") -> SocialDB:  # pragma: no cover
    return SocialDB.open(path)


def now_plus(minutes: int = 0, seconds: int = 0) -> str:
    return to_ts(utcnow() + timedelta(minutes=minutes, seconds=seconds))


def day_start_utc(day_offset: int = 0) -> str:
    dt = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return to_ts(dt + timedelta(days=day_offset))


def sleep_ms(ms: int) -> None:  # pragma: no cover - used by the worker loop
    time.sleep(ms / 1000.0)
