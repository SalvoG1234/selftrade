import json
from pathlib import Path

import pytest

from selftrade import rea as rea_mod
from selftrade.config import load_settings
from selftrade.memory import Memory


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.rea.enabled = True
    s.rea.dry_run = True
    s.rea.targets = [
        {"name": "eurusd-feed", "path": "C:/artifacts/feed.exe", "question": "how is close computed?", "authorized": True},
        {"name": "unpaid-thing", "path": "C:/artifacts/other.exe", "question": "?", "authorized": False},
    ]
    return s


def test_disabled_rea_reports_disabled(tmp_path):
    s = load_settings("config.yaml")
    assert s.rea.enabled is False
    assert "disabled" in rea_mod.doctor_line(s)
    mem = Memory(tmp_path / "m.db")
    out = rea_mod.run(s, mem, live=True)
    assert out["enabled"] is False


def test_plan_blocks_unauthorized(settings):
    info = rea_mod.plan(settings)
    statuses = {t["id"]: t["status"] for t in info["targets"]}
    assert statuses["eurusd-feed"] == "would run"
    assert "BLOCKED" in statuses["unpaid-thing"]
    assert info["authorized_count"] == 1
    assert info["blocked_count"] == 1


def test_dry_run_does_not_execute(settings):
    mem = Memory(settings.memory.db)
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return 0, "should not be called"

    out = rea_mod.run(settings, mem, live=True, runner=runner)
    # rea.dry_run is True, so even with live=True nothing executes.
    assert calls == []
    assert out["dry_run"] is True
    assert out["executed"][0]["dry_run"] is True
    assert mem.lessons() == []


def test_unauthorized_target_never_runs(settings):
    settings.rea.dry_run = False
    mem = Memory(settings.memory.db)
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return 0, "evidence: close = (bid+ask)/2"

    out = rea_mod.run(settings, mem, live=True, runner=runner)
    # Only the authorized target runs.
    assert out["blocked"] == ["unpaid-thing"]
    assert len(calls) == 1
    assert "eurusd-feed" not in " ".join(out["blocked"])


def test_live_run_stores_evidence(settings):
    settings.rea.dry_run = False
    mem = Memory(settings.memory.db)

    def runner(cmd):
        return 0, "how it works: the feed uses mid prices"

    out = rea_mod.run(settings, mem, live=True, runner=runner)
    assert out["dry_run"] is False
    lessons = mem.lessons()
    assert any("rea" in (l["tags"] or []) for l in lessons)
    ev = rea_mod.prompt_evidence(mem)
    assert ev and "mid prices" in ev[0]


def test_failed_run_does_not_store_evidence(settings):
    settings.rea.dry_run = False
    mem = Memory(settings.memory.db)
    rea_mod.run(settings, mem, live=True, runner=lambda cmd: (1, "boom"))
    assert mem.lessons() == []


def test_evidence_reaches_proposal_prompt(settings):
    settings.rea.dry_run = False
    mem = Memory(settings.memory.db)
    rea_mod.run(settings, mem, live=True, runner=lambda cmd: (0, "evidence about spread"))
    from selftrade.propose import build_user_prompt

    prompt = build_user_prompt(None, 0, [], evidence=rea_mod.prompt_evidence(mem))
    assert "evidence about spread" in prompt


def test_target_filter(settings):
    settings.rea.dry_run = False
    mem = Memory(settings.memory.db)
    out = rea_mod.run(settings, mem, live=True, target=0, runner=lambda cmd: (0, "x"))
    assert len(out["executed"]) == 1
    out = rea_mod.run(settings, mem, live=True, target=99, runner=lambda cmd: (0, "x"))
    assert "error" in out


def test_mcp_config_contents(settings):
    cfg = rea_mod.mcp_config(settings)
    assert "rea" in cfg["mcpServers"] and "ruflo" in cfg["mcpServers"]
    agents = cfg["selftrade"]["agents"]
    assert len(agents) >= 3
    for a in agents:
        assert a["inputs"] and a["outputs"] and a["acceptance"]
    assert cfg["selftrade"]["missions"]


def test_write_mcp_config(settings, tmp_path):
    path = rea_mod.write_mcp_config(settings, str(tmp_path / "mcp.json"))
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["mcpServers"]["ruflo"]["command"] == settings.rea.node_cmd
