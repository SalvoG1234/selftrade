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
