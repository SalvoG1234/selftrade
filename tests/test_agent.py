import json
from pathlib import Path

import numpy as np
import pytest

from selftrade.agent import Evolver, render_markdown
from selftrade.config import load_settings
from selftrade.data import make_synthetic
from selftrade.memory import Memory


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.data.min_rows = 100
    s.evolution.generations = 2
    s.evolution.population = 4
    s.evolution.elite = 2
    s.evolution.seed = 7
    s.evaluation.min_trades = 1
    s.evaluation.max_drawdown = 0.99
    s.memory.db = str(tmp_path / "state" / "m.db")
    return s


@pytest.fixture(scope="module")
def df():
    return make_synthetic(2500, seed=33)


def run(settings, df, tmp_path, generations=2, quiet=True):
    mem = Memory(settings.memory.db)
    ev = Evolver(settings, memory=mem, df=df, log=lambda *_: None, report_dir=str(tmp_path / "reports"))
    return ev.run(generations=generations, use_llm=False, quiet=quiet), mem


@pytest.fixture()
def ran(settings, df, tmp_path):
    """One run, shared by the ``result``/``mem`` fixtures below (a run is not cheap)."""
    return run(settings, df, tmp_path)


@pytest.fixture()
def result(ran):
    return ran[0]


@pytest.fixture()
def mem(ran):
    return ran[1]


def test_run_produces_report_and_memory(settings, df, tmp_path):
    result, mem = run(settings, df, tmp_path)
    assert result.run_id is not None
    assert len(result.candidates) >= 2
    assert result.report_md and Path(result.report_md).exists()
    assert result.report_json and Path(result.report_json).exists()
    assert mem.stats()["candidates"] == len(result.candidates)
    # one lesson per generation, plus the autopsy lesson of the champion
    assert mem.stats()["lessons"] == len(result.generations) + 1
    tags = {t for lesson in mem.lessons() for t in lesson["tags"]}
    assert "autopsy" in tags


def test_run_records_autopsy_and_critic(settings, df, tmp_path):
    result, mem = run(settings, df, tmp_path)
    assert result.autopsy is not None
    assert result.critic is not None
    assert result.autopsy["summary"]
    assert isinstance(result.critic["approved"], bool)
    assert result.critic["checks"]
    md = Path(result.report_md).read_text()
    assert "## Critic" in md
    assert "## Autopsy" in md
    assert result.autopsy["summary"] in md


def test_report_json_shape(settings, df, tmp_path):
    result, _ = run(settings, df, tmp_path)
    data = json.loads(Path(result.report_json).read_text())
    assert data["run_id"] == result.run_id
    assert data["llm_available"] is False
    assert "generations" in data
    assert "holdout" in data
    assert data["autopsy"]["n_trades"] == result.autopsy["n_trades"]
    assert "approved" in data["critic"]


def test_report_declares_deterministic(settings, df, tmp_path):
    result, _ = run(settings, df, tmp_path)
    md = Path(result.report_md).read_text()
    assert "deterministic" in md
    assert "Holdout" in md
    assert "read once" in md


def test_holdout_read_once_during_run(settings, df, tmp_path):
    result, _ = run(settings, df, tmp_path)
    assert result.holdout is not None
    assert result.holdout["read_count"] == 1


def test_gate_failures_recorded_with_reason(settings, df, tmp_path):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evolution.population = 3
    s.evaluation.min_trades = 100_000  # nothing can pass
    s.memory.db = str(tmp_path / "state" / "m2.db")
    result, mem = run(s, df, tmp_path, generations=1)
    assert result.champion is None
    assert all(not c.passed for c in result.candidates)
    rows = mem.candidates_for_run(result.run_id)
    assert all(not r["passed"] and "min_trades" in r["reason"] for r in rows)
    assert mem.hall_of_fame() == []


def test_hall_only_gate_passers(settings, df, tmp_path):
    result, mem = run(settings, df, tmp_path)
    hall = mem.hall_of_fame()
    # Every hall entry must correspond to a candidate that passed the gate.
    assert len(hall) == (1 if result.champion else 0)
    if result.champion:
        assert hall[0]["fitness"] == pytest.approx(result.champion.fitness)


def test_reproducibility_same_seed(settings, df, tmp_path):
    s1 = load_settings("config.yaml")
    s1.data.source = "synthetic"
    s1.evolution.population = 4
    s1.evolution.seed = 7
    s1.evaluation.min_trades = 1
    s1.evaluation.max_drawdown = 0.99
    s1.memory.db = str(tmp_path / "s1" / "m.db")
    s2 = load_settings("config.yaml")
    s2.data.source = "synthetic"
    s2.evolution.population = 4
    s2.evolution.seed = 7
    s2.evaluation.min_trades = 1
    s2.evaluation.max_drawdown = 0.99
    s2.memory.db = str(tmp_path / "s2" / "m.db")

    r1, _ = run(s1, df, tmp_path / "r1", generations=2)
    r2, _ = run(s2, df, tmp_path / "r2", generations=2)
    assert r1.champion is not None and r2.champion is not None
    assert r1.champion.genome == r2.champion.genome
    assert r1.champion.fitness == pytest.approx(r2.champion.fitness)
    assert r1.holdout["metrics"] == r2.holdout["metrics"]


def test_proposal_origins_tracked(settings, df, tmp_path):
    result, _ = run(settings, df, tmp_path)
    assert result.proposals["seed"] >= 1
    assert result.proposals["llm"] == 0


def test_markdown_contains_tables(settings, df, tmp_path):
    result, _ = run(settings, df, tmp_path)
    md = render_markdown(result, settings)
    assert "| family | origin |" in md
    assert "Generation 0" in md


# ------------------------------------------------- versioning and provenance


def test_every_candidate_is_recorded_as_a_version(mem, result):
    rows = mem.strategy_versions(result.run_id)
    assert len(rows) == len(result.candidates)
    # numbered in evaluation order, one per candidate
    assert [r["version"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["generation"] is not None and r["origin"] for r in rows)


def test_seeds_are_rooted_and_later_versions_name_their_parent(mem, result):
    rows = mem.strategy_versions(result.run_id)
    gen0 = [r for r in rows if r["generation"] == 0]
    later = [r for r in rows if r["generation"] != 0]
    # Generation 0 is sampled from nothing: those versions have no parent to diff against.
    assert gen0 and all(r["parent"] is None and r["diff"] == [] for r in gen0)
    assert all(r["parent_fitness"] is None for r in gen0)
    # Every later version points at the genome it came from - either the elite it was
    # carried over from (identical, so nothing changed) or the best it was mutated out of.
    assert later
    assert all(r["parent"] is not None and r["parent_fitness"] is not None for r in later)
    assert any(r["diff"] for r in later), "a mutation must show what it changed"
    assert any(r["parent"] is not None and r["diff"] == [] for r in later)
    # The stored diff must agree with the two stored genomes it claims to relate: a
    # key that says "10 -> 12" has to be 10 in the parent and 12 in the child.
    changed = next(r for r in later if r["diff"])
    for d in changed["diff"]:
        if d["key"] == "family":
            assert d["from"] == changed["parent"]["family"]
            assert d["to"] == changed["genome"]["family"]
        elif d["key"].startswith("risk."):
            leaf = d["key"].split(".", 1)[1]
            assert d["from"] == changed["parent"]["risk"][leaf]
            assert d["to"] == changed["genome"]["risk"][leaf]
        else:
            assert d["from"] == changed["parent"]["params"][d["key"]]
            assert d["to"] == changed["genome"]["params"][d["key"]]


def test_the_champion_is_labelled_in_the_version_history(mem, result):
    rows = mem.strategy_versions(result.run_id)
    champions = [r for r in rows if r["outcome"] == "champion"]
    if result.champion is None:
        assert champions == []
        return
    assert len(champions) == 1
    assert champions[0]["fitness"] == pytest.approx(result.champion.fitness)
    assert champions[0]["genome"] == result.champion.genome
    assert [r["outcome"] for r in rows].count("dropped") == sum(
        1 for c in result.candidates if not c.passed
    )


def test_delta_fitness_is_the_step_against_the_parent(mem, result):
    rows = mem.strategy_versions(result.run_id)
    for r in rows:
        if r["parent_fitness"] is None:
            assert r["delta_fitness"] is None
        else:
            assert r["delta_fitness"] == pytest.approx(r["fitness"] - r["parent_fitness"])


def test_the_run_stores_its_provenance(mem, result, df):
    run = mem.get_run(result.run_id)
    assert result.repro is not None
    assert run["repro_id"] == result.repro["repro_id"] and run["repro_id"]
    assert run["dataset_sha"] == result.repro["dataset"]["sha256"]
    assert run["dataset_rows"] == len(df)
    # synthetic prices never touched a file: the hash is over the frame it trained on
    assert result.repro["dataset"]["method"] == "frame"
    # this test machine's worktree carries the agent's own edits, so the commit alone
    # does not pin the code - the run must say so rather than claim reproducibility
    if result.repro["dirty"]:
        assert run["reproducible"] is False
        assert any("uncommitted changes" in n for n in result.notes)


def test_same_code_and_data_share_a_reproducibility_id(settings, df, tmp_path):
    first, mem1 = run(settings, df, tmp_path / "a")
    second, mem2 = run(settings, df, tmp_path / "b")
    assert first.repro["repro_id"] == second.repro["repro_id"]
    assert mem1.get_run(first.run_id)["repro_id"] == mem2.get_run(second.run_id)["repro_id"]


def test_a_different_dataset_gets_a_different_id(settings, df, tmp_path):
    first, _ = run(settings, df, tmp_path / "a")
    other = make_synthetic(2500, seed=99)
    second, _ = run(settings, other, tmp_path / "b")
    assert first.repro["repro_id"] != second.repro["repro_id"]


def test_report_carries_versions_and_provenance(settings, df, tmp_path):
    result, _ = run(settings, df, tmp_path)
    data = json.loads(Path(result.report_json).read_text())
    assert data["repro"]["repro_id"] == result.repro["repro_id"]
    assert len(data["versions"]) == len(result.candidates)
    assert data["versions"][0]["version"] == 1
    assert "diff" in data["versions"][0]
    md = Path(result.report_md).read_text()
    assert "## Strategy versions (parent -> candidate)" in md
    assert "reproducibility id" in md
    # a generation-0 seed has no parent, and the table says exactly that
    assert "root genome (no parent)" in md
    if any(c.parent_fitness is not None and not c.diff for c in result.candidates):
        assert "unchanged (elite carried over)" in md
    # a changed parameter is a readable "from -> to" in the stored table
    changed = next((c for c in result.candidates if c.diff), None)
    if changed is not None:
        key = changed.diff[0]["key"]
        assert f"{key}:" in md and "->" in md
