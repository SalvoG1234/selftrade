"""Propose the next experiment.

Two paths, always yielding a legal genome:

* **LLM** (Ollama) proposes *one bounded change* to the current best genome.
  Its output is clamped by :func:`validate_genome`; if Ollama is down or the
  JSON is unusable we fall back.
* **Deterministic** mutation (and, in ``explore`` mode, occasional family
  changes).

The chosen origin (``seed`` / ``deterministic`` / ``llm``) is recorded on the
candidate and reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from .genome import (
    GenomeError,
    mutate_genome,
    sample_genome,
    validate_genome,
)
from .llm import PROPOSAL_SCHEMA, OllamaClient, cosine_similarity
from .params import FAMILY_PARAMS
from .params import RISK_STOP_HIGH, RISK_STOP_LOW, RISK_TARGET_HIGH, RISK_TARGET_LOW

SYSTEM_PROMPT = """You are a quantitative research assistant for a trading strategy search engine.
You NEVER write trading logic or code. You may only choose and modulate a bounded set of
parameters for an existing strategy family, given on purpose.

Hard rules:
- Reply with a single JSON object only, matching the requested schema.
- Keep 'family' to one of the allowed families.
- Change at most a few parameters relative to the current genome; small, testable steps.
- Parameter values are clamped afterwards, but propose values inside the documented ranges.
- Prefer simpler strategies (fewer active filters) unless evidence justifies complexity.
- Include a short 'rationale' explaining the change in one sentence.
"""


@dataclass
class Proposal:
    genome: dict[str, Any]
    origin: str  # seed | deterministic | llm
    rationale: str = ""
    llm_used: bool = False
    rejected_llm: bool = False
    #: The genome this one was derived from - the elite carried over, or the current
    #: best that a mutation/LLM proposal modified.  ``None`` for a fresh seed.  Kept on
    #: the proposal so the recorded strategy version can diff against the *real* parent
    #: instead of guessing one after the fact.
    parent: dict[str, Any] | None = None
    parent_fitness: float | None = None


def format_ranges(family: str) -> str:
    lines = []
    for name, spec in FAMILY_PARAMS[family].items():
        if spec.kind == "int":
            lines.append(f"  {name}: int in [{spec.low}, {spec.high}] (default {spec.default})")
        elif spec.kind == "float":
            lines.append(f"  {name}: float in [{spec.low}, {spec.high}] (default {spec.default})")
        elif spec.kind == "bool":
            lines.append(f"  {name}: bool (default {spec.default})")
        else:
            lines.append(f"  {name}: one of {list(spec.choices)} (default {spec.default})")
    lines.append(f"  risk.stop_loss_pct: float in [{RISK_STOP_LOW}, {RISK_STOP_HIGH}] (negative)")
    lines.append(f"  risk.take_profit_pct: null or float in [{RISK_TARGET_LOW}, {RISK_TARGET_HIGH}]")
    lines.append(f"  risk.trailing_pct: null or float in [{RISK_TARGET_LOW}, {RISK_TARGET_HIGH}]")
    lines.append("  risk.trade_shorts: bool")
    return "\n".join(lines)


def build_user_prompt(
    best: dict[str, Any] | None,
    generation: int,
    lessons: list[str],
    evidence: list[str] | None = None,
    mode: str = "explore",
    metric: str = "sharpe",
) -> str:
    parts: list[str] = []
    parts.append(f"Generation: {generation}. Optimisation metric: {metric}. Mode: {mode}.")
    if best is not None:
        parts.append("Current best genome (JSON):")
        parts.append(json.dumps(best, indent=2, sort_keys=True))
        parts.append("Allowed parameter ranges for this family:")
        parts.append(format_ranges(best["family"]))
    else:
        parts.append(
            "No champion yet. Pick a family and propose a reasonable starting genome. "
            f"Allowed families: {', '.join(FAMILY_PARAMS)}."
        )
    if lessons:
        parts.append("Relevant past lessons (successes and failures):")
        for lesson in lessons[:8]:
            parts.append(f"- {lesson}")
    if evidence:
        parts.append("Reverse-engineering evidence (REA):")
        for item in evidence[:8]:
            parts.append(f"- {item}")
    parts.append(
        "Propose ONE bounded modification (or a seed genome). "
        "Return JSON with keys: family, params, risk, rationale."
    )
    return "\n".join(parts)


def retrieve_lessons(
    memory: Any,
    query: str,
    llm: OllamaClient | None,
    k: int = 5,
) -> list[str]:
    """Semantic lesson retrieval when embeddings exist, else most recent."""
    if memory is None:
        return []
    stored = memory.lessons_with_embeddings()
    if llm is not None and stored:
        q = llm.embed(query)
        if q is not None:
            scored = []
            for row in stored:
                sim = cosine_similarity(q, row["embedding"] or [])
                scored.append((sim, row["text"]))
            scored.sort(key=lambda t: t[0], reverse=True)
            return [t[1] for t in scored[:k]]
    recent = memory.lessons(limit=k)
    return [r["text"] for r in recent]


def propose_next(
    best: dict[str, Any] | None,
    rng: np.random.Generator,
    settings: Any,
    *,
    mode: str = "explore",
    generation: int = 0,
    lessons: list[str] | None = None,
    evidence: list[str] | None = None,
    llm: OllamaClient | None = None,
    use_llm: bool = True,
) -> Proposal:
    """Return the next genome to evaluate, with its provenance."""
    lessons = lessons or []
    evidence = evidence or []

    if use_llm and llm is not None and settings.llm.enabled:
        user = build_user_prompt(
            best,
            generation,
            lessons,
            evidence,
            mode=mode,
            metric=settings.evaluation.metric,
        )
        raw = llm.chat_json(SYSTEM_PROMPT, user, PROPOSAL_SCHEMA)
        if raw is not None:
            try:
                genome = validate_genome(raw)
                return Proposal(
                    genome=genome,
                    origin="llm",
                    rationale=str(raw.get("rationale", ""))[:500],
                    llm_used=True,
                    parent=best,
                )
            except GenomeError:
                # Fall through to deterministic; flag the rejection.
                fallback = _deterministic(best, rng, settings, mode, generation)
                fallback.rejected_llm = True
                fallback.rationale = (
                    "LLM proposal rejected by validate_genome; used deterministic mutation. "
                    + fallback.rationale
                )
                return fallback

    return _deterministic(best, rng, settings, mode, generation)


def _deterministic(
    best: dict[str, Any] | None,
    rng: np.random.Generator,
    settings: Any,
    mode: str,
    generation: int,
) -> Proposal:
    if best is None:
        g = sample_genome(None, rng)
        return Proposal(genome=g, origin="seed", rationale="seed genome", parent=None)
    g = mutate_genome(
        best,
        rng,
        rate=settings.evolution.mutation_rate,
        allow_family_change=(mode == "explore"),
    )
    return Proposal(
        genome=g,
        origin="deterministic",
        rationale=f"deterministic mutation (mode={mode})",
        parent=best,
    )
