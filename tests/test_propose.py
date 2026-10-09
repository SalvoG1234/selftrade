import json

import numpy as np
import pytest

from selftrade.config import load_settings
from selftrade.genome import sample_genome, validate_genome
from selftrade.memory import Memory
from selftrade.propose import build_user_prompt, propose_next, retrieve_lessons
from tests.test_llm import STUB, Handler  # reuse the stub server machinery

from http.server import ThreadingHTTPServer
import threading


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture()
def settings():
    s = load_settings("config.yaml")
    s.llm.enabled = True
    return s


def make_client(server):
    from selftrade.llm import OllamaClient

    return OllamaClient(server, timeout=5.0)


def test_prompt_contains_genome_and_ranges():
    best = sample_genome("rsi_reversion", np.random.default_rng(1))
    p = build_user_prompt(best, 2, ["lesson one"], ["evidence one"], mode="params")
    assert "rsi_reversion" in p
    assert "rsi_period" in p
    assert "lesson one" in p
    assert "evidence one" in p


def test_prompt_without_best_lists_families():
    p = build_user_prompt(None, 0, [])
    assert "No champion yet" in p


def test_propose_seed_when_no_best(settings):
    prop = propose_next(None, np.random.default_rng(1), settings, use_llm=False)
    assert prop.origin == "seed"
    assert prop.genome == validate_genome(prop.genome)


def test_propose_deterministic_when_llm_disabled(settings):
    best = sample_genome("trend_momentum", np.random.default_rng(2))
    prop = propose_next(best, np.random.default_rng(3), settings, use_llm=False)
    assert prop.origin == "deterministic"
    assert prop.llm_used is False
    assert prop.genome == validate_genome(prop.genome)


def test_propose_uses_llm(server, settings):
    STUB["chat_content"] = json.dumps(
        {
            "family": "rsi_reversion",
            "params": {"rsi_buy": 22, "rsi_exit": 65},
            "risk": {"stop_loss_pct": -0.03},
            "rationale": "tighten the entry",
        }
    )
    best = sample_genome("rsi_reversion", np.random.default_rng(4))
    prop = propose_next(best, np.random.default_rng(5), settings, llm=make_client(server))
    assert prop.origin == "llm"
    assert prop.llm_used is True
    assert prop.genome["params"]["rsi_buy"] == pytest.approx(22)
    assert "tighten" in prop.rationale


def test_llm_output_is_clamped(server, settings):
    # A wildly illegal LLM genome must come back clamped and legal.
    STUB["chat_content"] = json.dumps(
        {
            "family": "rsi_reversion",
            "params": {"rsi_buy": -999, "rsi_exit": 9999, "bogus": 1},
            "risk": {"stop_loss_pct": -9.0},
            "rationale": "chaos",
        }
    )
    best = sample_genome("rsi_reversion", np.random.default_rng(6))
    prop = propose_next(best, np.random.default_rng(7), settings, llm=make_client(server))
    assert prop.origin == "llm"
    assert prop.genome == validate_genome(prop.genome)
    assert prop.genome["params"]["rsi_buy"] < prop.genome["params"]["rsi_exit"]
    assert -0.25 <= prop.genome["risk"]["stop_loss_pct"] <= -0.008


def test_llm_unknown_family_falls_back(server, settings):
    STUB["chat_content"] = json.dumps({"family": "made_up_family", "params": {}})
    best = sample_genome("rsi_reversion", np.random.default_rng(8))
    prop = propose_next(best, np.random.default_rng(9), settings, llm=make_client(server))
    assert prop.origin == "deterministic"
    assert prop.rejected_llm is True
    assert "rejected" in prop.rationale


def test_llm_down_falls_back(settings):
    from selftrade.llm import OllamaClient

    dead = OllamaClient("http://127.0.0.1:1", timeout=0.3)
    best = sample_genome("rsi_reversion", np.random.default_rng(10))
    prop = propose_next(best, np.random.default_rng(11), settings, llm=dead)
    assert prop.origin == "deterministic"
    assert prop.llm_used is False


def test_retrieve_lessons_without_llm_returns_recent(tmp_path):
    mem = Memory(tmp_path / "m.db")
    for i in range(3):
        mem.add_lesson(1, f"lesson{i}")
    out = retrieve_lessons(mem, "query", None, k=2)
    assert len(out) == 2
    assert out[0] == "lesson2"


def test_retrieve_lessons_semantic(tmp_path, server):
    mem = Memory(tmp_path / "m.db")
    mem.add_lesson(1, "alpha", embedding=[1.0, 0.0, 0.0])
    mem.add_lesson(1, "beta", embedding=[0.0, 1.0, 0.0])
    STUB["embed"] = [1.0, 0.0, 0.0]
    out = retrieve_lessons(mem, "query", make_client(server), k=1)
    assert out == ["alpha"]
