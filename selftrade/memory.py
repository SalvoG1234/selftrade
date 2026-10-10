"""Persistent memory in a single SQLite file.

Tables: ``runs``, ``candidates``, ``hall_of_fame``, ``lessons`` and the paper
trading trio ``paper_sessions`` / ``paper_trades`` / ``paper_equity``.

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

SCHEMA_VERSION = 1

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
    outcome TEXT
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

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_hall_fitness ON hall_of_fame(fitness DESC);
CREATE INDEX IF NOT EXISTS idx_lessons_run ON lessons(run_id);
CREATE INDEX IF NOT EXISTS idx_ptrades_session ON paper_trades(session_id);
CREATE INDEX IF NOT EXISTS idx_pequity_session ON paper_equity(session_id);
CREATE INDEX IF NOT EXISTS idx_risk_events_created ON risk_events(created_at DESC);
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


class Memory:
    """Thin, thread-safe-enough SQLite wrapper (one connection per call)."""

    def __init__(self, db_path: str | Path = "state/selftrade.db"):
        self.db_path = str(db_path)
        p = Path(self.db_path)
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

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
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            # Future migrations would compare int(row["value"]) < SCHEMA_VERSION.

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    # -------------------------------------------------- runs
    def start_run(self, note: str = "", config: Any = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs(ts, note, config, summary, outcome) VALUES (?,?,?,?,?)",
                (time.time(), note, _json_or_none(config), None, "running"),
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
    return {
        "id": r["id"],
        "ts": r["ts"],
        "note": r["note"],
        "config": _loads(r["config"], {}),
        "summary": _loads(r["summary"], {}),
        "outcome": r["outcome"],
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
