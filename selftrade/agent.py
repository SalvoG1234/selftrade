"""The Evolver: the search loop that ties everything together.

Cycle per run:

    seed -> population -> evaluation (walk-forward) -> elite
         -> proposal (LLM or deterministic) -> next generation
         -> journal every candidate -> end-of-generation lesson
         -> champion -> holdout (read ONCE) -> markdown + JSON report

Must work with Ollama off; the report says so explicitly.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import Settings, as_dict
from . import autopsy as autopsy_mod
from . import critic as critic_mod
from .data import load_dataframe, split_holdout
from .evaluate import (
    Holdout,
    evaluate_folds,
    holdout_eval,
    metric_value,
    regime_luck_warning,
)
from .genome import (
    complexity,
    describe,
    genome_diff,
    genome_key,
    sample_genome,
    validate_genome,
)
from .llm import OllamaClient
from .memory import Memory
from .propose import Proposal, propose_next, retrieve_lessons
from .repro import fingerprint as repro_fingerprint


@dataclass
class Candidate:
    genome: dict[str, Any]
    origin: str
    fitness: float
    passed: bool
    reason: str
    generation: int
    fold_metrics: list[dict[str, Any]] = field(default_factory=list)
    metric_mean: float = 0.0
    metric_std: float = 0.0
    complexity: int = 0
    n_trades: int = 0
    max_drawdown: float = 0.0
    rationale: str = ""
    key: str = ""
    #: Row id in ``strategy_versions``: the recorded before/after lineage of this
    #: candidate.  ``None`` when the version could not be written (the search still runs).
    version_id: int | None = None
    #: 1-based sequence of this version inside the run (the number a human reads).
    version: int | None = None
    parent_fitness: float | None = None
    diff: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = ""

    @property
    def family(self) -> str:
        return self.genome["family"]


@dataclass
class EvolutionResult:
    run_id: int | None
    generations: list[dict[str, Any]]
    candidates: list[Candidate]
    champion: Candidate | None
    holdout: dict[str, Any] | None
    metric: str
    llm_enabled: bool
    llm_available: bool
    proposals: dict[str, int]
    regime_warning: str | None
    report_md: str | None = None
    report_json: str | None = None
    stopped: bool = False
    autopsy: dict[str, Any] | None = None
    critic: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    #: Provenance of this run: commit, dataset hash and the resulting repro id.
    repro: dict[str, Any] | None = None
    #: The run's version lineage: every parent -> candidate step, with its diff.
    versions: list[dict[str, Any]] = field(default_factory=list)


class Evolver:
    def __init__(
        self,
        settings: Settings,
        memory: Memory | None = None,
        llm: OllamaClient | None = None,
        df=None,
        session: Any = None,
        log: Callable[[str], None] = print,
        report_dir: str = "reports",
    ):
        self.settings = settings
        self.report_dir = report_dir
        self.memory = memory or Memory(settings.memory.db)
        self.llm = llm
        if llm is None and settings.llm.enabled:
            self.llm = OllamaClient(
                base_url=settings.llm.base_url,
                model=settings.llm.model,
                embed_model=settings.llm.embed_model,
                num_ctx=settings.llm.num_ctx,
                num_predict=settings.llm.num_predict,
                temperature=settings.llm.temperature,
                timeout=settings.llm.timeout,
            )
        self.df = df
        self.session = session
        self.log = log
        self.rng = np.random.default_rng(settings.evolution.seed)

    # ------------------------------------------------------------------
    def run(
        self,
        generations: int | None = None,
        *,
        note: str = "",
        use_llm: bool = True,
        quiet: bool = False,
    ) -> EvolutionResult:
        s = self.settings
        ev = s.evolution
        generations = int(generations if generations is not None else ev.generations)

        df = self.df if self.df is not None else load_dataframe(s)
        dev, hold_df = split_holdout(df, s.evaluation.holdout_frac)
        guard = Holdout(hold_df)
        if len(dev) < 50:
            raise RuntimeError(
                f"development series too short ({len(dev)} rows); generate more data"
            )

        llm_available = bool(
            use_llm and s.llm.enabled and self.llm is not None and self.llm.available()
        )
        # Provenance first: the commit and the dataset are recorded on the run *before*
        # anything is evaluated, so every fitness below is traceable to both.
        self._repro = repro_fingerprint(s, df=df)
        self._version_seq = 0
        run_id = self.memory.start_run(note, as_dict(s), repro=self._repro)

        # REA evidence (if any) is fed into the proposal prompt.
        self._rea_evidence: list[str] = []
        if s.rea.enabled and not getattr(self, "no_rea", False):
            from . import rea as rea_mod

            self._rea_evidence = rea_mod.prompt_evidence(self.memory, limit=5)

        def emit(msg: str) -> None:
            if not quiet:
                self.log(msg)

        emit(
            f"[evolve] run #{run_id} | rows={len(df)} dev={len(dev)} holdout={len(hold_df)} "
            f"| llm={'on' if llm_available else 'off'}"
        )
        emit(
            f"[evolve] repro {self._repro.get('repro_id') or 'N/D'} "
            f"| commit {self._repro.get('commit_short') or 'N/D'}"
            f"{' (dirty)' if self._repro.get('dirty') else ''} "
            f"| data {((self._repro.get('dataset') or {}).get('short')) or 'N/D'}"
        )

        all_candidates: list[Candidate] = []
        generation_reports: list[dict[str, Any]] = []
        proposals: dict[str, int] = {"seed": 0, "deterministic": 0, "llm": 0}
        elite: list[Candidate] = []
        stopped = False
        notes: list[str] = []
        # Provenance caveats belong in the report's limits section: a run from a dirty
        # worktree must say so where the numbers are read, not only in the dashboard.
        for repro_note in self._repro.get("notes") or []:
            notes.append(f"reproducibility: {repro_note}")

        if not llm_available:
            notes.append(
                "Ollama was not available (or --no-llm): proposals were deterministic."
            )

        for gen in range(generations):
            if self.session is not None:
                action = self.session.checkpoint(gen, generations)
                if action == "stop":
                    stopped = True
                    emit(f"[evolve] stop requested at generation {gen}; writing report")
                    break

            pop = self._build_population(gen, ev.population, elite, llm_available, run_id)
            evaluated: list[Candidate] = []
            seen: set[str] = set()
            for prop in pop:
                key = genome_key(prop.genome)
                if key in seen:
                    continue
                seen.add(key)
                cand = self._evaluate(prop, gen, run_id)
                evaluated.append(cand)
                all_candidates.append(cand)
                proposals[cand.origin] = proposals.get(cand.origin, 0) + 1
                emit(
                    f"  g{gen} {cand.family:<20} fit={cand.fitness: .3f} "
                    f"{'KEEP' if cand.passed else 'drop'} "
                    f"({cand.reason or 'ok'}) [{cand.origin}]"
                )

            elite = self._select_elite(evaluated, ev.elite)
            lesson = self._end_of_generation_lesson(gen, evaluated, elite, llm_available, run_id)
            generation_reports.append(
                {
                    "generation": gen,
                    "candidates": [self._candidate_summary(c) for c in evaluated],
                    "best_fitness": max((c.fitness for c in evaluated), default=0.0),
                    "kept": sum(1 for c in evaluated if c.passed),
                    "elite": [describe(c.genome) for c in elite],
                    "lesson": lesson,
                }
            )
            emit(
                f"  g{gen} summary: {len(evaluated)} candidates, "
                f"{sum(1 for c in evaluated if c.passed)} passed gate, "
                f"best={max((c.fitness for c in evaluated), default=0.0):.3f}"
            )

        # ---- champion + holdout (read exactly once) -------------------
        passers = [c for c in all_candidates if c.passed]
        champion = max(passers, key=lambda c: c.fitness) if passers else None
        sample = champion or (max(all_candidates, key=lambda c: c.fitness) if all_candidates else None)

        holdout_report: dict[str, Any] | None = None
        autopsy_report: dict[str, Any] | None = None
        critic_report: dict[str, Any] | None = None
        warning: str | None = None
        if sample is not None:
            hold_df_read = guard.read()  # the single read of this run
            hold = holdout_eval(hold_df_read, sample.genome, s)
            fold_values = sample.fold_metrics and [
                metric_value(m, s.evaluation.metric) for m in sample.fold_metrics
            ]
            fold_values = fold_values or []
            hv = metric_value(hold["metrics"], s.evaluation.metric)
            warning = regime_luck_warning(fold_values, hv)
            holdout_report = {
                "read_count": guard.reads,
                "metrics": hold["metrics"],
                "metric": s.evaluation.metric,
                "metric_value": hv,
                "fold_mean": float(np.mean(fold_values)) if fold_values else 0.0,
                "fold_values": fold_values,
                "n_trades": hold["n_trades"],
                "regime_luck_warning": warning,
                "genome": sample.genome,
                "was_champion": champion is not None and sample is champion,
            }
            # ---- autopsy: read the losers, cluster the reasons -------------
            try:
                autopsy_report = autopsy_mod.autopsy(
                    hold["trades"],
                    hold_df_read,
                    sample.genome,
                    worst_n=5,
                    min_trades=int(s.evaluation.min_trades),
                    window_label="holdout",
                    origin=sample.origin,
                )
                autopsy_lesson = autopsy_mod.lesson_text(autopsy_report)
                embedding = None
                if self.llm is not None and llm_available:
                    embedding = self.llm.embed(autopsy_lesson)
                self.memory.add_lesson(
                    run_id,
                    autopsy_lesson,
                    tags=["autopsy", f"family:{sample.family}", "window:holdout"],
                    embedding=embedding,
                )
                emit(f"[evolve] autopsy: {autopsy_report['summary']}")
                for line in autopsy_mod.console_lines(autopsy_report)[1:4]:
                    emit(line)
            except Exception as exc:  # never let the diagnosis kill the run
                notes.append(f"autopsy skipped: {exc}")

            # ---- critic: may this be deployed at all? ---------------------
            verdict = critic_mod.judge(
                hold["metrics"],
                genome=sample.genome,
                equity=hold.get("equity"),
                trades=hold.get("trades"),
                settings=s,
                min_trades=int(s.evaluation.min_trades),
            )
            critic_report = verdict.as_dict()
            emit(f"[evolve] critic: {verdict.text}")
            for warn in verdict.warnings:
                emit(f"[evolve] critic warning: {warn}")

            if champion is None:
                notes.append(
                    "No candidate passed all gates: nothing was promoted to the hall of "
                    "fame. The holdout was still read once, on the best-scoring candidate."
                )
            emit(
                f"[evolve] holdout read #{guard.reads} | "
                f"{s.evaluation.metric}={hv:.3f} vs fold mean "
                f"{(float(np.mean(fold_values)) if fold_values else 0.0):.3f}"
            )
            if warning:
                emit(f"[evolve] WARNING: {warning}")

        if champion is not None:
            self.memory.add_to_hall(run_id, champion.genome, champion.fitness, holdout_report)
            if champion.version_id is not None:
                # A candidate that passes every gate and wins the run is the one version a
                # reader will look for later; label it as such instead of leaving "kept".
                self.memory.set_version_outcome(champion.version_id, "champion")
            emit(f"[evolve] champion: {describe(champion.genome)}")

        versions_summary = [
            {
                "version": c.version,
                "generation": c.generation,
                "family": c.family,
                "origin": c.origin,
                "outcome": "champion" if champion is c else c.outcome,
                "fitness": c.fitness,
                "parent_fitness": c.parent_fitness,
                "delta_fitness": (
                    float(c.fitness) - float(c.parent_fitness)
                    if c.parent_fitness is not None
                    else None
                ),
                "passed": c.passed,
                "reason": c.reason,
                "rationale": c.rationale,
                "diff": c.diff,
                "key": c.key,
            }
            for c in all_candidates
        ]

        result = EvolutionResult(
            run_id=run_id,
            generations=generation_reports,
            candidates=all_candidates,
            champion=champion,
            holdout=holdout_report,
            autopsy=autopsy_report,
            critic=critic_report,
            metric=s.evaluation.metric,
            llm_enabled=s.llm.enabled,
            llm_available=llm_available,
            proposals=proposals,
            regime_warning=warning,
            stopped=stopped,
            notes=notes,
            repro=self._repro,
            versions=versions_summary,
        )
        md_path, json_path = write_report(result, s, self.report_dir)
        result.report_md = str(md_path)
        result.report_json = str(json_path)

        summary = {
            "generations": len(generation_reports),
            "candidates": len(all_candidates),
            "passed": len([c for c in all_candidates if c.passed]),
            "champion": describe(champion.genome) if champion else None,
            "champion_fitness": champion.fitness if champion else None,
            "holdout": holdout_report["metrics"] if holdout_report else None,
            "proposals": proposals,
            "llm_available": llm_available,
            "report_md": str(md_path),
            "report_json": str(json_path),
            "stopped": stopped,
            "repro_id": self._repro.get("repro_id"),
            "commit": self._repro.get("commit_short"),
            "dataset": (self._repro.get("dataset") or {}).get("short"),
            "reproducible": self._repro.get("reproducible"),
            "versions": self._version_seq,
        }
        self.memory.finish_run(run_id, summary, "stopped" if stopped else "ok")
        if self.session is not None:
            self.session.mark_finished("stopped" if stopped else "done")
        emit(f"[evolve] wrote {md_path}")
        return result

    # ------------------------------------------------------------------
    def _build_population(
        self,
        gen: int,
        population: int,
        elite: list[Candidate],
        llm_available: bool,
        run_id: int,
    ) -> list[Proposal]:
        s = self.settings
        pop: list[Proposal] = []
        if gen == 0:
            for _ in range(population):
                g = sample_genome(None, self.rng)
                pop.append(Proposal(genome=g, origin="seed", rationale="seed genome"))
            return pop

        for c in elite[:population]:
            pop.append(
                Proposal(
                    genome=c.genome,
                    origin=c.origin,
                    rationale="elite carried over",
                    parent=c.genome,
                    parent_fitness=float(c.fitness),
                )
            )

        best = elite[0].genome if elite else None
        best_fitness = float(elite[0].fitness) if elite else None
        remaining = max(0, population - len(pop))
        for _ in range(remaining):
            lessons = retrieve_lessons(
                self.memory, "what worked and what failed", self.llm if llm_available else None, k=5
            )
            prop = propose_next(
                best,
                self.rng,
                s,
                mode=s.evolution.mode,
                generation=gen,
                lessons=lessons,
                evidence=getattr(self, "_rea_evidence", []),
                llm=self.llm,
                use_llm=llm_available,
            )
            if prop.parent is not None and prop.parent_fitness is None:
                prop.parent_fitness = best_fitness
            pop.append(prop)
        return pop

    def _evaluate(self, prop: Proposal, gen: int, run_id: int) -> Candidate:
        rep = evaluate_folds(self._dev_cache(), prop.genome, self.settings)
        cand = Candidate(
            genome=prop.genome,
            origin=prop.origin,
            fitness=float(rep["fitness"]),
            passed=bool(rep["passed"]),
            reason=str(rep["gate_reason"]),
            generation=gen,
            fold_metrics=rep["folds"],
            metric_mean=float(rep["metric_mean"]),
            metric_std=float(rep["metric_std"]),
            complexity=int(rep["complexity"]),
            n_trades=int(rep["n_trades"]),
            max_drawdown=float(rep["max_drawdown"]),
            rationale=prop.rationale,
            key=genome_key(prop.genome),
            parent_fitness=prop.parent_fitness,
        )
        self.memory.add_candidate(
            run_id,
            gen,
            cand.genome,
            cand.origin,
            cand.fold_metrics,
            cand.fitness,
            cand.passed,
            cand.reason,
        )
        cand.version_id = self._record_version(prop, cand, gen, run_id)
        return cand

    def _record_version(self, prop: Proposal, cand: Candidate, gen: int, run_id: int) -> int | None:
        """Persist the parent -> candidate step with its diff, rationale and outcome.

        This is what makes "what changed between version 12 and version 13" a stored
        fact: the diff is taken against the genome the proposal actually came from (the
        elite it was carried from, or the best it was mutated out of), not against
        whatever happened to come first in the run.
        """
        self._version_seq += 1
        cand.version = self._version_seq
        cand.outcome = "kept" if cand.passed else "dropped"
        cand.diff = genome_diff(prop.parent, cand.genome) if prop.parent else []
        try:
            return self.memory.add_strategy_version(
                run_id,
                genome=cand.genome,
                parent=prop.parent,
                generation=gen,
                version=cand.version,
                origin=cand.origin,
                diff=cand.diff,
                rationale=prop.rationale,
                reason=cand.reason,
                fitness=cand.fitness,
                parent_fitness=prop.parent_fitness,
                passed=cand.passed,
                outcome=cand.outcome,
            )
        except Exception as exc:  # noqa: BLE001 - the search never dies over an audit row
            self.log(f"[evolve] version not recorded for {cand.family}: {exc}")
            return None

    def _dev_cache(self):
        if not hasattr(self, "_dev") or self._dev is None:
            df = self.df if self.df is not None else load_dataframe(self.settings)
            dev, _ = split_holdout(df, self.settings.evaluation.holdout_frac)
            self._dev = dev
        return self._dev

    @staticmethod
    def _select_elite(evaluated: list[Candidate], elite_n: int) -> list[Candidate]:
        ranked = sorted(evaluated, key=lambda c: (c.passed, c.fitness), reverse=True)
        return ranked[: max(1, elite_n)]

    def _end_of_generation_lesson(
        self,
        gen: int,
        evaluated: list[Candidate],
        elite: list[Candidate],
        llm_available: bool,
        run_id: int,
    ) -> str:
        s = self.settings
        best = max(evaluated, key=lambda c: c.fitness) if evaluated else None
        failed = [c for c in evaluated if not c.passed][:3]
        text = (
            f"Generation {gen}: {len(evaluated)} candidates, best fitness "
            f"{best.fitness if best else 0:.3f} "
            f"({describe(best.genome) if best else 'n/a'}). "
        )
        if failed:
            text += "Rejections: " + "; ".join(
                f"{c.family} {c.reason}" for c in failed
            ) + "."
        else:
            text += "All candidates passed the gate."

        if llm_available and self.llm is not None:
            refined = self.llm.chat_text(
                "You summarise quantitative experiment results as one terse lesson. "
                "No promises of profit. Plain text, one or two sentences.",
                text,
            )
            if refined:
                text = refined

        embedding = None
        if self.llm is not None and llm_available:
            embedding = self.llm.embed(text)
        self.memory.add_lesson(
            run_id,
            text,
            tags=[f"generation:{gen}", f"metric:{s.evaluation.metric}"],
            embedding=embedding,
        )
        return text

    @staticmethod
    def _candidate_summary(c: Candidate) -> dict[str, Any]:
        return {
            "family": c.family,
            "genome": c.genome,
            "origin": c.origin,
            "fitness": c.fitness,
            "passed": c.passed,
            "reason": c.reason,
            "metric_mean": c.metric_mean,
            "metric_std": c.metric_std,
            "complexity": c.complexity,
            "n_trades": c.n_trades,
            "max_drawdown": c.max_drawdown,
            "rationale": c.rationale,
            "version": c.version,
            "parent_fitness": c.parent_fitness,
            "diff": c.diff,
            "outcome": c.outcome,
        }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def write_report(result: EvolutionResult, settings: Settings, outdir: str = "reports") -> tuple[Path, Path]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"run_{result.run_id or 0}_{stamp}"
    md_path = out / f"{base}.md"
    json_path = out / f"{base}.json"

    md = render_markdown(result, settings)
    md_path.write_text(md, encoding="utf-8")

    data = {
        "run_id": result.run_id,
        "metric": result.metric,
        "llm_enabled": result.llm_enabled,
        "llm_available": result.llm_available,
        "proposals": result.proposals,
        "stopped": result.stopped,
        "notes": result.notes,
        "generations": result.generations,
        "champion": result.champion.genome if result.champion else None,
        "champion_fitness": result.champion.fitness if result.champion else None,
        "holdout": result.holdout,
        "autopsy": result.autopsy,
        "critic": result.critic,
        "regime_luck_warning": result.regime_warning,
        "repro": result.repro,
        "versions": result.versions,
        "settings": as_dict(settings),
    }
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return md_path, json_path


def _short_value(value: Any) -> str:
    """A diff value short enough for a table cell, with no promise of precision."""
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def render_markdown(result: EvolutionResult, settings: Settings) -> str:
    L: list[str] = []
    L.append(f"# selftrade run report #{result.run_id}")
    L.append("")
    L.append(f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"- data source: `{settings.data.source}` symbol `{settings.data.symbol}` timeframe `{settings.data.timeframe}`")
    r = result.repro or {}
    ds = r.get("dataset") or {}
    L.append(f"- reproducibility id: `{r.get('repro_id') or 'N/D'}`")
    L.append(
        f"- code: commit `{r.get('commit') or 'N/D'}`"
        + (f" ({r.get('branch')})" if r.get("branch") else "")
        + (" - **worktree dirty**" if r.get("dirty") else "")
    )
    L.append(
        f"- dataset: `{ds.get('short') or 'N/D'}`"
        + (f" from `{ds.get('path')}`" if ds.get("path") else f" by {ds.get('method')} hash")
        + (f", {ds.get('rows')} rows" if ds.get("rows") else "")
    )
    if r and not r.get("reproducible", False) and r.get("repro_id"):
        L.append(
            "  - **not reproducible from the commit alone**: the worktree had "
            "uncommitted changes when this run started."
        )
    L.append(f"- optimisation metric: `{result.metric}`")
    L.append(
        f"- LLM (Ollama): enabled={result.llm_enabled} available={result.llm_available}"
    )
    if not result.llm_available:
        L.append(
            "- **proposals were deterministic** (Ollama unavailable or disabled); "
            "this run did not use the LLM."
        )
    L.append(f"- proposal origins: {result.proposals}")
    if result.stopped:
        L.append("- **run stopped early by session control; report written anyway.**")
    L.append("")

    L.append("## Per-generation results")
    L.append("")
    for gen in result.generations:
        L.append(
            f"### Generation {gen['generation']} — best fitness {gen['best_fitness']:.3f} "
            f"({gen['kept']} kept)"
        )
        L.append("")
        L.append("| family | origin | fitness | n_trades | maxDD | gate | reason |")
        L.append("|---|---|---|---|---|---|---|")
        for c in gen["candidates"]:
            L.append(
                f"| {c['family']} | {c['origin']} | {c['fitness']:.3f} | {c['n_trades']} | "
                f"{c['max_drawdown']:.3f} | {'KEEP' if c['passed'] else 'drop'} | {c['reason'] or 'ok'} |"
            )
        L.append("")
        L.append(f"Lesson: {gen['lesson']}")
        L.append("")

    L.append("## Champion")
    L.append("")
    if result.champion is None:
        L.append(
            "**No candidate passed every gate.** Nothing was promoted to the hall of fame."
        )
    else:
        L.append("```json")
        L.append(json.dumps(result.champion.genome, indent=2, sort_keys=True))
        L.append("```")
        L.append("")
        L.append(
            f"fitness {result.champion.fitness:.3f} over "
            f"{len(result.champion.fold_metrics)} walk-forward folds; "
            f"n_trades={result.champion.n_trades}, max_drawdown={result.champion.max_drawdown:.3f}."
        )
    L.append("")

    L.append("## Holdout (read once, out of sample)")
    L.append("")
    if result.holdout is None:
        L.append("Holdout not evaluated (no candidate available).")
    else:
        h = result.holdout
        L.append(f"- holdout reads this run: **{h['read_count']}**")
        L.append(
            f"- {h['metric']}: holdout **{h['metric_value']:.3f}** vs fold mean "
            f"**{h['fold_mean']:.3f}** over folds {[round(v, 3) for v in h['fold_values']]}"
        )
        L.append(f"- holdout trades: {h['n_trades']}")
        L.append(f"- evaluated the {'champion' if h['was_champion'] else 'best-scoring candidate'}")
        if h["regime_luck_warning"]:
            L.append(f"- **regime-luck warning**: {h['regime_luck_warning']}")
    L.append("")

    L.append("## Critic: may this be deployed?")
    L.append("")
    if result.critic is None:
        L.append("No verdict (no candidate to judge).")
    else:
        L.append(critic_mod.render_markdown(result.critic))
    L.append("")

    if result.autopsy is None:
        L.append("## Autopsy")
        L.append("")
        L.append("Not run (no candidate to dissect).")
        L.append("")
    else:
        L.append(autopsy_mod.render_markdown(result.autopsy))

    L.append("## Strategy versions (parent -> candidate)")
    L.append("")
    if not result.versions:
        L.append("No version was recorded for this run.")
    else:
        L.append("| v | gen | family | origin | fitness | vs parent | outcome | change |")
        L.append("|---|---|---|---|---|---|---|---|")
        for v in result.versions:
            delta = v.get("delta_fitness")
            delta_txt = "N/D" if delta is None else f"{delta:+.3f}"
            change = "; ".join(
                f"{d['key']}: {_short_value(d['from'])} -> {_short_value(d['to'])}"
                for d in (v.get("diff") or [])[:4]
            )
            if len(v.get("diff") or []) > 4:
                change += f" (+{len(v['diff']) - 4} more)"
            if not change:
                # No parent at all is not the same as a parent it did not move away from.
                change = (
                    "unchanged (elite carried over)"
                    if v.get("parent_fitness") is not None
                    else "root genome (no parent)"
                )
            L.append(
                f"| {v.get('version')} | {v.get('generation')} | {v.get('family')} | "
                f"{v.get('origin')} | {v.get('fitness'):.3f} | {delta_txt} | "
                f"{v.get('outcome')} | {change} |"
            )
        L.append("")

    L.append("## Notes and limits")

    L.append("")
    for n in result.notes:
        L.append(f"- {n}")
    L.append(
        "- Numbers on synthetic data are **not market results**; they only exercise the pipeline."
    )
    L.append(
        "- The backtester does not model overnight swap/financing or FX contract specifications."
    )
    L.append("")
    return "\n".join(L)
