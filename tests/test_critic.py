"""Tests for the critic: the agent that refuses to deploy a breaking strategy."""

import json

import numpy as np
import pandas as pd
import pytest

from selftrade.config import load_settings
from selftrade.critic import judge, render_markdown, verdict_for_genome, worst_daily_loss
from selftrade.data import make_synthetic
from selftrade.genome import sample_genome, validate_genome
from selftrade.memory import Memory
from selftrade.paper import create_session
from selftrade.risk_rules import LIMITS, RiskLimits


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.evaluation.min_trades = 8
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.session_file = str(tmp_path / "state" / "session.json")
    return s


@pytest.fixture(scope="module")
def df():
    return make_synthetic(1200, seed=41)


def flat_equity(n=200, step=-0.001, freq="1D"):
    idx = pd.date_range("2022-01-01", periods=n, freq=freq, tz="UTC")
    return pd.Series(1000.0 * np.cumprod(np.full(n, 1.0 + step)), index=idx)


TIGHT = sample_genome("trend_momentum", np.random.default_rng(3))
TIGHT["risk"]["stop_loss_pct"] = -0.04
LOOSE = json.loads(json.dumps(TIGHT))
LOOSE["risk"]["stop_loss_pct"] = -0.25


def test_worst_daily_loss_reads_the_deepest_day():
    idx = pd.date_range("2022-01-01", periods=6, freq="12h", tz="UTC")
    equity = pd.Series([1000, 1000, 990, 990, 960, 970], index=idx)
    # daily closes: 1000, 990, 970 -> the third day is the worst (-2.02%)
    assert worst_daily_loss(equity) == pytest.approx(970 / 990 - 1.0)


def test_worst_daily_loss_never_raises_on_odd_input():
    assert worst_daily_loss(pd.Series([1.0, 2.0])) == 0.0
    assert worst_daily_loss(None) == 0.0
    assert worst_daily_loss(pd.Series([1.0, 2.0, 3.0])) == 0.0  # no datetime index


def test_approves_a_strategy_inside_the_limits(settings):
    v = judge(
        {"max_drawdown": 0.08, "n_trades": 40, "sharpe": 1.2},
        genome=TIGHT,
        equity=flat_equity(),
        settings=settings,
        apply_seal=False,
    )
    assert v.approved is True
    assert v.text.startswith("Approved")
    names = {c["name"] for c in v.checks}
    assert {"max_drawdown", "hard_stop", "max_daily_loss", "trades"} <= names


def test_refuses_the_drawdown_the_demo_showed(settings):
    v = judge(
        {"max_drawdown": 0.665, "n_trades": 214, "sharpe": 0.4},
        genome=TIGHT,
        equity=flat_equity(),
        settings=settings,
        apply_seal=False,
    )
    assert v.approved is False
    assert v.text.startswith("Refused")
    assert v.text.endswith("I will not deploy it.")
    assert any("66.5% drawdown breaks the sealed 15.0% limit" in r for r in v.reasons)


def test_refuses_a_loose_or_missing_stop(settings):
    loose = judge(
        {"max_drawdown": 0.05, "n_trades": 30},
        genome=LOOSE,
        equity=flat_equity(),
        settings=settings,
        apply_seal=False,
    )
    assert loose.approved is False
    assert any("looser than the sealed" in r for r in loose.reasons)

    # A validated genome can never be stop-less: validate_genome clamps 0 to the
    # tightest allowed stop (-0.008), which the critic then accepts.  The only
    # route to the "no stop" refusal is a genome-shaped dict with no risk rules
    # at all, which is what the critic guards against here.
    assert validate_genome({"family": "trend_momentum", "risk": {"stop_loss_pct": 0.0}})["risk"][
        "stop_loss_pct"
    ] == pytest.approx(-0.008)
    v = judge({"max_drawdown": 0.05, "n_trades": 30}, genome={}, settings=settings, apply_seal=False)
    assert v.approved is False
    assert any("no stop loss" in r for r in v.reasons)


def test_refuses_a_daily_halt_breach(settings):
    equity = flat_equity(120, step=0.0005)
    equity.iloc[60:] = equity.iloc[60] * 0.95  # one 5% day
    v = judge(
        {"max_drawdown": 0.06, "n_trades": 25},
        genome=TIGHT,
        equity=equity,
        settings=settings,
        apply_seal=False,
    )
    assert v.approved is False
    assert any("daily halt" in r for r in v.reasons)


def test_refuses_a_strategy_that_never_trades(settings):
    v = judge({"max_drawdown": 0.0, "n_trades": 0}, genome=TIGHT, settings=settings, apply_seal=False)
    assert v.approved is False
    assert any("never traded" in r for r in v.reasons)


def test_warns_on_a_thin_sample_and_on_concentration(settings):
    trades = pd.DataFrame(
        {
            "pnl": [100.0, 90.0, 80.0, -10.0, -10.0, -10.0, -10.0, -10.0],
            "exit_reason": ["target"] * 3 + ["stop"] * 5,
        }
    )
    v = judge(
        {"max_drawdown": 0.05, "n_trades": 5},
        genome=TIGHT,
        equity=flat_equity(),
        trades=trades,
        settings=settings,
        apply_seal=False,
    )
    assert any("too small a sample" in w for w in v.warnings)
    assert any("3 best trades" in w for w in v.warnings)
    assert any("research sizing" in w for w in v.warnings)


def test_a_broken_seal_refuses(settings):
    settings.session_file = str(settings.memory.db) + ".session.json"
    from selftrade.risk_rules import seal_path

    path = seal_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"digest": "0" * 64}), encoding="utf-8")
    v = judge({"max_drawdown": 0.05, "n_trades": 30}, genome=TIGHT, settings=settings)
    assert v.approved is False
    assert any("seal broken" in r for r in v.reasons)


def test_verdict_for_genome_judges_the_window(settings, df):
    verdict, res, cut = verdict_for_genome(settings, TIGHT, df, window="holdout", apply_seal=False)
    assert len(cut) < len(df)
    assert set(verdict.as_dict()) == {"approved", "text", "reasons", "warnings", "checks"}
    assert any(c["name"] == "window" for c in verdict.checks)
    assert res.metrics["n_trades"] >= 0


def test_render_markdown_accepts_the_dict_form(settings):
    v = judge({"max_drawdown": 0.05, "n_trades": 30}, genome=TIGHT, settings=settings, apply_seal=False)
    md = render_markdown(v.as_dict())
    assert "| check | value | limit | ok | detail |" in md
    assert "max_drawdown" in md


# ---------------------------------------------------- the deployment rail


def test_paper_refuses_a_genome_the_critic_rejected(settings, df, tmp_path, monkeypatch):
    mem = Memory(settings.memory.db)
    mem.add_to_hall(None, LOOSE, 1.0, {"metrics": {"max_drawdown": 0.8}})
    monkeypatch.setattr("selftrade.paper.load_dataframe", lambda s: df)

    with pytest.raises(RuntimeError) as exc:
        create_session(settings, mem, name="refused")
    assert "the critic refused this deployment" in str(exc.value)
    assert any("looser than the sealed" in str(exc.value) for _ in [0])
    tags = {t for lesson in mem.lessons() for t in lesson["tags"]}
    assert "critic" in tags and "refused" in tags
    assert mem.stats()["paper_sessions"] == 0


def test_paper_mechanics_can_bypass_the_gate_explicitly(settings, df, monkeypatch):
    mem = Memory(settings.memory.db)
    monkeypatch.setattr("selftrade.paper.load_dataframe", lambda s: df)
    sid = create_session(settings, mem, genome=LOOSE, name="mechanics", deploy_gate=False)
    assert sid == 1
    assert mem.stats()["paper_sessions"] == 1


def test_sealed_limits_are_the_ones_used(settings):
    v = judge({"max_drawdown": 0.16, "n_trades": 30}, genome=TIGHT, settings=settings, apply_seal=False)
    assert v.approved is False
    assert LIMITS.MAX_DRAWDOWN_PCT == 15.0
    assert RiskLimits().MAX_DRAWDOWN_PCT == 15.0
