import numpy as np
import pytest

from selftrade.config import load_settings
from selftrade.data import make_synthetic, split_holdout
from selftrade.evaluate import holdout_eval
from selftrade.genome import sample_genome
from selftrade.memory import Memory
from selftrade.paper import paper_status, run_session


@pytest.fixture(scope="module")
def df():
    return make_synthetic(3000, seed=55)


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.backtest.fee_bps = 5.0
    s.backtest.slippage_bps = 2.0
    return s


def make_session(memory, settings, df, genome, window_df):
    return memory.create_paper_session(
        name="test",
        genome=genome,
        window="custom",
        total_bars=len(window_df),
        start_time=str(window_df.index[0]),
        end_time=str(window_df.index[-1]),
        initial_cash=settings.backtest.initial_cash,
        settings={
            "fee_bps": settings.backtest.fee_bps,
            "slippage_bps": settings.backtest.slippage_bps,
            "position_size": settings.backtest.position_size,
            "allow_short": settings.backtest.allow_short,
            "timeframe": settings.data.timeframe,
        },
    )


GENOME = sample_genome("trend_momentum", np.random.default_rng(3))


def test_one_shot_equals_many_steps(settings, df, tmp_path):
    win = df.iloc[1000:2200]
    m1 = Memory(tmp_path / "a.db")
    s1 = make_session(m1, settings, df, GENOME, win)
    settings.paper.step_bars = len(win)
    st1 = run_session(settings, m1, s1, mode="all", df=df)

    m2 = Memory(tmp_path / "b.db")
    s2 = make_session(m2, settings, df, GENOME, win)
    settings.paper.step_bars = 137
    st2 = run_session(settings, m2, s2, mode="all", df=df)

    assert st1["n_trades"] == st2["n_trades"]
    assert st1["total_return"] == pytest.approx(st2["total_return"])
    t1 = [(t["entry_time"], t["exit_time"], t["pnl"]) for t in m1.paper_trades(s1)]
    t2 = [(t["entry_time"], t["exit_time"], t["pnl"]) for t in m2.paper_trades(s2)]
    assert t1 == t2
    e1 = [p["equity"] for p in m1.paper_equity(s1)]
    e2 = [p["equity"] for p in m2.paper_equity(s2)]
    assert e1 == pytest.approx(e2)


def test_restart_continues_identically(settings, df, tmp_path):
    win = df.iloc[800:2000]
    path = tmp_path / "r.db"

    # Phase 1: a few steps, then "restart" (new Memory object on the same file).
    m = Memory(path)
    sid = make_session(m, settings, df, GENOME, win)
    settings.paper.step_bars = 100
    for _ in range(3):
        run_session(settings, m, sid, mode="step", df=df)
    partial = paper_status(settings, m, sid)
    assert partial["status"] == "active"
    assert 0 < partial["cursor"] < len(win)

    m2 = Memory(path)  # simulate a process restart
    run_session(settings, m2, sid, mode="all", df=df)
    restarted = paper_status(settings, m2, sid)

    # Reference: same window, one shot in a fresh db.
    ref = Memory(tmp_path / "ref.db")
    sid_ref = make_session(ref, settings, df, GENOME, win)
    settings.paper.step_bars = len(win)
    run_session(settings, ref, sid_ref, mode="all", df=df)
    reference = paper_status(settings, ref, sid_ref)

    assert restarted["cursor"] == reference["cursor"] == len(win)
    assert restarted["n_trades"] == reference["n_trades"]
    assert restarted["total_return"] == pytest.approx(reference["total_return"])

    def trades(mem, s):
        return [(t["entry_time"], t["exit_time"], t["pnl"]) for t in mem.paper_trades(s)]

    assert trades(m2, sid) == trades(ref, sid_ref)


def test_paper_matches_holdout_for_same_genome(settings, df, tmp_path):
    dev, hold = split_holdout(df, 0.2)
    m = Memory(tmp_path / "h.db")
    sid = make_session(m, settings, df, GENOME, hold)
    settings.paper.step_bars = 200
    st = run_session(settings, m, sid, mode="all", df=df)
    ref = holdout_eval(hold, GENOME, settings)
    assert st["n_trades"] == ref["n_trades"]
    assert st["total_return"] == pytest.approx(ref["metrics"]["total_return"])
    assert st["metrics"]["sharpe"] == pytest.approx(ref["metrics"]["sharpe"])


def test_status_progress_and_equity(settings, df, tmp_path):
    win = df.iloc[500:1500]
    m = Memory(tmp_path / "s.db")
    sid = make_session(m, settings, df, GENOME, win)
    settings.paper.step_bars = 250
    st = run_session(settings, m, sid, mode="step", df=df)
    assert st["cursor"] == 250
    assert st["equity_points"] == 250
    assert st["status"] == "active"
    st = run_session(settings, m, sid, mode="all", df=df)
    assert st["status"] == "done"
    assert st["equity_points"] == len(win)


def test_max_ticks_limits_steps(settings, df, tmp_path):
    win = df.iloc[500:2000]
    m = Memory(tmp_path / "t.db")
    sid = make_session(m, settings, df, GENOME, win)
    settings.paper.step_bars = 100
    settings.paper.max_ticks = 2
    st = run_session(settings, m, sid, mode="all", df=df)
    assert st["cursor"] == 200


def test_reset_clears_results(settings, df, tmp_path):
    win = df.iloc[500:1200]
    m = Memory(tmp_path / "z.db")
    sid = make_session(m, settings, df, GENOME, win)
    settings.paper.step_bars = 200
    run_session(settings, m, sid, mode="all", df=df)
    assert len(m.paper_trades(sid)) >= 0
    m.reset_paper_session(sid)
    st = paper_status(settings, m, sid)
    assert st["cursor"] == 0
    assert st["n_trades"] == 0
