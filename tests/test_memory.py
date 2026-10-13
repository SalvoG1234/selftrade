import sqlite3

import numpy as np
import pytest

from selftrade.genome import sample_genome
from selftrade.memory import SCHEMA_VERSION, Memory


@pytest.fixture()
def mem(tmp_path):
    return Memory(tmp_path / "state" / "test.db")


def test_schema_created(mem):
    assert mem.schema_version() == SCHEMA_VERSION
    assert mem.stats()["runs"] == 0


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "m.db"
    Memory(path)
    m2 = Memory(path)  # opening again must not wipe data
    assert m2.schema_version() == SCHEMA_VERSION


def test_run_lifecycle(mem):
    rid = mem.start_run("hello", {"a": 1})
    assert rid > 0
    run = mem.get_run(rid)
    assert run["note"] == "hello"
    assert run["outcome"] == "running"
    mem.finish_run(rid, {"best": 0.3}, "ok")
    run = mem.get_run(rid)
    assert run["outcome"] == "ok"
    assert run["summary"]["best"] == 0.3
    assert mem.last_run()["id"] == rid


def test_candidates_recorded_with_gate_reason(mem):
    rid = mem.start_run("evolve")
    g = sample_genome("rsi_reversion", np.random.default_rng(1))
    mem.add_candidate(rid, 0, g, "seed", [{"sharpe": 0.1}], 0.2, False, "n_trades=2 < min_trades=8")
    mem.add_candidate(rid, 0, g, "deterministic", [{"sharpe": 0.9}], 0.8, True, "")
    rows = mem.candidates_for_run(rid)
    assert len(rows) == 2
    assert rows[0]["passed"] is False
    assert "min_trades" in rows[0]["reason"]
    assert rows[1]["passed"] is True
    assert rows[1]["origin"] == "deterministic"
    assert rows[1]["fold_metrics"][0]["sharpe"] == 0.9


def test_hall_only_holds_gate_passers_and_champion(mem):
    g1 = sample_genome("trend_momentum", np.random.default_rng(2))
    g2 = sample_genome("donchian_breakout", np.random.default_rng(3))
    mem.add_to_hall(1, g1, 0.5, {"sharpe": 0.5})
    mem.add_to_hall(1, g2, 1.5, {"sharpe": 1.5})
    champ = mem.champion()
    assert champ["family"] == "donchian_breakout"
    assert champ["fitness"] == 1.5
    assert champ["genome"]["family"] == "donchian_breakout"


def test_hall_min_fitness_filter(mem):
    g = sample_genome("rsi_reversion", np.random.default_rng(1))
    mem.add_to_hall(1, g, 0.5, {})
    mem.add_to_hall(1, g, 2.0, {})
    assert len(mem.hall_of_fame(min_fitness=1.0)) == 1


def test_no_champion_when_empty(mem):
    assert mem.champion() is None


def test_lessons_roundtrip(mem):
    e = [0.1, 0.2, 0.3]
    mem.add_lesson(1, "rsi_buy<20 failed", tags=["rsi", "fail"], question="why?", target="ea.exe", embedding=e)
    rows = mem.lessons()
    assert rows[0]["text"].startswith("rsi_buy")
    assert "rsi" in rows[0]["tags"]
    assert rows[0]["embedding"] == e
    with_emb = mem.lessons_with_embeddings()
    assert len(with_emb) == 1


def test_paper_session_lifecycle(mem):
    g = sample_genome("rsi_reversion", np.random.default_rng(4))
    sid = mem.create_paper_session(
        name="selftrade-btc",
        genome=g,
        window="holdout",
        total_bars=500,
        start_time="2021-01-01T00:00:00Z",
        end_time="2021-02-01T00:00:00Z",
        initial_cash=10000.0,
    )
    s = mem.get_paper_session(sid)
    assert s["cursor"] == 0
    assert s["status"] == "active"
    mem.add_paper_trades(sid, [{"direction": "long", "entry_time": "t1", "exit_time": "t2", "pnl": 5.0, "exit_reason": "signal"}])
    mem.add_paper_equity(sid, [("t1", 10000.0), ("t2", 10005.0)])
    mem.update_paper_cursor(sid, 168, 10005.0)
    s = mem.get_paper_session(sid)
    assert s["cursor"] == 168
    assert len(mem.paper_trades(sid)) == 1
    assert len(mem.paper_equity(sid)) == 2
    assert mem.active_paper_session()["id"] == sid


def test_paper_reset_is_explicit_and_clears(mem):
    g = sample_genome("rsi_reversion", np.random.default_rng(5))
    sid = mem.create_paper_session(
        name="x", genome=g, window="holdout", total_bars=100,
        start_time=None, end_time=None, initial_cash=5000.0,
    )
    mem.add_paper_trades(sid, [{"direction": "long", "pnl": 1.0, "exit_reason": "stop"}])
    mem.add_paper_equity(sid, [("t", 5001.0)])
    mem.update_paper_cursor(sid, 50, 5001.0)
    mem.reset_paper_session(sid)
    assert mem.paper_trades(sid) == []
    assert mem.paper_equity(sid) == []
    assert mem.get_paper_session(sid)["cursor"] == 0


def test_replace_paper_results_is_idempotent(mem):
    g = sample_genome("rsi_reversion", np.random.default_rng(6))
    sid = mem.create_paper_session(
        name="x", genome=g, window="holdout", total_bars=100,
        start_time=None, end_time=None, initial_cash=1000.0,
    )
    trades = [{"direction": "long", "pnl": 2.0, "exit_reason": "signal"}]
    equity = [("a", 1000.0), ("b", 1002.0)]
    mem.replace_paper_results(sid, trades, equity, 10, 1002.0)
    mem.replace_paper_results(sid, trades, equity, 10, 1002.0)
    assert len(mem.paper_trades(sid)) == 1
    assert len(mem.paper_equity(sid)) == 2


def test_stats(mem):
    rid = mem.start_run("x")
    g = sample_genome("rsi_reversion", np.random.default_rng(7))
    mem.add_candidate(rid, 0, g, "seed", [], 0.1, True, "")
    mem.add_to_hall(rid, g, 0.9, {})
    mem.add_lesson(rid, "note")
    s = mem.stats()
    assert s["runs"] == 1
    assert s["candidates"] == 1
    assert s["hall_of_fame"] == 1
    assert s["lessons"] == 1
    assert s["best_fitness"] == pytest.approx(0.9)


def test_history(mem):
    for i in range(3):
        mem.start_run(f"run{i}")
    hist = mem.history()
    assert len(hist) == 3
    assert hist[0]["note"] == "run2"


# ------------------------------------------------------------- provenance


def test_a_v1_database_is_migrated_in_place(tmp_path):
    """An existing database keeps its rows and gains the provenance columns."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
           note TEXT, config TEXT, summary TEXT, outcome TEXT);
           INSERT INTO runs(ts, note, config, summary, outcome)
           VALUES (1.0, 'old run', NULL, NULL, 'ok');"""
    )
    conn.commit()
    conn.close()

    mem = Memory(path)
    assert mem.schema_version() == SCHEMA_VERSION
    run = mem.last_run()
    assert run["note"] == "old run"          # the history survived the migration
    assert run["repro_id"] is None           # and reads N/D rather than a guess
    assert run["reproducible"] is None


def test_run_stores_the_reproducibility_record(mem):
    repro = {
        "repro_id": "abc123",
        "reproducible": False,
        "commit": "f" * 40,
        "commit_short": "fffffff",
        "dataset": {"sha256": "e" * 64, "rows": 18000},
    }
    rid = mem.start_run("with provenance", None, repro=repro)
    run = mem.get_run(rid)
    assert run["repro_id"] == "abc123"
    assert run["commit_sha"] == "f" * 40
    assert run["code_version"] == "fffffff"
    assert run["dataset_sha"] == "e" * 64
    assert run["dataset_rows"] == 18000
    assert run["reproducible"] is False      # a dirty tree is recorded as such


def test_run_without_a_fingerprint_stores_nulls(mem):
    rid = mem.start_run("no provenance")
    run = mem.get_run(rid)
    assert run["repro_id"] is None and run["commit_sha"] is None


# --------------------------------------------------------- strategy versions


def test_strategy_version_records_parent_diff_and_delta(mem):
    parent = {"family": "trend_momentum", "params": {"ema_fast": 10, "ema_slow": 50},
              "risk": {"stop_loss_pct": -0.05}}
    child = {"family": "trend_momentum", "params": {"ema_fast": 12, "ema_slow": 50},
             "risk": {"stop_loss_pct": -0.05}}
    rid = mem.start_run("v")
    vid = mem.add_strategy_version(
        rid, genome=child, parent=parent, generation=1, version=2, origin="llm",
        rationale="faster entry", reason="", fitness=0.8, parent_fitness=0.5,
        passed=True, outcome="kept",
    )
    rows = mem.strategy_versions(rid)
    assert len(rows) == 1
    v = rows[0]
    assert v["id"] == vid and v["version"] == 2 and v["generation"] == 1
    assert v["family"] == "trend_momentum" and v["parent_family"] == "trend_momentum"
    assert v["delta_fitness"] == pytest.approx(0.3)
    assert [d["key"] for d in v["diff"]] == ["ema_fast"]
    assert v["diff"][0]["from"] == 10 and v["diff"][0]["to"] == 12
    assert v["genome_hash"] and v["genome_hash"] != v["parent_hash"]
    assert v["rationale"] == "faster entry"


def test_a_seed_version_has_no_parent(mem):
    rid = mem.start_run("v")
    g = sample_genome("rsi_reversion", np.random.default_rng(11))
    mem.add_strategy_version(rid, genome=g, origin="seed", version=1, outcome="dropped")
    v = mem.strategy_versions(rid)[0]
    assert v["parent"] is None and v["diff"] == [] and v["parent_fitness"] is None
    assert v["delta_fitness"] is None


def test_version_history_is_per_run_and_ordered(mem):
    g = sample_genome("rsi_reversion", np.random.default_rng(12))
    first = mem.start_run("a")
    second = mem.start_run("b")
    mem.add_strategy_version(first, genome=g, version=1, outcome="kept")
    mem.add_strategy_version(second, genome=g, version=1, outcome="dropped")
    mem.add_strategy_version(second, genome=g, version=2, outcome="kept")
    assert [v["version"] for v in mem.strategy_versions(second)] == [1, 2]
    assert len(mem.strategy_versions(first)) == 1
    assert len(mem.strategy_versions()) == 3
    assert mem.stats()["strategy_versions"] == 3


def test_version_outcome_can_be_relabelled(mem):
    rid = mem.start_run("v")
    g = sample_genome("rsi_reversion", np.random.default_rng(13))
    vid = mem.add_strategy_version(rid, genome=g, version=1, passed=True, outcome="kept")
    mem.set_version_outcome(vid, "champion")
    assert mem.strategy_versions(rid)[0]["outcome"] == "champion"


def test_latest_versions_cross_runs_newest_first(mem):
    g = sample_genome("rsi_reversion", np.random.default_rng(14))
    rid = mem.start_run("v")
    for i in range(3):
        mem.add_strategy_version(rid, genome=g, version=i + 1)
    latest = mem.latest_strategy_versions(limit=2)
    assert [v["version"] for v in latest] == [3, 2]


def test_a_bad_genome_does_not_break_a_version_write(mem):
    rid = mem.start_run("v")
    vid = mem.add_strategy_version(rid, genome={"family": "nope"}, version=1)
    assert vid > 0
    v = mem.strategy_versions(rid)[0]
    assert v["genome_hash"] is None and v["diff"] == []


# ------------------------------------------------- a store that cannot be read


def test_a_file_that_is_not_a_database_names_the_file_and_the_fix(tmp_path):
    """A bare `sqlite3.DatabaseError` told the operator nothing they could act on."""
    from selftrade.memory import StorageError

    bad = tmp_path / "state" / "not_a_db.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not sqlite, not even a little")
    with pytest.raises(StorageError) as raised:
        Memory(str(bad))
    message = str(raised.value)
    assert str(bad) in message
    assert "not a database" in message
    assert "doctor" in message, "the message must point at the command that diagnoses it"


def test_a_path_that_is_a_directory_is_reported_as_one(tmp_path):
    from selftrade.memory import StorageError

    folder = tmp_path / "as_a_dir"
    folder.mkdir()
    with pytest.raises(StorageError) as raised:
        Memory(str(folder))
    assert "directory" in str(raised.value)


def test_a_healthy_store_still_opens(tmp_path):
    mem = Memory(str(tmp_path / "state" / "ok.db"))
    assert mem.schema_version() == SCHEMA_VERSION
