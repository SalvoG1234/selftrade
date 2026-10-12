import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

import pytest

from selftrade import notify
from selftrade.config import load_settings
from selftrade.memory import Memory


class FakeResponse:
    def __init__(self, code=200):
        self.status_code = code


class FakeSession:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse(self.code)


def test_disabled(tmp_path):
    s = load_settings("config.yaml")
    ok, msg = notify.send(s, "hello")
    assert ok is False
    assert "disabled" in msg


def test_missing_env(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.delenv(s.notify.telegram_token_env, raising=False)
    monkeypatch.delenv(s.notify.telegram_chat_id_env, raising=False)
    ok, msg = notify.send(s, "hello")
    assert ok is False
    assert "not set" in msg


def test_send_success(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "tok123")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    sess = FakeSession(200)
    ok, msg = notify.send(s, "body", session=sess)
    assert ok is True
    assert len(sess.calls) == 1
    assert sess.calls[0]["json"]["chat_id"] == "chat9"


def test_send_http_error(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "tok123")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    ok, msg = notify.send(s, "body", session=FakeSession(500))
    assert ok is False
    assert "500" in msg


def test_token_never_printed(monkeypatch):
    s = load_settings("config.yaml")
    s.notify.enabled = True
    monkeypatch.setenv(s.notify.telegram_token_env, "SUPERSECRETTOKEN")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok, msg = notify.send(s, "body", session=FakeSession(200))
    assert "SUPERSECRETTOKEN" not in buf.getvalue()
    assert "SUPERSECRETTOKEN" not in msg


def test_paper_summary_text():
    text = notify.paper_summary({"name": "x", "status": "done", "cursor": 10, "total_bars": 10, "n_trades": 3, "total_return": 0.05})
    assert "selftrade paper" in text and "5.00%" in text

# --------------------------------------------------------------------------
# the risk alerts: when to send, and never twice for the same thing
# --------------------------------------------------------------------------

class FailingSession(FakeSession):
    def __init__(self, code=500):
        super().__init__(code)


def risk_settings(tmp_path, *, enabled=True, warn=0.25):
    """Settings with their own database and state directory - nothing shared."""
    s = load_settings("config.yaml")
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.session_file = str(tmp_path / "state" / "session.json")
    s.notify.enabled = enabled
    s.notify.warn_below_frac = warn
    return s


def curve_points(curve, day="2024-01-01"):
    """One point per UTC day, so a slow slide does not read as a single-day crash.

    The daily-loss limit is measured from the first point *of the last date*: spreading the
    curve over days is what makes "deep drawdown, quiet day" expressible at all.
    """
    start = date.fromisoformat(day)
    return [
        (f"{(start + timedelta(days=i)).isoformat()} 00:00:00+00:00", float(eq))
        for i, eq in enumerate(curve)
    ]


def seed_session(settings, curve, cash, *, day="2024-01-01"):
    """A paper session whose recorded equity curve is ``curve``, one point per day."""
    mem = Memory(settings.memory.db)
    points = curve_points(curve, day)
    sid = mem.create_paper_session(
        name="alert test",
        genome={"family": "rsi_reversion", "params": {}, "risk": {}},
        window="recent",
        total_bars=len(curve),
        start_time=points[0][0],
        end_time=points[-1][0],
        initial_cash=10_000.0,
        settings={},
    )
    mem.add_paper_equity(sid, points)
    mem.update_paper_cursor(sid, len(curve), float(cash), "active")
    return sid


def arm(settings, reason="stopped by the test", *, by="risk_gate", detail=None):
    from selftrade.execution import KillSwitch, kill_switch_path

    return KillSwitch(kill_switch_path(settings)).trigger(reason, by=by, detail=detail)


def reset_switch(settings):
    from selftrade.execution import KillSwitch, kill_switch_path

    return KillSwitch(kill_switch_path(settings)).reset("test done")


def test_a_halt_is_announced_once_with_the_numbers_that_explain_it(tmp_path, monkeypatch):
    s = risk_settings(tmp_path, warn=0.0)
    monkeypatch.setenv(s.notify.telegram_token_env, "SUPERSECRETTOKEN")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat9")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)  # 12% drawdown, 20% of cap left
    arm(s, "drawdown limit breached: new orders halted")
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    outcomes = watcher.notify()

    assert [o["kind"] for o in outcomes] == [notify.HALT]
    assert outcomes[0]["sent"] is True and outcomes[0]["recorded"] is True
    text = sess.calls[0]["json"]["text"]
    assert text.startswith("selftrade HALT")
    assert "drawdown limit breached" in text            # the switch's own reason
    assert "max_drawdown" in text and "15.00" in text   # the limit and its cap
    assert "+3.00pp" in text                            # ...and the room that was left
    assert "no position was closed" in text             # what arming does not do
    assert "SUPERSECRETTOKEN" not in text and "chat9" not in text
    assert notify.state_path(s).exists()

    # the same halt is not news the second time: this is what makes it usable per tick
    assert watcher.notify() == []
    assert len(sess.calls) == 1

    # a new halt (disarm, then arm again) is news again
    reset_switch(s)
    arm(s, "daily loss limit breached: new orders halted")
    again = watcher.notify()
    assert [o["kind"] for o in again] == [notify.HALT]
    assert len(sess.calls) == 2


def test_a_warning_fires_once_per_approach_and_re_arms_after_recovery(tmp_path, monkeypatch):
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    sid = seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)  # 12% dd = 20% of cap left
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    first = watcher.notify()
    assert [o["kind"] for o in first] == [notify.WARNING]
    assert "WARNING" in sess.calls[0]["json"]["text"]
    assert "close to halting" in sess.calls[0]["json"]["text"]
    assert watcher.notify() == []           # still close: said once

    # the account recovers: the ledger forgets the approach, so the next one is news
    memory = Memory(s.memory.db)
    memory.replace_paper_results(sid, [], curve_points([10_000.0]), 1, 10_000.0)
    assert watcher.notify() == []
    memory.replace_paper_results(
        sid, [], curve_points([10_000.0, 10_000.0, 8_800.0]), 3, 8_800.0
    )                                                       # and close again
    again = watcher.notify()
    assert [o["kind"] for o in again] == [notify.WARNING]


def test_warnings_can_be_turned_off_without_turning_halts_off(tmp_path):
    """``warn_below_frac=0`` silences the *close* band; a halt is never silenced."""
    s = risk_settings(tmp_path, warn=0.0)
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)  # 12% of the 15% cap: close
    assert notify.RiskWatch(s).pending() == []                    # ...and close is off
    assert notify.RiskWatch(s, session=FakeSession(200)).notify() == []

    arm(s)                                                        # a halt still goes out
    said = notify.RiskWatch(s, session=FakeSession(200)).notify()
    assert [o["kind"] for o in said] == [notify.HALT]


def test_a_limit_past_its_cap_is_announced_even_with_nothing_armed(tmp_path, monkeypatch):
    """The breach the halt cannot report: past a cap, and no order has been checked.

    The kill switch is armed *by the gate*, when it next sees an order - which may be
    never: a finished paper window sits past its cap indefinitely, and this is the only
    moment anybody can hear about it.
    """
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    sid = seed_session(s, [10_000.0, 9_500.0, 8_400.0], cash=8_400.0)  # 16% > the 15% cap
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    first = watcher.pending()
    assert [p["kind"] for p in first] == [notify.WARNING]
    assert first[0]["band"] == "past" and first[0]["reason"] == "max_drawdown"
    assert watcher.pending() == first          # pure: asking twice does not consume it
    assert watcher.notify()[0]["sent"] is True
    text = sess.calls[0]["json"]["text"]
    assert "past its cap" in text and "the next order arms the stop" in text
    assert "nothing has halted yet" in text
    assert watcher.notify() == []              # and it is not repeated every tick

    # the account recovers to the close band: a *different* band is news again
    Memory(s.memory.db).replace_paper_results(
        sid, [], curve_points([10_000.0, 9_500.0, 8_800.0]), 3, 8_800.0
    )
    recovered = watcher.notify()
    assert [o["kind"] for o in recovered] == [notify.WARNING]
    assert "close to halting" in sess.calls[1]["json"]["text"]
    assert "past its cap" not in sess.calls[1]["json"]["text"]


GATE_DETAIL = {
    "drawdown": 0.2387,
    "day_pnl_pct": -0.004,
    "equity": 8_456.0,
    "as_of": "2026-10-10T08:00:00+00:00",
    "alerts": ["drawdown limit breached: new orders halted"],
    "checks": [
        {"name": "max_drawdown", "value": 23.87, "limit": 15.0, "headroom": -8.87,
         "headroom_frac": -0.59, "unit": "pp", "ok": False},
        {"name": "max_daily_loss", "value": 0.4, "limit": 2.0, "headroom": 1.6,
         "headroom_frac": 0.8, "unit": "pp", "ok": True},
    ],
}


def test_a_halt_armed_by_the_program_says_so_and_with_how_much_room(tmp_path, monkeypatch):
    """The three numbers a reader of a HALT asks for, taken from the refusal itself.

    A human who armed the stop knows what they did; somebody who touched nothing needs to
    know the program did it *and* how close it was, or the message only says "it stopped".
    """
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)
    arm(s, "drawdown limit breached: new orders halted", detail=GATE_DETAIL)
    sess = FakeSession(200)

    assert notify.RiskWatch(s, session=sess).notify()[0]["sent"] is True
    text = sess.calls[0]["json"]["text"]

    assert text.startswith("selftrade HALT (automatic) - the gate stopped trading, not you")
    assert "stop armed by risk_gate: drawdown limit breached: new orders halted" in text
    assert "the numbers the gate saw when it refused the order at 2026-10-10T08:00:00+00:00, equity 8,456.00:" in text
    assert "  drawdown 23.87% of the 15.00% cap - 8.87pp past it" in text
    assert "  today's loss 0.40% of the 2.00% cap - +1.60pp of margin left" in text
    assert "where the account stands now:" in text
    assert "no position was closed" in text


def test_a_halt_you_armed_yourself_does_not_claim_the_gate_did_it(tmp_path, monkeypatch):
    """The channel must not tell an operator that the program stopped trading when they did."""
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)
    arm(s, "stopping new orders before the weekend", by="operator")
    sess = FakeSession(200)

    notify.RiskWatch(s, session=sess).notify()
    text = sess.calls[0]["json"]["text"]

    assert text.startswith("selftrade HALT - every new order is refused")
    assert "(automatic)" not in text and "not you" not in text
    assert "the numbers the gate saw" not in text
    assert "stop armed by operator: stopping new orders before the weekend" in text
    assert "max_drawdown" in text          # the live measurement is still reported


def test_an_automatic_halt_without_recorded_numbers_still_names_the_gate(tmp_path, monkeypatch):
    """A switch armed before the numbers were kept (or by an older file) is still the gate."""
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)
    arm(s, "drawdown limit breached: new orders halted")   # by="risk_gate", no detail
    sess = FakeSession(200)

    notify.RiskWatch(s, session=sess).notify()
    text = sess.calls[0]["json"]["text"]

    assert text.startswith("selftrade HALT (automatic)")
    assert "the numbers the gate saw" not in text
    assert "max_drawdown 12.00% of the 15.00% cap" in text   # the live numbers, unlabelled


def test_the_arming_lines_fall_back_to_the_account_when_checks_are_missing():
    """A detail with the account's own scalars still says how much room was left."""
    lines = notify.arming_lines({"drawdown": 0.2387, "day_pnl_pct": -0.004})
    assert lines[0] == ""
    assert lines[1] == "the numbers the gate saw when it refused the order:"
    assert lines[2] == "  account drawdown 23.87%"
    assert lines[3] == "  the day's move -0.40%"
    # nothing usable at all: no empty paragraph is added to the message
    assert notify.arming_lines({}) == []
    assert notify.arming_lines({"checks": [{}]}) == []


def test_a_halt_supersedes_the_band_warnings(tmp_path, monkeypatch):
    """One message per state: the halt already carries every limit."""
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 9_500.0, 8_400.0], cash=8_400.0)  # past the cap
    arm(s, "drawdown limit breached: new orders halted")

    sess = FakeSession(200)
    outcomes = notify.RiskWatch(s, session=sess).notify()

    assert [o["kind"] for o in outcomes] == [notify.HALT]   # not a halt *and* a warning
    assert "max_drawdown" in sess.calls[0]["json"]["text"]  # the numbers are in it


def test_a_past_limit_is_announced_again_after_the_stop_is_disarmed(tmp_path, monkeypatch):
    """Disarming does not fix the account: the next order re-arms the stop."""
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 9_500.0, 8_400.0], cash=8_400.0)
    arm(s, "drawdown limit breached: new orders halted")
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    assert [o["kind"] for o in watcher.notify()] == [notify.HALT]
    assert watcher.notify() == []                      # the halt said it once

    reset_switch(s)                                    # nothing was fixed by that
    again = watcher.notify()

    assert [o["kind"] for o in again] == [notify.WARNING]
    assert "past its cap" in sess.calls[-1]["json"]["text"]


def test_a_disabled_channel_sends_nothing_and_remembers_nothing(tmp_path):
    s = risk_settings(tmp_path, enabled=False, warn=0.0)
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)
    arm(s)
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    pending = watcher.pending()
    assert [p["kind"] for p in pending] == [notify.HALT]      # there *is* something to say
    outcomes = watcher.notify()
    assert outcomes[0]["sent"] is False and "disabled" in outcomes[0]["message"]
    assert sess.calls == []                                    # and no request was made
    assert not notify.state_path(s).exists()                   # nothing was marked as told
    assert notify.readiness(s)["ready"] is False


def test_a_failed_send_stays_pending_and_is_retried(tmp_path, monkeypatch):
    s = risk_settings(tmp_path, warn=0.0)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)
    arm(s)

    bad = FailingSession(500)
    first = notify.RiskWatch(s, session=bad).notify()
    assert first[0]["sent"] is False and "500" in first[0]["message"]
    assert not notify.state_path(s).exists()          # an undelivered halt is not "told"

    good = FakeSession(200)
    second = notify.RiskWatch(s, session=good).notify()
    assert second[0]["sent"] is True                  # retried, and it went out
    assert len(bad.calls) == 1 and len(good.calls) == 1
    assert notify.RiskWatch(s, session=good).notify() == []
    assert notify.readiness(s)["ready"] is True


def test_a_halt_is_still_announced_when_the_account_cannot_be_read(tmp_path, monkeypatch):
    """The stop matters even when the numbers behind it cannot be reconstructed."""
    s = risk_settings(tmp_path)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    arm(s, "operator stopped the bot")
    sess = FakeSession(200)

    outcomes = notify.RiskWatch(s, session=sess).notify()

    assert outcomes[0]["sent"] is True
    text = sess.calls[0]["json"]["text"]
    assert "HALT" in text and "operator stopped the bot" in text
    assert "could not be measured" in text


def test_an_older_or_corrupt_ledger_is_read_not_raised(tmp_path, monkeypatch):
    """The ledger is a file another version may have written: never take the channel down."""
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)  # 12%: the close band
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    # the older shape: a flat list of names already announced in the close band
    notify.state_path(s).parent.mkdir(parents=True, exist_ok=True)
    notify.state_path(s).write_text(json.dumps({"warning": {"announced": ["max_drawdown"]}}),
                                    encoding="utf-8")
    assert watcher.notify() == []                  # still the same band: quiet, no crash
    assert notify.announced_of({"warning": {"announced": ["max_drawdown"]}}) == {
        "max_drawdown": "close"
    }

    # a ledger that cannot be parsed at all is treated as empty: tell again, do not die
    notify.state_path(s).write_text("{not json at all", encoding="utf-8")
    again = watcher.notify()
    assert [o["kind"] for o in again] == [notify.WARNING]
    assert json.loads(notify.state_path(s).read_text(encoding="utf-8"))["warning"]["announced"] == {
        "max_drawdown": "close"
    }


def test_the_ledger_keeps_what_went_out_not_what_was_decided(tmp_path, monkeypatch):
    """The history is the record of *messages*: a failure is not one, and a reset keeps it."""
    s = risk_settings(tmp_path, warn=0.25)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    assert notify.sent_log(s) == []
    seed_session(s, [10_000.0, 9_500.0, 8_400.0], cash=8_400.0)   # past the drawdown cap
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    assert watcher.notify()[0]["sent"] is True                    # the `past`-band warning
    arm(s, "drawdown limit breached: new orders halted", detail=GATE_DETAIL)
    assert watcher.notify()[0]["kind"] == notify.HALT

    log = notify.sent_log(s)
    assert [e["kind"] for e in log] == [notify.HALT, notify.WARNING]   # newest first
    assert log[0]["automatic"] is True and log[0]["band"] is None
    assert log[0]["reason"].startswith("drawdown limit breached")
    assert log[0]["headline"].startswith("selftrade HALT (automatic)")
    assert log[0]["at"] and log[1]["at"]
    assert log[1]["band"] == "past" and log[1]["reason"] == "max_drawdown"
    assert log[1]["headline"].startswith("selftrade WARNING - max_drawdown is past its cap")
    assert notify.sent_log(s, limit=1) == log[:1]

    # a reset re-arms the dedupe and does **not** rewrite what happened
    watcher.clear()
    assert len(notify.sent_log(s)) == 2

    # a message that never went out is not a message
    reset_switch(s)
    arm(s, "daily loss limit breached: new orders halted")
    failed = notify.RiskWatch(s, session=FailingSession(500)).notify()
    assert failed[0]["sent"] is False
    assert [e["kind"] for e in notify.sent_log(s)] == [notify.HALT, notify.WARNING]

    # and the retry that succeeds is recorded exactly once
    assert notify.RiskWatch(s, session=FakeSession(200)).notify()[0]["sent"] is True
    assert [e["kind"] for e in notify.sent_log(s)] == ["halt", "halt", "warning"]
    assert notify.sent_log(s)[0]["reason"].startswith("daily loss")


def test_the_history_is_bounded(tmp_path, monkeypatch):
    """A file read on every tick and every refresh cannot grow without end."""
    s = risk_settings(tmp_path, warn=0.0)
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 8_800.0], cash=8_800.0)
    monkeypatch.setattr(notify, "SENT_LOG_LIMIT", 3)
    watcher = notify.RiskWatch(s, session=FakeSession(200))

    for i in range(4):
        reset_switch(s)
        arm(s, f"halt number {i}")
        assert watcher.notify()[0]["sent"] is True

    log = notify.sent_log(s)
    assert len(log) == 3
    assert [e["reason"] for e in log] == ["halt number 3", "halt number 2", "halt number 1"]


def test_a_quiet_round_sends_nothing_and_writes_nothing(tmp_path, monkeypatch):
    """`paper` calls this every tick: when there is nothing to say it must cost no write.

    The account *is* measured on every call - a breach at the cap is exactly the state
    nobody else is looking at, and :meth:`RiskWatch.current` documents the ~1 ms it
    costs - but a round with nothing to say must not touch the ledger or the network.
    """
    s = risk_settings(tmp_path, warn=0.0)          # warning bands off, switch clear
    monkeypatch.setenv(s.notify.telegram_token_env, "tok")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "chat")
    seed_session(s, [10_000.0, 10_000.0, 10_000.0], cash=10_000.0)
    sess = FakeSession(200)
    watcher = notify.RiskWatch(s, session=sess)

    assert watcher.watch_once()["outcomes"] == []
    assert watcher.pending() == []                 # nothing to say, either
    assert sess.calls == [] and not notify.state_path(s).exists()


def test_readiness_reports_names_and_never_values(tmp_path, monkeypatch):
    s = risk_settings(tmp_path)
    monkeypatch.setenv(s.notify.telegram_token_env, "SUPERSECRETTOKEN")
    monkeypatch.setenv(s.notify.telegram_chat_id_env, "CHATSECRET")
    info = notify.readiness(s)
    blob = json.dumps(info)
    assert info["ready"] is True and info["channel"] == "telegram"
    assert "SUPERSECRETTOKEN" not in blob and "CHATSECRET" not in blob
    assert info["token_env"] == s.notify.telegram_token_env
    assert info["warn_below_frac"] == 0.25


def test_the_halt_message_says_which_limit_and_how_much_room_was_left(tmp_path):
    report = {
        "ok": True,
        "breached": ["max_drawdown"],
        "would_arm": True,
        "tight_frac": 0.25,
        "nearest": {"name": "max_drawdown", "headroom": -8.0, "headroom_frac": -0.5, "unit": "pp"},
        "checks": [
            {"name": "max_drawdown", "value": 23.87, "limit": 15.0, "ok": False, "unit": "pp",
             "headroom": -8.87, "headroom_frac": -0.59, "source": "sealed",
             "effect": "arms the kill switch", "arms": True},
            {"name": "max_daily_loss", "value": 0.0, "limit": 2.0, "ok": True, "unit": "pp",
             "headroom": 2.0, "headroom_frac": 1.0, "source": "sealed",
             "effect": "arms the kill switch", "arms": True},
        ],
        "session": {"session_id": 3, "points": 10, "as_of": "2024-01-01 02:00:00+00:00"},
    }
    text = notify.halt_message({"blocked": True, "reason": "risk_gate: too deep",
                                "triggered_by": "risk_gate", "triggered_at": "T0"}, report)
    assert "BREACHED: max_drawdown" in text and "next order" in text
    assert "max_drawdown 23.87% of the 15.00% cap (room -8.87pp)" in text
    assert "session #3" in text
