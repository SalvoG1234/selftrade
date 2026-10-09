"""REA integration: an *information* source, never an execution source.

REA is used to learn *how* a feed, indicator or contract actually computes
something.  Evidence (and its limits) is stored in memory and fed into the
proposal prompt.

Hard rules:

* a target is only analysed when it declares ``authorized: true``;
* ``dry_run`` is the default - the real command is shown, not executed;
* nothing is analysed that you are not permitted to analyse;
* with REA disabled the whole project still works.

An optional ``mcp`` command generates an MCP config for REA + Ruflo with a
roster of agents (inputs, outputs, acceptance criteria) and ready missions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .memory import Memory


@dataclass
class Target:
    index: int
    name: str
    path: str = ""
    question: str = ""
    provider: str = ""
    authorized: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identifier(self) -> str:
        return self.name or self.path or f"target-{self.index}"


def parse_targets(settings: Any) -> list[Target]:
    out: list[Target] = []
    for i, raw in enumerate(settings.rea.targets or []):
        if isinstance(raw, str):
            out.append(Target(index=i, name=raw, path=raw, authorized=False))
            continue
        if not isinstance(raw, dict):
            continue
        out.append(
            Target(
                index=i,
                name=str(raw.get("name", "")),
                path=str(raw.get("path", raw.get("url", ""))),
                question=str(raw.get("question", raw.get("goal", ""))),
                provider=str(raw.get("provider", settings.rea.provider)),
                authorized=bool(raw.get("authorized", False)),
                raw=raw,
            )
        )
    return out


def plan(settings: Any) -> dict[str, Any]:
    targets = parse_targets(settings)
    authorized = [t for t in targets if t.authorized]
    blocked = [t for t in targets if not t.authorized]
    return {
        "enabled": settings.rea.enabled,
        "dry_run": settings.rea.dry_run,
        "provider": settings.rea.provider,
        "node_cmd": settings.rea.node_cmd,
        "package": settings.rea.package,
        "node_available": shutil.which(settings.rea.node_cmd) is not None,
        "targets": [
            {
                "id": t.identifier,
                "provider": t.provider,
                "question": t.question,
                "path": t.path,
                "authorized": t.authorized,
                "status": "would run" if t.authorized else "BLOCKED (authorized: false)",
            }
            for t in targets
        ],
        "authorized_count": len(authorized),
        "blocked_count": len(blocked),
    }


def build_command(settings: Any, target: Target) -> list[str]:
    cmd = [settings.rea.node_cmd, "-y", settings.rea.package]
    cmd += ["--provider", target.provider or settings.rea.provider]
    if target.path:
        cmd += ["--target", target.path]
    if target.question:
        cmd += ["--question", target.question]
    return cmd


Runner = Callable[[list[str]], tuple[int, str]]


def _default_runner(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)  # noqa: S603
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "REA command timed out"


def run(
    settings: Any,
    memory: Memory,
    *,
    live: bool = False,
    target: int | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Run (or dry-run) REA analysis for authorized targets only."""
    if not settings.rea.enabled:
        return {"enabled": False, "executed": [], "blocked": [], "message": "rea.enabled is false"}

    targets = parse_targets(settings)
    if target is not None:
        targets = [t for t in targets if t.index == target]
        if not targets:
            return {"enabled": True, "error": f"no target with index {target}"}

    dry_run = settings.rea.dry_run or not live
    runner = runner or _default_runner
    executed: list[dict[str, Any]] = []
    blocked: list[str] = []

    for t in targets:
        if not t.authorized:
            blocked.append(t.identifier)
            continue
        cmd = build_command(settings, t)
        record = {"target": t.identifier, "command": cmd, "dry_run": dry_run}
        if dry_run:
            record["note"] = "dry run: command shown, not executed"
            executed.append(record)
            continue
        code, output = runner(cmd)
        record["returncode"] = code
        record["summary"] = output.strip()[:4000]
        executed.append(record)
        if code == 0 and output.strip():
            memory.add_lesson(
                None,
                f"REA evidence for {t.identifier}: {output.strip()[:1500]}",
                tags=["rea", t.provider],
                question=t.question or None,
                target=t.path or t.identifier,
            )
    return {
        "enabled": True,
        "dry_run": dry_run,
        "executed": executed,
        "blocked": blocked,
        "message": (
            "dry run: nothing executed (set rea.dry_run=false and pass --live to execute)"
            if dry_run
            else "live run complete"
        ),
    }


def prompt_evidence(memory: Memory, limit: int = 5) -> list[str]:
    """Recent REA evidence to include in the proposal prompt."""
    if memory is None:
        return []
    items = []
    for lesson in memory.lessons(limit=200):
        tags = lesson.get("tags") or []
        if "rea" in tags:
            items.append(lesson["text"])
        if len(items) >= limit:
            break
    return items


def doctor_line(settings: Any) -> str:
    if not settings.rea.enabled:
        return "disabled in config (rea.enabled=false)"
    node = shutil.which(settings.rea.node_cmd)
    plan_info = plan(settings)
    return (
        f"enabled provider={settings.rea.provider} dry_run={settings.rea.dry_run} "
        f"node={'found' if node else 'MISSING'} "
        f"targets={len(plan_info['targets'])} authorized={plan_info['authorized_count']} "
        f"blocked={plan_info['blocked_count']}"
    )


# --------------------------------------------------------------------------
# MCP config (REA + Ruflo) with a real agent roster and missions
# --------------------------------------------------------------------------

AGENT_ROSTER = [
    {
        "name": "rea-analyst",
        "role": "Gather evidence about how a feed/indicator/contract behaves",
        "inputs": ["authorized target", "question"],
        "outputs": ["evidence text saved to selftrade memory (tag: rea)"],
        "acceptance": [
            "targets without authorized:true are refused",
            "evidence is stored with target and question",
            "dry_run is honoured",
        ],
    },
    {
        "name": "strategy-proposer",
        "role": "Propose one bounded genome change",
        "inputs": ["champion genome", "recent lessons", "REA evidence"],
        "outputs": ["one JSON genome (origin=llm)"],
        "acceptance": [
            "validate_genome returns a legal genome",
            "at most a single bounded change",
            "falls back to deterministic mutation if unavailable",
        ],
    },
    {
        "name": "backtest-auditor",
        "role": "Audit walk-forward results and gates",
        "inputs": ["candidate genome", "development series"],
        "outputs": ["per-fold metrics", "gate verdict with reason"],
        "acceptance": [
            "holdout is never read during evolution",
            "rejected candidates are still recorded with a reason",
        ],
    },
    {
        "name": "mt5-bridge-guard",
        "role": "Guard the FX/CFD order path",
        "inputs": ["order request"],
        "outputs": ["dry-run request or a readable rejection list"],
        "acceptance": [
            "order_send is never called without both keys",
            "real accounts are refused when require_demo",
            "the password never appears in any output",
        ],
    },
]

MISSIONS = [
    {
        "name": "evidence-sweep",
        "agent": "rea-analyst",
        "goal": "Collect evidence for each authorized target and store limits in memory.",
        "acceptance": "each authorized target yields a stored lesson tagged 'rea'",
    },
    {
        "name": "guided-evolution",
        "agent": "strategy-proposer",
        "goal": "Run evolve with LLM proposals seeded by REA evidence.",
        "acceptance": "a run report exists and declares the proposal origin mix",
    },
    {
        "name": "gate-audit",
        "agent": "backtest-auditor",
        "goal": "Verify that no candidate reaches the hall without passing every gate.",
        "acceptance": "hall_of_fame count equals the number of gate-passing champions",
    },
    {
        "name": "fx-safety-drill",
        "agent": "mt5-bridge-guard",
        "goal": "Exercise the MT5 dry run and rejection paths against a fake terminal.",
        "acceptance": "order_send is not called in any dry run; a real account is refused",
    },
]


def mcp_config(settings: Any) -> dict[str, Any]:
    return {
        "mcpServers": {
            "rea": {"command": settings.rea.node_cmd, "args": ["-y", settings.rea.package, "mcp"]},
            "ruflo": {"command": settings.rea.node_cmd, "args": ["-y", "ruflo@latest", "mcp"]},
        },
        "selftrade": {
            "agents": AGENT_ROSTER,
            "missions": MISSIONS,
            "note": (
                "Only roles with inputs, outputs and acceptance criteria. No decorative agents."
            ),
        },
    }


def write_mcp_config(settings: Any, out: str | None = None) -> Path:
    path = Path(out or ".mcp.json")
    path.write_text(json.dumps(mcp_config(settings), indent=2), encoding="utf-8")
    return path


def cli(settings: Any, args: Any) -> int:
    from .memory import Memory

    action = args.action
    if action == "doctor":
        print("rea: " + doctor_line(settings))
        return 0
    if action == "plan":
        _print(json.dumps(plan(settings), indent=2))
        return 0
    if action == "run":
        mem = Memory(settings.memory.db)
        out = run(settings, mem, live=bool(args.live), target=args.target)
        _print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("enabled") else 1
    print(f"unknown rea action: {action}")
    return 1


def _print(text: str) -> None:
    print(text)
