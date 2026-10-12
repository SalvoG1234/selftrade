"""Reproducibility: which code, and which data, produced a run.

A stored fitness is only worth something if you can say *what* produced it.  Two
facts decide that, and this module records both:

* the **code** - the git commit, plus whether the worktree was dirty when the run
  started (a dirty worktree means the commit alone does *not* determine the code
  that ran, so the run is flagged as not reproducible from the commit);
* the **data** - a SHA-256 over the exact bytes of the source file, or, when the
  prices never touched a file (synthetic, MT5), a canonical hash of the frame
  the backtester was handed.

:func:`repro_id` combines the two into one identifier: two runs over the same
commit and the same dataset yield the **same** id, which is the whole point -
it is what lets you say "this number came from that code and that data" instead
of "this number came from somewhere".

Nothing here raises.  A machine without git, or without a data file, gets a
fingerprint that says ``N/D`` and explains itself, because a run that cannot be
fingerprinted must still be *reported*, not aborted.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Length of the identifiers shown to a human (the stored value is full-length).
SHORT = 12

#: How long a git probe may take before it is treated as unavailable.
GIT_TIMEOUT_S = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _git(args: list[str], repo_dir: Path) -> str | None:
    """Run one read-only git command.  ``None`` when git or the repo is missing."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def repo_dir() -> Path:
    """The project root, guessed from this file's location (``selftrade/`` parent)."""
    return Path(__file__).resolve().parent.parent


def code_fingerprint(repo: str | Path | None = None) -> dict[str, Any]:
    """The code that is running: commit, branch, and whether the tree was dirty.

    ``available`` is False when git could not answer at all; ``dirty`` is True
    when tracked files differ from the commit, which is the interesting case -
    a run from a dirty tree is not reproducible from its SHA alone.
    """
    where = Path(repo) if repo is not None else repo_dir()
    commit = _git(["rev-parse", "HEAD"], where)
    if commit is None:
        return {
            "available": False,
            "commit": None,
            "short": None,
            "branch": None,
            "dirty": None,
            "note": f"git could not read {where}: the code is not identified by a commit",
        }
    short = _git(["rev-parse", "--short", "HEAD"], where) or commit[:SHORT]
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], where)
    status = _git(["status", "--porcelain"], where)
    dirty = None if status is None else bool(status)
    return {
        "available": True,
        "commit": commit,
        "short": short,
        "branch": branch,
        "dirty": dirty,
        # Whether the tree was dirty is the flag; :func:`fingerprint` turns it into the
        # sentence the report carries, so the wording lives in one place.
        "note": None,
    }


def dataset_fingerprint(settings: Any = None, df: Any = None) -> dict[str, Any]:
    """Identify the prices a run was evaluated on.

    Prefers the source file's bytes (that is the exact input, and it is cheap);
    falls back to a canonical hash of the dataframe when the prices never came
    from a file.  The frame hash uses the values plus the first/last timestamp,
    so it cannot be accidentally equal for a different series.
    """
    source = getattr(getattr(settings, "data", None), "source", None)
    symbol = getattr(getattr(settings, "data", None), "symbol", None)
    timeframe = getattr(getattr(settings, "data", None), "timeframe", None)
    data = getattr(settings, "data", None)

    out: dict[str, Any] = {
        "method": "unavailable",
        "sha256": None,
        "rows": None,
        "symbol": symbol,
        "timeframe": timeframe,
        "source": source,
        "first_ts": None,
        "last_ts": None,
        "path": None,
        "bytes": None,
        "note": "neither a data file nor a dataframe was available to hash",
    }

    # --- a file is the exact input: hash its bytes ------------------------
    csv = getattr(data, "csv", None)
    if source in ("csv", "freqtrade_json") and csv and Path(str(csv)).is_file():
        path = Path(str(csv))
        try:
            payload = path.read_bytes()
        except OSError as exc:
            out["note"] = f"the data file could not be read: {exc}"
        else:
            out.update(
                {
                    "method": "file",
                    "sha256": _sha256_hex(payload),
                    "bytes": len(payload),
                    "path": str(path),
                    "note": None,
                }
            )
            if source == "csv":
                # A row count without parsing the file: a header plus one line per bar.
                # Only for CSV - an embedded newline in a JSON export would lie.
                try:
                    text = payload.decode("utf-8", errors="strict")
                    out["rows"] = max(0, sum(1 for line in text.splitlines() if line.strip()) - 1)
                except Exception:  # noqa: BLE001 - a count is a nicety, not the hash
                    pass

    # --- the frame itself (synthetic, MT5, or a file we could not read) ---
    if out["sha256"] is None and df is not None:
        try:
            import numpy as np

            values = np.ascontiguousarray(df.to_numpy(dtype="float64")).tobytes()
            index = df.index
            first = str(index[0]) if len(index) else ""
            last = str(index[-1]) if len(index) else ""
            header = "|".join(str(c) for c in df.columns)
            digest = _sha256_hex(f"{header}|{first}|{last}|".encode("utf-8") + values)
        except Exception as exc:  # noqa: BLE001 - a fingerprint never breaks a run
            out["note"] = f"the dataframe could not be hashed: {exc}"
        else:
            out.update(
                {
                    "method": "frame",
                    "sha256": digest,
                    "rows": int(len(index)),
                    "first_ts": first or None,
                    "last_ts": last or None,
                    "note": None,
                }
            )

    # Rows from the file path: count them only if asked (the frame branch knows).
    if out["sha256"] is not None and out["rows"] is None and df is not None:
        try:
            out["rows"] = int(len(df.index))
            out["first_ts"] = str(df.index[0]) if len(df.index) else None
            out["last_ts"] = str(df.index[-1]) if len(df.index) else None
        except Exception:  # noqa: BLE001
            pass
    if out["sha256"] is not None:
        out["short"] = out["sha256"][:SHORT]
    else:
        out["short"] = None
    return out


def repro_id(commit: Any, dataset_sha: Any) -> str | None:
    """One identifier for *this code over this data*.

    Deterministic by construction: the same commit and the same dataset hash
    always produce the same value, which is what makes it usable as a key
    ("the same experiment again") rather than decoration.
    """
    if not commit or not dataset_sha:
        return None
    return _sha256_hex(f"{commit}:{dataset_sha}".encode("utf-8"))[:16]


def fingerprint(
    settings: Any = None,
    *,
    df: Any = None,
    repo: str | Path | None = None,
) -> dict[str, Any]:
    """The full reproducibility record stored on a run.

    ``reproducible`` is False when either half is missing, or when the tree was
    dirty: the id is still emitted (two dirty runs of the same commit and data
    share it, which is honest) but the page says out loud that the commit does
    not pin the code.
    """
    code = code_fingerprint(repo)
    data = dataset_fingerprint(settings, df)
    rid = repro_id(code.get("commit"), data.get("sha256"))
    dirty = code.get("dirty")
    reproducible = bool(rid and dirty is not True)
    notes: list[str] = []
    if code.get("note"):
        notes.append(code["note"])
    if data.get("note"):
        notes.append(data["note"])
    if code.get("available") and dirty is True:
        notes.append(
            "the worktree had uncommitted changes when this run started: the id is still "
            "recorded, but the commit does not determine the code that ran"
        )
    return {
        "repro_id": rid,
        "reproducible": reproducible,
        "commit": code.get("commit"),
        "commit_short": code.get("short"),
        "branch": code.get("branch"),
        "dirty": dirty,
        "code_available": bool(code.get("available")),
        "dataset": data,
        "notes": notes,
        "checked_at": _now(),
    }
