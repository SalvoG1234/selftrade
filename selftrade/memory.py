"""Persistent memory in a single SQLite file.

Tables: ``runs``, ``candidates``, ``hall_of_fame``, ``lessons``, the paper trading
trio ``paper_sessions`` / ``paper_trades`` / ``paper_equity``, the risk and provider
logs ``risk_events`` / ``api_calls``, and ``strategy_versions`` - the lineage of a
strategy (parent -> candidate) with its stored diff and the reason it was kept.

A run also carries its *provenance*: the commit, the dataset hash and the resulting
``repro_id`` (:mod:`selftrade.repro`), so a fitness can be traced back to the code
and the prices that produced it.

The schema is created (and migrated) on open.  Nothing is destroyed without an
explicit command (``paper --reset``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 3


class StorageError(RuntimeError):
    """The memory store cannot be opened, or the file is not a database.

    Raised instead of a bare ``sqlite3.DatabaseError`` so every front end - the CLI, the
    dashboard, a script - says which file failed and what to do about it.  ``doctor`` answers
    the same question without raising.
    """


def _unusable(db_path: str, exc: Exception) -> str:
    """The sentence an operator gets when the store cannot be read."""
    path = Path(db_path)
    if path.is_dir():
        why = "that path is a directory"
    else:
        why = str(exc)
    return (
        f"the memory store at {db_path} could not be opened ({why}). Nothing was changed: "
        "move the file aside to start a new one (state/selftrade.db.broken) or point "
        "`memory.db` in config.yaml at another path. `python -m selftrade doctor` reports "
        "which store the program is trying to use."
    )

#: The only kinds a ``cash_ledger`` row may carry.  The sign of ``amount`` is always the
#: effect on the account's **free** balance: a deposit and a release add to it, a
#: withdrawal and an allocation take from it.  One number, one meaning.
LEDGER_KINDS = ("deposit", "withdrawal", "allocation", "release")

#: Columns added to ``runs`` after schema v1; applied by :meth:`Memory.init_schema`
#: so an existing database keeps its history instead of needing a rebuild.
_RUN_COLUMNS: dict[str, str] = {
    "commit_sha": "TEXT",
    "code_version": "TEXT",
    "dataset_sha": "TEXT",
    "dataset_rows": "INTEGER",
    "repro_id": "TEXT",
    "reproducible": "INTEGER",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    note TEXT,
    config TEXT,
    summary TEXT,
    outcome TEXT,
    commit_sha TEXT,
    code_version TEXT,
    dataset_sha TEXT,
    dataset_rows INTEGER,
    repro_id TEXT,
    reproducible INTEGER
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    generation INTEGER,
    family TEXT,
    genome TEXT NOT NULL,
    origin TEXT,
    fold_metrics TEXT,
    fitness REAL,
    passed INTEGER,
    reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hall_of_fame (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    family TEXT,
    genome TEXT NOT NULL,
    fitness REAL,
    metrics_oos TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    text TEXT NOT NULL,
    tags TEXT,
    question TEXT,
    target TEXT,
    embedding TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    genome TEXT NOT NULL,
    window TEXT,
    status TEXT,
    cursor INTEGER DEFAULT 0,
    total_bars INTEGER,
    start_time TEXT,
    end_time TEXT,
    cash REAL,
    initial_cash REAL,
    settings TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    direction TEXT,
    entry_time TEXT,
    exit_time TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    return_pct REAL,
    bars_held INTEGER,
    exit_reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT,
    reason TEXT,
    source TEXT,
    session_id INTEGER,
    balance_after REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_kind ON cash_ledger(kind);
CREATE INDEX IF NOT EXISTS idx_ledger_session ON cash_ledger(session_id);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    provider TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    params TEXT,
    status INTEGER,
    ok INTEGER NOT NULL,
    code TEXT,
    msg TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    response TEXT,
    note TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_hall_fitness ON hall_of_fame(fitness DESC);
CREATE INDEX IF NOT EXISTS idx_lessons_run ON lessons(run_id);
CREATE INDEX IF NOT EXISTS idx_ptrades_session ON paper_trades(session_id);
CREATE INDEX IF NOT EXISTS idx_pequity_session ON paper_equity(session_id);
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    generation INTEGER,
    version INTEGER,
    family TEXT,
    parent_family TEXT,
    origin TEXT,
    genome TEXT NOT NULL,
    parent_genome TEXT,
    genome_hash TEXT,
    parent_hash TEXT,
    diff TEXT,
    rationale TEXT,
    reason TEXT,
    fitness REAL,
    parent_fitness REAL,
    delta_fitness REAL,
    passed INTEGER,
    outcome TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_events_created ON risk_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_calls_created ON api_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_run ON strategy_versions(run_id);
CREATE INDEX IF NOT EXISTS idx_versions_hash ON strategy_versions(genome_hash);
"""


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _genome_key(genome: Any) -> str | None:
    """Canonical genome hash, or ``None`` when the value is not a genome.

    Imported lazily: ``memory`` stays usable on its own (an old database, a small
    script) and a bad genome degrades to ``NULL`` instead of raising.
    """
    if not isinstance(genome, dict):
        return None
    try:
        from .genome import genome_key

        return genome_key(genome)
    except Exception:  # noqa: BLE001 - a hash must never break a write
        return None


def _safe_diff(parent: Any, genome: Any) -> list[dict[str, Any]]:
    try:
        from .genome import genome_diff

        return genome_diff(parent, genome)
    except Exception:  # noqa: BLE001
        return []


class Memory:
    """Thin, thread-safe-enough SQLite wrapper (one connection per call)."""

    def __init__(self, db_path: str | Path = "state/selftrade.db"):
        self.db_path = str(db_path)
        p = Path(self.db_path)
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.init_schema()
        except sqlite3.DatabaseError as exc:
            # SQLite opens lazily: `connect` succeeds on any file and the first statement is
            # where "file is not a database" appears, so the schema step is what gets wrapped.
            raise StorageError(_unusable(self.db_path, exc)) from exc

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -------------------------------------------------- schema
    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                conn.execute(
                    "UPDATE schema_meta SET value=? WHERE key='version'",
                    (str(SCHEMA_VERSION),),
                )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the columns a v1 database is missing, without touching its rows."""
        have = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        for name, column_type in _RUN_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {column_type}")

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    # -------------------------------------------------- runs
    def start_run(
        self, note: str = "", config: Any = None, repro: dict[str, Any] | None = None
    ) -> int:
        """Open a run and stamp it with its provenance (commit, dataset, ``repro_id``).

        ``repro`` is the record from :func:`selftrade.repro.fingerprint`; a caller with
        no fingerprint (an old script, a test) simply stores ``NULL`` and the page
        reads ``N/D`` rather than inventing a commit.
        """
        repro = repro or {}
        dataset = repro.get("dataset") or {}
        commit = repro.get("commit")
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO runs
                   (ts, note, config, summary, outcome, commit_sha, code_version,
                    dataset_sha, dataset_rows, repro_id, reproducible)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(),
                    note,
                    _json_or_none(config),
                    None,
                    "running",
                    commit,
                    repro.get("commit_short") or (commit[:12] if commit else None),
                    dataset.get("sha256"),
                    dataset.get("rows"),
                    repro.get("repro_id"),
                    None if repro.get("repro_id") is None else int(bool(repro.get("reproducible"))),
                ),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: Any, outcome: str = "ok") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET summary=?, outcome=? WHERE id=?",
                (_json_or_none(summary), outcome, run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return _run_row(row) if row else None

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run_row(row) if row else None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_run_row(r) for r in rows]

    # -------------------------------------------------- candidates
    def add_candidate(
        self,
        run_id: int | None,
        generation: int | None,
        genome: dict[str, Any],
        origin: str,
        fold_metrics: list[dict[str, Any]] | None,
        fitness: float,
        passed: bool,
        reason: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO candidates
                   (run_id, generation, family, genome, origin, fold_metrics, fitness, passed, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    generation,
                    genome.get("family"),
                    _json_or_none(genome),
                    origin,
                    _json_or_none(fold_metrics),
                    float(fitness),
                    int(bool(passed)),
                    reason,
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def candidates_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE run_id=? ORDER BY generation, id",
                (run_id,),
            ).fetchall()
        return [_candidate_row(r) for r in rows]

    def recent_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_candidate_row(r) for r in rows]

    # -------------------------------------------------- strategy versions
    def add_strategy_version(
        self,
        run_id: int | None,
        *,
        genome: dict[str, Any],
        parent: dict[str, Any] | None = None,
        generation: int | None = None,
        version: int | None = None,
        origin: str = "",
        diff: Any = None,
        rationale: str = "",
        reason: str = "",
        fitness: float | None = None,
        parent_fitness: float | None = None,
        passed: bool = False,
        outcome: str = "",
    ) -> int:
        """Record one strategy version: the candidate, its parent and what changed.

        This is the audit trail the search used to keep only in the report.  The diff is
        the :func:`selftrade.genome.genome_diff` output against the *actual* parent the
        proposal was derived from, so "mutation of the champion" and "fresh genome"
        stay distinguishable.
        """
        genome_hash = _genome_key(genome)
        parent_hash = _genome_key(parent) if parent else None
        if diff is None and parent:
            diff = _safe_diff(parent, genome)
        delta = None
        if fitness is not None and parent_fitness is not None:
            delta = float(fitness) - float(parent_fitness)
        family = (genome or {}).get("family")
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO strategy_versions
                   (run_id, generation, version, family, parent_family, origin, genome,
                    parent_genome, genome_hash, parent_hash, diff, rationale, reason,
                    fitness, parent_fitness, delta_fitness, passed, outcome, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    None if generation is None else int(generation),
                    None if version is None else int(version),
                    family,
                    (parent or {}).get("family") if parent else None,
                    str(origin or ""),
                    _json_or_none(genome),
                    _json_or_none(parent) if parent else None,
                    genome_hash,
                    parent_hash,
                    _json_or_none(diff) if diff is not None else None,
                    str(rationale or ""),
                    str(reason or ""),
                    None if fitness is None else float(fitness),
                    None if parent_fitness is None else float(parent_fitness),
                    delta,
                    int(bool(passed)),
                    str(outcome or ""),
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def set_version_outcome(self, version_id: int, outcome: str) -> None:
        """Re-label a recorded version (a candidate becomes the run's champion)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE strategy_versions SET outcome=? WHERE id=?",
                (str(outcome), int(version_id)),
            )

    def strategy_versions(
        self, run_id: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """The version history, oldest first - a lineage reads top to bottom."""
        q = "SELECT * FROM strategy_versions"
        args: tuple[Any, ...] = ()
        if run_id is not None:
            q += " WHERE run_id=?"
            args = (int(run_id),)
        q += " ORDER BY id ASC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(q, (*args, int(limit))).fetchall()
        return [_version_row(r) for r in rows]

    def latest_strategy_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        """The most recently recorded versions across runs, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_versions ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_version_row(r) for r in rows]

    # -------------------------------------------------- hall of fame
    def add_to_hall(
        self, run_id: int | None, genome: dict[str, Any], fitness: float, metrics_oos: Any
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO hall_of_fame(run_id, family, genome, fitness, metrics_oos, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    run_id,
                    genome.get("family"),
                    _json_or_none(genome),
                    float(fitness),
                    _json_or_none(metrics_oos),
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def hall_of_fame(self, limit: int = 50, min_fitness: float | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM hall_of_fame"
        args: list[Any] = []
        if min_fitness is not None:
            q += " WHERE fitness >= ?"
            args.append(float(min_fitness))
        q += " ORDER BY fitness DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(q, args).fetchall()
        return [_hall_row(r) for r in rows]

    def champion(self) -> dict[str, Any] | None:
        """Best genome in the hall of fame (highest fitness)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM hall_of_fame ORDER BY fitness DESC, id ASC LIMIT 1"
            ).fetchone()
        return _hall_row(row) if row else None

    # -------------------------------------------------- lessons
    def add_lesson(
        self,
        run_id: int | None,
        text: str,
        tags: Any = None,
        question: str | None = None,
        target: str | None = None,
        embedding: Any = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO lessons(run_id, text, tags, question, target, embedding, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    run_id,
                    text,
                    _json_or_none(tags),
                    question,
                    target,
                    _json_or_none(embedding),
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def lessons(self, run_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if run_id is None:
                rows = conn.execute(
                    "SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM lessons WHERE run_id=? ORDER BY id DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
        return [_lesson_row(r) for r in rows]

    def lessons_with_embeddings(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM lessons WHERE embedding IS NOT NULL ORDER BY id DESC"
            ).fetchall()
        return [r for r in (_lesson_row(row) for row in rows) if r["embedding"]]

    # -------------------------------------------------- risk events
    def add_risk_event(
        self,
        run_id: int | None,
        kind: str,
        reason: str,
        detail: Any = None,
    ) -> int:
        """Record a risk decision: a blocked order, a kill switch, a limit breach."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO risk_events(run_id, kind, reason, detail, created_at)
                   VALUES (?,?,?,?,?)""",
                (run_id, str(kind), str(reason), _json_or_none(detail), time.time()),
            )
            return int(cur.lastrowid)

    def risk_events(self, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        """Most recent risk events first."""
        q = "SELECT * FROM risk_events"
        args: tuple[Any, ...] = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(q, (*args, int(limit))).fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "kind": r["kind"],
                "reason": r["reason"],
                "detail": _loads(r["detail"], None),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # -------------------------------------------------- api calls
    def add_api_call(
        self,
        run_id: int | None,
        *,
        provider: str,
        method: str,
        path: str,
        params: Any = None,
        status: int | None = None,
        ok: bool = True,
        code: Any = None,
        msg: str = "",
        dry_run: bool = False,
        latency_ms: float | None = None,
        response: Any = None,
        note: str = "",
    ) -> int:
        """Record one provider call: request, status, error code, response.

        Secrets are the caller's responsibility: the signature and the API key are
        never passed here (a test asserts it for the Binance rail).
        """
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO api_calls
                   (run_id, provider, method, path, params, status, ok, code, msg,
                    dry_run, latency_ms, response, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    str(provider),
                    str(method),
                    str(path),
                    _json_or_none(params),
                    None if status is None else int(status),
                    int(bool(ok)),
                    None if code is None else str(code),
                    str(msg or ""),
                    int(bool(dry_run)),
                    None if latency_ms is None else float(latency_ms),
                    _json_or_none(response),
                    str(note or ""),
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def api_calls(self, limit: int = 100, provider: str | None = None) -> list[dict[str, Any]]:
        """Most recent provider calls first."""
        q = "SELECT * FROM api_calls"
        args: tuple[Any, ...] = ()
        if provider:
            q += " WHERE provider=?"
            args = (str(provider),)
        q += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(q, (*args, int(limit))).fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "provider": r["provider"],
                "method": r["method"],
                "path": r["path"],
                "params": _loads(r["params"], {}),
                "status": r["status"],
                "ok": bool(r["ok"]),
                "code": r["code"],
                "msg": r["msg"],
                "dry_run": bool(r["dry_run"]),
                "latency_ms": r["latency_ms"],
                "response": _loads(r["response"], None),
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # -------------------------------------------------- paper sessions
    def create_paper_session(
        self,
        *,
        name: str,
        genome: dict[str, Any],
        window: str,
        total_bars: int,
        start_time: str | None,
        end_time: str | None,
        initial_cash: float,
        settings: Any = None,
    ) -> int:
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO paper_sessions
                   (name, genome, window, status, cursor, total_bars, start_time, end_time,
                    cash, initial_cash, settings, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    name,
                    _json_or_none(genome),
                    window,
                    "active",
                    0,
                    int(total_bars),
                    start_time,
                    end_time,
                    float(initial_cash),
                    float(initial_cash),
                    _json_or_none(settings),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_paper_session(self, session_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return _paper_session_row(row) if row else None

    def active_paper_session(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _paper_session_row(row) if row else None

    def update_paper_cursor(
        self, session_id: int, cursor: int, cash: float, status: str | None = None
    ) -> None:
        with self._conn() as conn:
            if status is None:
                conn.execute(
                    "UPDATE paper_sessions SET cursor=?, cash=?, updated_at=? WHERE id=?",
                    (int(cursor), float(cash), time.time(), session_id),
                )
            else:
                conn.execute(
                    "UPDATE paper_sessions SET cursor=?, cash=?, status=?, updated_at=? WHERE id=?",
                    (int(cursor), float(cash), status, time.time(), session_id),
                )

    def add_paper_trades(self, session_id: int, trades: list[dict[str, Any]]) -> int:
        if not trades:
            return 0
        now = time.time()
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO paper_trades
                   (session_id, direction, entry_time, exit_time, entry_price, exit_price,
                    size, pnl, return_pct, bars_held, exit_reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        session_id,
                        t.get("direction"),
                        str(t.get("entry_time")),
                        str(t.get("exit_time")),
                        float(t.get("entry_price", 0.0)),
                        float(t.get("exit_price", 0.0)),
                        float(t.get("size", 0.0)),
                        float(t.get("pnl", 0.0)),
                        float(t.get("return_pct", 0.0)),
                        int(t.get("bars_held", 0)),
                        str(t.get("exit_reason", "")),
                        now,
                    )
                    for t in trades
                ],
            )
        return len(trades)

    def paper_trades(self, session_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def add_paper_equity(self, session_id: int, points: list[tuple[str, float]]) -> int:
        if not points:
            return 0
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO paper_equity(session_id, ts, equity) VALUES (?,?,?)",
                [(session_id, str(ts), float(eq)) for ts, eq in points],
            )
        return len(points)

    def paper_equity(self, session_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, equity FROM paper_equity WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def replace_paper_results(
        self,
        session_id: int,
        trades: list[dict[str, Any]],
        equity: list[tuple[str, float]],
        cursor: int,
        cash: float,
        status: str | None = None,
    ) -> None:
        """Idempotent rewrite of the session's derived results."""
        with self._conn() as conn:
            conn.execute("DELETE FROM paper_trades WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM paper_equity WHERE session_id=?", (session_id,))
        self.add_paper_trades(session_id, trades)
        self.add_paper_equity(session_id, equity)
        self.update_paper_cursor(session_id, cursor, cash, status)

    def reset_paper_session(self, session_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM paper_trades WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM paper_equity WHERE session_id=?", (session_id,))
            conn.execute(
                "UPDATE paper_sessions SET cursor=0, cash=initial_cash, status='active', updated_at=? WHERE id=?",
                (time.time(), session_id),
            )

    def paper_sessions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM paper_sessions ORDER BY id").fetchall()
        return [_paper_session_row(r) for r in rows]

    def paper_session_started(self, session_id: int) -> bool:
        """True once a session has advanced its cursor or booked a trade.

        The capital layer asks this before it re-bases a session, so the refusal can name
        the reason instead of just failing.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT (SELECT COUNT(*) FROM paper_trades WHERE session_id=?) AS trades,
                          (SELECT COALESCE(cursor, 0) FROM paper_sessions WHERE id=?) AS cursor""",
                (int(session_id), int(session_id)),
            ).fetchone()
        return bool(row["trades"]) or int(row["cursor"] or 0) > 0

    # -------------------------------------------------- capital ledger
    def add_ledger_entry(
        self,
        *,
        kind: str,
        amount: float,
        currency: str = "USDT",
        reason: str = "",
        source: str = "",
        session_id: int | None = None,
    ) -> dict[str, Any]:
        """Append one cash movement and return it with the balance it left behind.

        The ledger is **append-only**: no row is ever updated or deleted, so the account
        balance is always the sum of what was recorded and a mistake is corrected by a new
        entry with a reason, never by rewriting history.  The balance is read and the row
        written inside one connection, so two credits cannot both claim the balance they
        saw.
        """
        if kind not in LEDGER_KINDS:
            raise ValueError(
                f"unknown ledger kind {kind!r}; use one of {', '.join(LEDGER_KINDS)}"
            )
        now = time.time()
        with self._conn() as conn:
            self._begin_immediate(conn)
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0.0) AS balance FROM cash_ledger"
            ).fetchone()
            balance = float(row["balance"] or 0.0) + float(amount)
            cur = conn.execute(
                """INSERT INTO cash_ledger
                   (ts, kind, amount, currency, reason, source, session_id, balance_after,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    now,
                    kind,
                    float(amount),
                    currency or None,
                    reason or None,
                    source or None,
                    None if session_id is None else int(session_id),
                    balance,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid)
        return {
            "id": entry_id,
            "ts": now,
            "kind": kind,
            "amount": float(amount),
            "currency": currency or None,
            "reason": reason or None,
            "source": source or None,
            "session_id": None if session_id is None else int(session_id),
            "balance_after": balance,
            "created_at": now,
        }

    def ledger_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest ledger rows, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cash_ledger ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_ledger_row(r) for r in rows]

    def ledger_totals(self) -> dict[str, Any]:
        """The account in one read: free balance, credited, withdrawn, allocated."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT kind, COALESCE(SUM(amount), 0.0) AS total, COUNT(*) AS n
                   FROM cash_ledger GROUP BY kind"""
            ).fetchall()
            net = conn.execute(
                "SELECT COALESCE(SUM(amount), 0.0) AS balance FROM cash_ledger"
            ).fetchone()
        by_kind = {r["kind"]: float(r["total"] or 0.0) for r in rows}
        counts = {r["kind"]: int(r["n"]) for r in rows}
        return {
            "balance": float(net["balance"] or 0.0),
            "credited": by_kind.get("deposit", 0.0) + 0.0,
            # Reported as positive magnitudes; `+ 0.0` keeps an empty ledger from
            # reading "-0.00".
            "withdrawn": -by_kind.get("withdrawal", 0.0) + 0.0,
            "allocated": -(by_kind.get("allocation", 0.0) + by_kind.get("release", 0.0)) + 0.0,
            "entries": sum(counts.values()),
            "counts": counts,
        }

    def session_allocations(self) -> dict[int, float]:
        """Net capital allocated to each session, keyed by session id."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT session_id, COALESCE(SUM(amount), 0.0) AS total
                   FROM cash_ledger
                   WHERE session_id IS NOT NULL AND kind IN ('allocation','release')
                   GROUP BY session_id"""
            ).fetchall()
        return {int(r["session_id"]): -float(r["total"] or 0.0) for r in rows}

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> None:
        """Take the write lock before reading a balance that a write will depend on.

        Two credits that read the same balance and then both insert would leave two rows
        claiming the same ``balance_after``.  ``BEGIN IMMEDIATE`` serialises them: the
        second waits, then reads the balance the first one wrote.  Called inside an open
        transaction it is a no-op.
        """
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:  # pragma: no cover - already in a transaction
            pass

    def allocate_capital(
        self,
        *,
        session_id: int,
        amount: float,
        new_capital: float,
        currency: str = "",
        reason: str = "",
        source: str = "",
        kind: str = "allocation",
    ) -> dict[str, Any] | None:
        """Move capital between the account and a session, in **one transaction**.

        The guard ("the session must not have traded"), the ledger row and the session's
        new capital are written together: a session cannot start trading between the check
        and the write, and a ledger row never exists without the capital it describes (or
        the other way round).  Returns ``None`` - writing nothing - when the guard fails.

        ``amount`` is the signed effect on the **free** balance, ``new_capital`` the
        session's total capital after this movement.
        """
        if kind not in ("allocation", "release"):
            raise ValueError(f"allocate_capital records an allocation or a release, not {kind!r}")
        now = time.time()
        with self._conn() as conn:
            self._begin_immediate(conn)
            guard = conn.execute(
                """SELECT (SELECT COUNT(*) FROM paper_trades WHERE session_id=?) AS trades,
                          (SELECT cursor FROM paper_sessions WHERE id=?) AS cursor""",
                (int(session_id), int(session_id)),
            ).fetchone()
            if guard is None or guard["cursor"] is None:
                return None
            if int(guard["cursor"] or 0) > 0 or int(guard["trades"] or 0) > 0:
                return None
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0.0) AS balance FROM cash_ledger"
            ).fetchone()
            balance = float(row["balance"] or 0.0) + float(amount)
            cur = conn.execute(
                """INSERT INTO cash_ledger
                   (ts, kind, amount, currency, reason, source, session_id, balance_after,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    now,
                    kind,
                    float(amount),
                    currency or None,
                    reason or None,
                    source or None,
                    int(session_id),
                    balance,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE paper_sessions SET initial_cash=?, cash=?, updated_at=? WHERE id=?",
                (float(new_capital), float(new_capital), now, int(session_id)),
            )
        return {
            "id": entry_id,
            "ts": now,
            "kind": kind,
            "amount": float(amount),
            "currency": currency or None,
            "reason": reason or None,
            "source": source or None,
            "session_id": int(session_id),
            "balance_after": balance,
            "created_at": now,
        }

    # -------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            def count(table: str) -> int:
                return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])

            out = {
                "runs": count("runs"),
                "candidates": count("candidates"),
                "hall_of_fame": count("hall_of_fame"),
                "lessons": count("lessons"),
                "paper_sessions": count("paper_sessions"),
                "paper_trades": count("paper_trades"),
                "risk_events": count("risk_events"),
                "api_calls": count("api_calls"),
                "strategy_versions": count("strategy_versions"),
            }
            row = conn.execute(
                "SELECT MAX(fitness) AS best FROM hall_of_fame"
            ).fetchone()
            out["best_fitness"] = float(row["best"]) if row and row["best"] is not None else None
        return out


# --------------------------------------------------------------------------
# Row mappers
# --------------------------------------------------------------------------


def _run_row(r: sqlite3.Row) -> dict[str, Any]:
    keys = r.keys()
    return {
        "id": r["id"],
        "ts": r["ts"],
        "note": r["note"],
        "config": _loads(r["config"], {}),
        "summary": _loads(r["summary"], {}),
        "outcome": r["outcome"],
        # Provenance.  Read defensively: a database that failed to migrate must still
        # be browsable, so a missing column reads as `None` rather than raising.
        "commit_sha": r["commit_sha"] if "commit_sha" in keys else None,
        "code_version": r["code_version"] if "code_version" in keys else None,
        "dataset_sha": r["dataset_sha"] if "dataset_sha" in keys else None,
        "dataset_rows": r["dataset_rows"] if "dataset_rows" in keys else None,
        "repro_id": r["repro_id"] if "repro_id" in keys else None,
        "reproducible": (
            None if "reproducible" not in keys or r["reproducible"] is None else bool(r["reproducible"])
        ),
    }


def _version_row(r: sqlite3.Row) -> dict[str, Any]:
    parent = _loads(r["parent_genome"], None)
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "generation": r["generation"],
        "version": r["version"],
        "family": r["family"],
        "parent_family": r["parent_family"],
        "origin": r["origin"],
        "genome": _loads(r["genome"], {}),
        "parent": parent if isinstance(parent, dict) else None,
        "genome_hash": r["genome_hash"],
        "parent_hash": r["parent_hash"],
        "diff": _loads(r["diff"], []),
        "rationale": r["rationale"],
        "reason": r["reason"],
        "fitness": r["fitness"],
        "parent_fitness": r["parent_fitness"],
        "delta_fitness": r["delta_fitness"],
        "passed": bool(r["passed"]),
        "outcome": r["outcome"],
        "created_at": r["created_at"],
    }


def _candidate_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "generation": r["generation"],
        "family": r["family"],
        "genome": _loads(r["genome"], {}),
        "origin": r["origin"],
        "fold_metrics": _loads(r["fold_metrics"], []),
        "fitness": r["fitness"],
        "passed": bool(r["passed"]),
        "reason": r["reason"],
        "created_at": r["created_at"],
    }


def _hall_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "family": r["family"],
        "genome": _loads(r["genome"], {}),
        "fitness": r["fitness"],
        "metrics_oos": _loads(r["metrics_oos"], {}),
        "created_at": r["created_at"],
    }


def _lesson_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "text": r["text"],
        "tags": _loads(r["tags"], []),
        "question": r["question"],
        "target": r["target"],
        "embedding": _loads(r["embedding"], None),
        "created_at": r["created_at"],
    }


def _ledger_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"],
        "kind": r["kind"],
        "amount": r["amount"],
        "currency": r["currency"],
        "reason": r["reason"],
        "source": r["source"],
        "session_id": r["session_id"],
        "balance_after": r["balance_after"],
        "created_at": r["created_at"],
    }


def _paper_session_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "genome": _loads(r["genome"], {}),
        "window": r["window"],
        "status": r["status"],
        "cursor": r["cursor"],
        "total_bars": r["total_bars"],
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "cash": r["cash"],
        "initial_cash": r["initial_cash"],
        "settings": _loads(r["settings"], {}),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
