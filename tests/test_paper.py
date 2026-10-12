import numpy as np
import pytest

from selftrade.backtest import simulate
from selftrade.config import load_settings
from selftrade.data import make_synthetic, split_holdout
from selftrade.genome import sample_genome, validate_genome
from selftrade.memory import Memory
from selftrade.paper import paper_status, run_session, session_settings
from selftrade.risk_rules import LIMITS, effective_position_size


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


def make_session(memory, settings, df, genome, window_df, position_size=None):
    cfg = session_settings(settings)
    if position_size is not None:  # a hand-written manifest, as an old session had it
        cfg["position_size"] = float(position_size)
    return memory.create_paper_session(
        name="test",
        genome=genome,
        window="custom",
        total_bars=len(window_df),
        start_time=str(window_df.index[0]),
        end_time=str(window_df.index[-1]),
        initial_cash=settings.backtest.initial_cash,
        settings=cfg,
    )


GENOME = sample_genome("trend_momentum", np.random.default_rng(3))
"""This one carries an 11% stop: outside the sealed 5% hard stop on purpose.

``paper --create`` refuses it (the critic checks the same limit), and now the rail
refuses its orders too - see ``test_the_session_refuses_what_the_sealed_layer_refuses``.
"""

TRADEABLE = validate_genome(
    {**GENOME, "risk": {**GENOME["risk"], "stop_loss_pct": -0.03}}
)
"""The same family with a stop the sealed layer allows: what a session may trade."""


def test_one_shot_equals_many_steps(settings, df, tmp_path):
    win = df.iloc[1000:2200]
    m1 = Memory(tmp_path / "a.db")
    s1 = make_session(m1, settings, df, TRADEABLE, win)
    settings.paper.step_bars = len(win)
    st1 = run_session(settings, m1, s1, mode="all", df=df)
    assert st1["n_trades"] > 0  # otherwise "identical" would mean "identically empty"

    m2 = Memory(tmp_path / "b.db")
    s2 = make_session(m2, settings, df, TRADEABLE, win)
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
    sid = make_session(m, settings, df, TRADEABLE, win)
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
    sid_ref = make_session(ref, settings, df, TRADEABLE, win)
    settings.paper.step_bars = len(win)
    run_session(settings, ref, sid_ref, mode="all", df=df)
    reference = paper_status(settings, ref, sid_ref)

    assert restarted["cursor"] == reference["cursor"] == len(win)
    assert restarted["n_trades"] == reference["n_trades"] > 0
    assert restarted["total_return"] == pytest.approx(reference["total_return"])

    def trades(mem, s):
        return [(t["entry_time"], t["exit_time"], t["pnl"]) for t in mem.paper_trades(s)]

    assert trades(m2, sid) == trades(ref, sid_ref)


def test_paper_matches_holdout_for_same_genome(settings, df, tmp_path):
    """The session agrees with the bar engine run at the session's own size.

    It no longer agrees with the *configured* size, and that is the point: the
    session is sized at the sealed cap (95% orders can only ever be refused), so the
    reference is the engine at that same fraction.
    """
    _, hold = split_holdout(df, 0.2)
    m = Memory(tmp_path / "h.db")
    sid = make_session(m, settings, df, TRADEABLE, hold)
    settings.paper.step_bars = 200
    st = run_session(settings, m, sid, mode="all", df=df)

    size = effective_position_size(settings)
    assert size == pytest.approx(LIMITS.MAX_POSITION_PCT / 100.0)
    assert size < settings.backtest.position_size  # the sealed cap does bite

    ref = simulate(
        hold,
        TRADEABLE,
        initial_cash=settings.backtest.initial_cash,
        fee_bps=settings.backtest.fee_bps,
        slippage_bps=settings.backtest.slippage_bps,
        position_size=size,
        allow_short=settings.backtest.allow_short,
        timeframe=settings.data.timeframe,
    )
    assert st["n_trades"] == int(ref.metrics["n_trades"])
    assert st["total_return"] == pytest.approx(ref.metrics["total_return"], rel=1e-9)
    assert st["metrics"]["sharpe"] == pytest.approx(ref.metrics["sharpe"], rel=1e-9)


def test_the_session_refuses_what_the_sealed_layer_refuses(settings, df, tmp_path):
    """A genome whose stop breaks the sealed limit cannot be paper-traded either.

    Every entry comes back refused with the sealed limit named, nothing is traded,
    and the refusals are recorded as risk events attributed to this session - the
    session has no way to trade an order the deployment gate would refuse.
    """
    win = df.iloc[1000:2000]
    m = Memory(tmp_path / "g.db")
    sid = make_session(m, settings, df, GENOME, win)
    settings.paper.step_bars = len(win)
    st = run_session(settings, m, sid, mode="all", df=df)

    assert GENOME["risk"]["stop_loss_pct"] == pytest.approx(-0.11, abs=0.01)
    assert st["n_trades"] == 0
    assert st["blocked_orders"] > 0
    assert m.paper_trades(sid) == []
    events = m.risk_events(limit=50, kind="order_blocked")
    assert events
    assert all(e["detail"]["paper_session"] == sid for e in events)
    assert all("sealed hard stop of 5.0%" in e["reason"] for e in events)


def test_the_gate_is_only_off_by_configuration(settings, df, tmp_path):
    """`risk_gate.enabled: false` is the only way past the gate - and it is a config
    change, not a flag on the paper command. With it off, the same genome and window
    that produced nothing but refusals trades again, which is what proves the
    refusals came from the gate."""
    win = df.iloc[1000:2000]
    m = Memory(tmp_path / "off.db")
    sid = make_session(m, settings, df, GENOME, win)
    settings.paper.step_bars = len(win)
    settings.risk_gate.enabled = False

    st = run_session(settings, m, sid, mode="all", df=df)

    assert st["n_trades"] > 0  # the 11% stop is no longer checked
    assert st["blocked_orders"] == 0
    assert m.risk_events(limit=5, kind="order_blocked") == []


def test_a_session_manifest_cannot_size_above_the_sealed_cap(settings, df, tmp_path):
    """A session that says 95% and one that says 5% trade identically.

    Sessions written before the rail existed still say ``position_size: 0.95``.  The
    rail clamps at run time rather than trusting the manifest, so they keep working
    instead of firing orders the gate must refuse.
    """
    win = df.iloc[1000:2200]
    settings.paper.step_bars = len(win)

    big = Memory(tmp_path / "big.db")
    small = Memory(tmp_path / "small.db")
    sid_big = make_session(big, settings, df, TRADEABLE, win, position_size=0.95)
    sid_small = make_session(
        small, settings, df, TRADEABLE, win, position_size=effective_position_size(settings)
    )
    st_big = run_session(settings, big, sid_big, mode="all", df=df)
    st_small = run_session(settings, small, sid_small, mode="all", df=df)

    def shape(mem, sid):
        return [
            (t["entry_time"], t["exit_time"], t["size"], t["pnl"], t["exit_reason"])
            for t in mem.paper_trades(sid)
        ]

    assert len(shape(big, sid_big)) > 0
    assert shape(big, sid_big) == shape(small, sid_small)
    assert st_big["blocked_orders"] == st_small["blocked_orders"] == 0


def test_status_progress_and_equity(settings, df, tmp_path):
    win = df.iloc[500:1500]
    m = Memory(tmp_path / "s.db")
    sid = make_session(m, settings, df, TRADEABLE, win)
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
    sid = make_session(m, settings, df, TRADEABLE, win)
    settings.paper.step_bars = 100
    settings.paper.max_ticks = 2
    st = run_session(settings, m, sid, mode="all", df=df)
    assert st["cursor"] == 200


def test_reset_clears_results(settings, df, tmp_path):
    win = df.iloc[500:1200]
    m = Memory(tmp_path / "z.db")
    sid = make_session(m, settings, df, TRADEABLE, win)
    settings.paper.step_bars = 200
    run_session(settings, m, sid, mode="all", df=df)
    assert len(m.paper_trades(sid)) >= 0
    m.reset_paper_session(sid)
    st = paper_status(settings, m, sid)
    assert st["cursor"] == 0
    assert st["n_trades"] == 0

def test_the_session_keeps_its_open_position_in_equity_minus_cash(settings, df, tmp_path):
    """``equity - cash`` is the open exposure the dashboard's risk panel reports.

    The session stores no positions table, so the panel subtracts the stored cash from
    the last equity point.  That is only right if the two describe the **same bar** and
    if the difference really is the position marked at that bar's close - so this pins
    both, with the position rebuilt from the fill ledger rather than from the broker's
    own book.
    """
    from selftrade.execution import KillSwitch, RiskGate, kill_switch_path, run_orders
    from selftrade.strategies import generate_signals

    win = df.iloc[500:1500]
    m = Memory(tmp_path / "inv.db")
    sid = make_session(m, settings, df, TRADEABLE, win)
    # Stop one bar after the first entry: the fill lands on the following open, so the
    # session is still holding the position on its last recorded bar.
    first = int(np.flatnonzero(generate_signals(TRADEABLE, win)["long_entry"].to_numpy())[0])
    settings.paper.step_bars = first + 2
    settings.session_file = str(tmp_path / "session.json")  # keep the switch in tmp
    st = run_session(settings, m, sid, mode="step", df=df)

    session = m.get_paper_session(sid)
    equity = m.paper_equity(sid)
    cfg = session["settings"]
    prefix = win.iloc[: st["cursor"]]
    res = run_orders(
        prefix,
        TRADEABLE,
        initial_cash=settings.backtest.initial_cash,
        fee_bps=cfg["fee_bps"],
        slippage_bps=cfg["slippage_bps"],
        position_size=cfg["position_size"],
        allow_short=cfg["allow_short"],
        timeframe=cfg["timeframe"],
        symbol=str(settings.data.symbol),
        gate=RiskGate.from_config(settings.risk_gate),
        kill_switch=KillSwitch(kill_switch_path(settings)),
    )

    # the stored pair is the run's own final bar, not an older one
    assert float(res.cash) == pytest.approx(session["cash"])
    assert float(res.equity.iloc[-1]) == pytest.approx(equity[-1]["equity"])
    # and what is left in the difference is the position at the last close
    net_qty = sum(f["qty"] * (1 if f["side"] == "buy" else -1) for f in res.fills)
    assert net_qty > 0                       # there is something to measure
    exposure = equity[-1]["equity"] - session["cash"]
    assert exposure == pytest.approx(net_qty * float(prefix["close"].iloc[-1]))

def test_a_paper_run_announces_the_halt_it_ran_into(settings, df, tmp_path, monkeypatch):
    """The rail that stops trading tells someone: once, with the numbers and the reason.

    `notify.enabled` is the only switch that matters here - the run is otherwise the
    ordinary one, and the message goes through the real `RiskWatch` (only the HTTP
    transport is replaced, so the test never touches the network).
    """
    from selftrade import notify as notify_mod
    from selftrade.execution import KillSwitch, kill_switch_path

    win = df.iloc[500:1500]
    settings.memory.db = str(tmp_path / "n.db")
    settings.session_file = str(tmp_path / "state" / "session.json")
    settings.paper.step_bars = 250
    settings.notify.enabled = True
    settings.notify.warn_below_frac = 0.0          # this test is about the halt
    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod, "send", lambda s, text, **kw: (sent.append(text), (True, "sent"))[1]
    )
    m = Memory(settings.memory.db)
    sid = make_session(m, settings, df, TRADEABLE, win)
    KillSwitch(kill_switch_path(settings)).trigger("armed for the test", by="operator")

    st = run_session(settings, m, sid, mode="step", df=df)

    assert [n["kind"] for n in st["notices"]] == ["halt"]
    assert st["notices"][0]["sent"] is True and st["notices"][0]["recorded"] is True
    assert len(sent) == 1
    assert "HALT" in sent[0] and "armed for the test" in sent[0]
    assert "max_drawdown" in sent[0] and "paper session #" in sent[0]

    # the next tick has nothing new to say: the halt was announced when it happened
    again = run_session(settings, m, sid, mode="step", df=df)
    assert not again.get("notices") and len(sent) == 1

    # with the channel off the same run says nothing, and nothing is remembered either
    settings.notify.enabled = False
    KillSwitch(kill_switch_path(settings)).reset("test done")
    notify_mod.RiskWatch(settings).clear()
    quiet = run_session(settings, m, sid, mode="step", df=df)
    assert not quiet.get("notices") and len(sent) == 1


def test_a_paper_run_says_when_the_gate_stopped_it_not_the_operator(
    settings, df, tmp_path, monkeypatch
):
    """The chain the rail actually runs: the gate arms it, the tick announces *that*.

    Same run and same real `RiskWatch` as the test above; what changes is who armed the
    switch.  The gate passes the margin it saw at the refusal, and the message has to
    carry it, because by the time anybody reads it the account has moved.
    """
    from selftrade import notify as notify_mod
    from selftrade.execution import KillSwitch, arming_detail, kill_switch_path

    class Snap:
        equity = 8_456.0
        drawdown = 0.2387
        day_pnl_pct = -0.004
        ts = "2026-10-10T08:00:00+00:00"

    checks = [
        {"name": "max_drawdown", "value": 23.87, "limit": 15.0, "headroom": -8.87,
         "headroom_frac": -0.59, "unit": "pp", "ok": False, "arms": True},
        {"name": "max_daily_loss", "value": 0.4, "limit": 2.0, "headroom": 1.6,
         "headroom_frac": 0.8, "unit": "pp", "ok": True, "arms": True},
    ]

    win = df.iloc[500:1500]
    settings.memory.db = str(tmp_path / "g.db")
    settings.session_file = str(tmp_path / "state" / "session.json")
    settings.paper.step_bars = 250
    settings.notify.enabled = True
    settings.notify.warn_below_frac = 0.0
    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod, "send", lambda s, text, **kw: (sent.append(text), (True, "sent"))[1]
    )
    m = Memory(settings.memory.db)
    sid = make_session(m, settings, df, TRADEABLE, win)
    KillSwitch(kill_switch_path(settings)).trigger(
        "blocked: account drawdown 23.87% breaks the sealed 15.0% limit: trading halts",
        by="risk_gate",
        detail=arming_detail(Snap(), checks, alerts=["drawdown limit breached: new orders halted"]),
    )

    st = run_session(settings, m, sid, mode="step", df=df)

    assert [n["kind"] for n in st["notices"]] == ["halt"]
    assert len(sent) == 1
    text = sent[0]
    assert text.startswith("selftrade HALT (automatic) - the gate stopped trading, not you")
    assert "armed by risk_gate" in text
    assert "  drawdown 23.87% of the 15.00% cap - 8.87pp past it" in text
    assert "  today's loss 0.40% of the 2.00% cap - +1.60pp of margin left" in text
    assert "where the account stands now:" in text and "paper session #" in text
