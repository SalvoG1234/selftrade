"""Optional notifications (Telegram) via environment variables only - and *when* to send.

The token and chat id are read from the environment variables named in config
(``notify.telegram_token_env`` / ``notify.telegram_chat_id_env``) and are never
printed, stored in memory, or written to reports: :func:`readiness` reports the *names*
of what is missing, never a value.

A notification channel nobody sends to is decoration, so this module is also the policy
that decides when the operator hears about something.  Two events are worth a message,
and both are about the **risk layer**, because that is the part of this program that
can stop on its own:

* **a halt** - the kill switch is engaged.  It is armed by the operator, or by the gate
  itself when a sealed limit breaks, and either way new orders are refused from that
  moment: a stop nobody hears about is the same as no stop at all;
* **a warning** - one of the two limits that can halt trading is *close*
  (``notify.warn_below_frac`` of its cap, 25% by default), **or already past it** while
  nothing has stopped yet.  That second case is the one that hides in plain sight: a
  finished paper window, a weekend or a stopped loop can sit past a cap for hours, and
  the halt only happens when the gate next checks an order - which may be never.  The
  halt itself supersedes both: its message already carries every limit.

Three rules make it usable rather than noisy:

1. **the same halt is announced once.**  The signature is the switch's own
   ``triggered_at`` + reason, so re-reading the state - every ``paper`` tick, every poll
   of :func:`watch` - sends nothing; a *new* halt (after a reset and a re-arm) is news
   again;
2. **a warning is announced once per band, per approach**, and forgotten when the limit
   recovers, so the next approach warns again - and a limit that moves from *close* to
   *past* (or back) is news again, because that is a different thing to say;
3. **the state is recorded only after the message actually went out.**  A network
   failure leaves the notice pending: the next call retries it and keeps reporting that
   it is undelivered, rather than silently marking it as told.  The same file keeps a
   bounded **history** of what went out (the newest :data:`SENT_LOG_LIMIT` notices, with
   when, why and which band), which is what the dashboard's *Alerts* card and
   ``notify status`` read - so a message that never left can never appear in it.

Nothing here raises: every failure is returned as text.  And nothing here sends
anything unless ``notify.enabled`` is true *and* both environment variables are set, so
this stays opt-in and costs no request by default.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

from .risk_report import CLOSE, PAST, describe, halting_bands, limits_report

TELEGRAM_API = "https://api.telegram.org"

#: Where the "already announced" ledger lives: next to the session file (``state/``).
STATE_FILENAME = "notify_state.json"

#: How many delivered notices the ledger keeps, newest kept:
#: a bounded history, because this file is read on every `paper` tick and on every
#: dashboard refresh, and because "what was I told, and when" is asked about days, not
#: years.  The dedupe state stays exact; only the *history* is trimmed.
SENT_LOG_LIMIT = 50

#: Kinds of notice this module sends.  They are separate on purpose: a halt and a
#: warning have different signatures, different lifetimes and different audiences.
HALT = "halt"
WARNING = "warning"

#: Who can arm the kill switch *by itself*: ``KillSwitch.trigger``'s default is what the
#: paper rail passes when a sealed limit breaks.  An operator arm is ``operator`` (CLI) or
#: ``dashboard``, and those are not this.
AUTO_ARMERS = frozenset({"risk_gate", "broker", "binance", "paper", "loop"})

#: How the gate's own check names read in a message: the reader thinks "drawdown", the
#: gate says ``max_drawdown``.  Anything unlisted is printed as it is.
CHECK_LABELS = {
    "max_drawdown": "drawdown",
    "max_daily_loss": "today's loss",
    "position_pct": "position size",
    "leverage": "leverage",
}


def configured(settings: Any) -> tuple[bool, str]:
    """Return (ready, message) without revealing any secret value."""
    cfg = settings.notify
    if not cfg.enabled:
        return False, "notify.disabled"
    token = os.environ.get(cfg.telegram_token_env)
    chat = os.environ.get(cfg.telegram_chat_id_env)
    if not token:
        return False, f"env {cfg.telegram_token_env} is not set"
    if not chat:
        return False, f"env {cfg.telegram_chat_id_env} is not set"
    return True, "ready"


def readiness(settings: Any) -> dict[str, Any]:
    """What the dashboard and the CLI show: would anybody be told, and why not.

    Only environment variable *names* travel; the values are never read into a report.
    """
    ready, reason = configured(settings)
    cfg = settings.notify
    return {
        "ready": bool(ready),
        "reason": reason,
        "channel": "telegram" if ready else None,
        "token_env": cfg.telegram_token_env,
        "chat_env": cfg.telegram_chat_id_env,
        "warn_below_frac": float(cfg.warn_below_frac),
        "state_file": str(state_path(settings)),
        "hint": (
            "set notify.enabled=true and export both variables"
            if not ready
            else "the halt and the warning below are the only things this channel sends"
        ),
    }


def send(settings: Any, text: str, *, timeout: float = 10.0, session: Any = None) -> tuple[bool, str]:
    """Send a message.  Returns (sent, message); never raises for HTTP issues."""
    ready, msg = configured(settings)
    if not ready:
        return False, msg
    cfg = settings.notify
    token = os.environ[cfg.telegram_token_env]
    chat = os.environ[cfg.telegram_chat_id_env]
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        poster = session.post if session is not None else requests.post
        r = poster(url, json={"chat_id": chat, "text": text[:4000]}, timeout=timeout)
        if getattr(r, "status_code", 0) == 200:
            return True, "sent"
        return False, f"telegram returned status {getattr(r, 'status_code', '?')}"
    except requests.RequestException as exc:
        return False, f"telegram request failed: {type(exc).__name__}"


def outstanding(settings: Any, report: dict[str, Any]) -> list[dict[str, Any]]:
    """The notices still owed, in the shape a *reader* wants: kind, band, one short line.

    :meth:`RiskWatch.pending` returns whole messages, which is what a message needs; a
    dashboard row or a terminal line wants the list.  Pure, like the method it wraps:
    nothing is sent and nothing is written, so a page can ask this on every refresh.
    """
    from .execution import KillSwitch, kill_switch_path

    switch = KillSwitch(kill_switch_path(settings)).describe()
    notices = RiskWatch(settings).pending({"switch": switch, "report": report})
    return [
        {
            "kind": notice["kind"],
            "reason": notice["reason"],
            "band": notice.get("band"),
            "headline": (notice["text"].splitlines() or [""])[0],
        }
        for notice in notices
    ]


def paper_summary(stats: dict[str, Any]) -> str:
    return (
        f"selftrade paper {stats.get('name')}: status={stats.get('status')} "
        f"cursor={stats.get('cursor')}/{stats.get('total_bars')} "
        f"trades={stats.get('n_trades')} return={stats.get('total_return', 0)*100:.2f}%"
    )


# --------------------------------------------------------------------------
# the text of a notice
# --------------------------------------------------------------------------

def armed_by_the_program(switch: dict[str, Any]) -> bool:
    """True when the **program** armed the switch, not a human.

    ``KillSwitch.trigger`` defaults to ``risk_gate`` and that is the value the paper rail
    passes when a sealed limit breaks; the dashboard arms as ``dashboard`` and the CLI as
    ``operator``.  A reader who stopped the bot themselves does not need to be told that
    their own action happened - a reader who did *nothing* does, and urgently.
    """
    return str(switch.get("triggered_by") or "") in AUTO_ARMERS


def _num(value: Any) -> float | None:
    """A float, or ``None``: a message builder never raises on a hand-written state file."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def arming_lines(detail: dict[str, Any]) -> list[str]:
    """The numbers the gate saw *at the refusal*, which is the only moment they exist.

    The margin is a property of the instant the order was refused: minutes later the
    account has moved, and "with how much room?" can no longer be answered.  That is why
    the switch keeps this and why the message leads with it, before the live measurement.
    """
    checks = detail.get("checks") or []
    lines: list[str] = []
    for check in checks:
        value, limit, room = (
            _num(check.get("value")),
            _num(check.get("limit")),
            _num(check.get("headroom")),
        )
        if value is None or limit is None or room is None:
            continue
        unit = "x" if check.get("unit") == "x" else "%"
        room_unit = "x" if unit == "x" else "pp"
        label = CHECK_LABELS.get(str(check.get("name")), str(check.get("name")))
        margin = (
            f"{room:+.2f}{room_unit} of margin left"
            if check.get("ok")
            else f"{-room:.2f}{room_unit} past it"
        )
        lines.append(f"  {label} {value:.2f}{unit} of the {limit:.2f}{unit} cap - {margin}")
    if lines:
        equity = _num(detail.get("equity"))
        when = f" at {detail['as_of']}" if detail.get("as_of") else ""
        worth = f", equity {equity:,.2f}" if equity is not None else ""
        lines.insert(0, f"the numbers the gate saw when it refused the order{when}{worth}:")
        return ["", *lines]
    # No check details (an older switch file, or a rail that does not record them): fall
    # back to the account's own two numbers rather than saying nothing.
    drawdown, day = _num(detail.get("drawdown")), _num(detail.get("day_pnl_pct"))
    if drawdown is None and day is None:
        return []
    lines = ["", "the numbers the gate saw when it refused the order:"]
    if drawdown is not None:
        lines.append(f"  account drawdown {drawdown * 100:.2f}%")
    if day is not None:
        lines.append(f"  the day's move {day * 100:+.2f}%")
    return lines


def halt_message(switch: dict[str, Any], report: dict[str, Any], *, threshold: float = 0.0) -> str:
    """The message for an engaged kill switch, with the numbers that explain it.

    Three questions, in the order a reader asks them: *did I do this?* (the headline, see
    :func:`armed_by_the_program`), *with how much room?* (the numbers the gate saw at the
    refusal, when the program armed it) and *where is the account now?* (the live
    measurement).  A halt is the most consequential message this program can send, and the
    point of it is that the reader needs no follow-up question to act.
    """
    lines = [
        "selftrade HALT (automatic) - the gate stopped trading, not you"
        if armed_by_the_program(switch)
        else "selftrade HALT - every new order is refused"
    ]
    if switch.get("reason"):
        lines.append(f"stop armed by {switch.get('triggered_by') or '?'}: {switch['reason']}")
    if switch.get("triggered_at"):
        lines.append(f"at {switch['triggered_at']}")
    lines.extend(arming_lines(switch.get("detail") or {}))
    if report.get("ok"):
        lines.append("")
        lines.append("where the account stands now:")
        lines.extend(describe(report))
    else:
        lines.append(f"the account could not be measured: {report.get('error')}")
    lines.append("")
    lines.append(
        "arming only refuses orders - no position was closed. "
        "`python -m selftrade switch show` reads the same file; `switch reset` disarms."
    )
    return "\n".join(lines)


def warning_message(
    check: dict[str, Any], report: dict[str, Any], *, threshold: float, band: str = CLOSE
) -> str:
    """The message for a halting limit that is close to its cap - or already past it.

    Both bands mean the same thing to the reader: **nothing has halted yet**, and this is
    the limit that will do it.  ``close`` says the next day like today would be enough;
    ``past`` says the next order checked by the gate is, because the account is already
    beyond a cap the gate refuses on - which is why it is sent even when the warning
    threshold is off.
    """
    unit = "x" if check["unit"] == "x" else "%"
    room_unit = "pp" if unit == "%" else "x"
    against = (
        f"{check['name']} is {check['value']:.2f}{unit} against the "
        f"{check['limit']:.2f}{unit} cap, {check['headroom']:+.2f}{room_unit}"
    )
    if band == PAST:
        lines = [
            f"selftrade WARNING - {check['name']} is past its cap: the next order arms the stop",
            against,
            "nothing has halted yet: no order has been checked since the breach. The "
            "gate refuses the next one and arms the kill switch when it sees it",
        ]
    else:
        lines = [
            f"selftrade WARNING - {check['name']} is close to halting trading",
            f"{against} of room left "
            f"({check['headroom_frac'] * 100:.0f}% of the cap, warning band <= "
            f"{threshold * 100:.0f}%)",
            "the gate still allows orders; a breach arms the kill switch",
        ]
    if report.get("ok") and report.get("session"):
        lines.append("")
        lines.extend(describe(report))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# state: what has already been announced
# --------------------------------------------------------------------------

def state_path(settings: Any) -> Path:
    """Where the announced-notices ledger lives: next to the session file."""
    if getattr(settings, "session_file", None):
        return Path(settings.session_file).parent / STATE_FILENAME
    return Path("state") / STATE_FILENAME


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sent_entry(notice: dict[str, Any]) -> dict[str, Any]:
    """One line of the history: when, what, which band, why, and in one sentence."""
    return {
        "at": notice.get("at"),
        "kind": notice.get("kind"),
        "band": notice.get("band"),
        "reason": notice.get("reason"),
        # True/False for a halt (the program armed it, or a human did); None elsewhere.
        "automatic": notice.get("automatic"),
        "headline": (notice["text"].splitlines() or [""])[0],
    }


def sent_log(settings: Any, limit: int = SENT_LOG_LIMIT) -> list[dict[str, Any]]:
    """What the channel has already sent, **newest first**, as far as the ledger remembers.

    Read straight off the same file the dedupe state lives in, so the two can never
    disagree about whether a message went out; a failed send is not here, because only
    delivered notices are recorded at all.  Tolerant like the rest of this module: a
    missing file, an older layout or a hand-edited one is simply an empty history.
    """
    entries = _read_state(state_path(settings)).get("sent")
    if not isinstance(entries, list):
        return []
    return list(reversed([e for e in entries if isinstance(e, dict)]))[: max(0, int(limit))]


def announced_of(state: dict[str, Any]) -> dict[str, str]:
    """The bands already announced, ``{limit: close|past}``, read leniently.

    Tolerant on purpose: the ledger is a file on disk that another version of this program
    may have written, and a reader that raises on it would take the whole channel down.
    The previous format kept a flat *list* of names announced in the ``close`` band, so a
    list is read as exactly that - one message may be repeated once after an upgrade, and
    nothing else changes.
    """
    raw = (state.get(WARNING) or {}).get("announced")
    if isinstance(raw, dict):
        return {str(name): str(band) for name, band in raw.items()}
    if isinstance(raw, list):
        return {str(name): CLOSE for name in raw}
    return {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    """Write the ledger atomically: a half-written file would forget what was told."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class RiskWatch:
    """Decides what is new, sends it, and remembers what went out.

    ``session`` is the HTTP transport (a ``requests``-like object); tests inject one, and
    so can a caller that wants its own connection pool.  ``path`` overrides the ledger
    file.  One instance per process is enough; a second *process* watching the same state
    is not coordinated (the ledger is read-modify-write) - run one watcher.
    """

    def __init__(self, settings: Any, *, session: Any = None, path: Path | None = None):
        self.settings = settings
        self.session = session
        self.path = path if path is not None else state_path(settings)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- reading
    def current(self) -> dict[str, Any]:
        """The switch and the limits, right now: one file read and one database read.

        The database read happens even when the switch is clear, because a limit that is
        *past* its cap without having halted is precisely the state worth announcing, and
        it lives in the account, not in the switch.  Measured cost on a 3 600-point
        session: ~1 ms, against the ~170 ms the paper tick itself takes.
        """
        from .execution import KillSwitch, kill_switch_path

        switch = KillSwitch(kill_switch_path(self.settings)).describe()
        report = limits_report(self.settings)
        return {"switch": switch, "report": report, "measured": True}

    def pending(self, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """The notices that have not been announced yet, newest problem first.

        Pure: it takes a snapshot (from :meth:`current` if not given) and the ledger, and
        returns what *would* be sent.  Nothing is written and no request is made, so the
        dashboard and ``notify status`` can show the same list the watcher would act on.
        """
        snapshot = state if state is not None else self.current()
        ledger = _read_state(self.path)
        report = snapshot["report"]
        switch = snapshot["switch"]
        out: list[dict[str, Any]] = []

        halt_signature = self._halt_signature(switch)
        if switch.get("blocked") and halt_signature != (ledger.get(HALT) or {}).get("signature"):
            out.append(
                {
                    "kind": HALT,
                    "signature": halt_signature,
                    "text": halt_message(
                        switch, report, threshold=float(self.settings.notify.warn_below_frac)
                    ),
                    "reason": switch.get("reason") or "",
                    "automatic": armed_by_the_program(switch),
                }
            )

        if switch.get("blocked"):
            # The halt supersedes every band: its message already carries all four limits,
            # and a second notice about the same state would be noise, not information.
            return out

        threshold = float(getattr(self.settings.notify, "warn_below_frac", 0.0) or 0.0)
        told = announced_of(ledger)
        for name, band in halting_bands(report, threshold).items():
            # A band is news once per approach: the ledger holds the band each limit was
            # announced in, and `record` drops the ones that left it.  Moving from one
            # band to another (close -> past, or back) is news again.
            if told.get(name) == band:
                continue
            check = next(c for c in report["checks"] if c["name"] == name)
            out.append(
                {
                    "kind": WARNING,
                    "signature": f"{band}:{name}",
                    "band": band,
                    "text": warning_message(check, report, threshold=threshold, band=band),
                    "reason": name,
                    "check": check,
                }
            )
        return out

    @staticmethod
    def _halt_signature(switch: dict[str, Any]) -> str:
        """A halt is the same halt while the switch file says the same thing."""
        return f"{switch.get('triggered_at')}|{switch.get('triggered_by')}|{switch.get('reason')}"

    # ---------------------------------------------------------------- writing
    def record(
        self, notices: list[dict[str, Any]], snapshot: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fold successfully sent notices into the ledger, and forget recovered warnings.

        A limit that left the warning band is dropped, so the next approach is news
        again; one that is still inside it stays announced and sends nothing more.

        Delivered notices are also appended to the ledger's bounded history: this is the
        one place a message that actually went out passes through, which is what makes the
        history trustworthy - and it does not grow when nothing is sent.
        """
        state = _read_state(self.path)
        previous = announced_of(state)
        announced = dict(previous)
        changed = False
        history = state.get("sent")
        history = list(history) if isinstance(history, list) else []
        delivered = [_sent_entry(notice) for notice in notices if notice.get("at")]
        if delivered:
            state["sent"] = (history + delivered)[-SENT_LOG_LIMIT:]
            changed = True
        for notice in notices:
            if notice["kind"] == HALT:
                entry = {
                    "signature": notice["signature"],
                    "reason": notice["reason"],
                    "at": notice.get("at"),
                }
                if state.get(HALT) != entry:
                    state[HALT] = entry
                    changed = True
            else:
                announced[notice["reason"]] = notice["band"]

        snap = snapshot if snapshot is not None else self.current()
        threshold = float(getattr(self.settings.notify, "warn_below_frac", 0.0) or 0.0)
        here = halting_bands(snap["report"], threshold)
        # Keep what is still in a band (so it stays quiet), drop what left one (so the
        # next approach is news again), and keep the un-delivered old bands as they are
        # so a failed send is retried next round.
        kept = {name: band for name, band in announced.items() if name in here}
        if kept != previous:
            if kept:
                state[WARNING] = {
                    "announced": kept,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            else:
                state.pop(WARNING, None)
            changed = True
        if changed:
            _write_state(self.path, state)
        return state

    def clear(self) -> None:
        """Forget what was announced (``notify reset``): the next halt is announced again.

        The **history** of what already went out is kept: a reset re-arms the channel, it
        does not rewrite what happened.  Only the dedupe state is dropped.
        """
        state = _read_state(self.path)
        kept = state.get("sent")
        _write_state(self.path, {"sent": kept} if isinstance(kept, list) and kept else {})

    # ---------------------------------------------------------------- sending
    def notify(self, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Send every pending notice, and report what happened to each one.

        Returns one entry per notice: ``sent`` (whether it went out) and ``message`` (the
        reason, or ``sent``).  A notice that failed stays pending - the ledger is only
        updated for the ones that actually went out, and :meth:`record` is called with
        exactly those.
        """
        with self._lock:
            snapshot = state if state is not None else self.current()
            notices = self.pending(snapshot)
            if not notices:
                # Nothing to send - but a warning that has *recovered* still has to be
                # forgotten here, or its next approach would look already announced.
                self.record([], snapshot)
                return []
            outcomes: list[dict[str, Any]] = []
            delivered: list[dict[str, Any]] = []
            for notice in notices:
                ok, message = send(self.settings, notice["text"], session=self.session)
                if ok:
                    notice["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    delivered.append(notice)
                outcomes.append(
                    {
                        "kind": notice["kind"],
                        "reason": notice["reason"],
                        "sent": bool(ok),
                        "message": message,
                        "recorded": bool(ok),
                    }
                )
            # One ledger update per round.  Only what actually went out is remembered (a
            # message that failed stays pending and is retried on the next call), and a
            # limit that left the warning band is forgotten at the same time.
            self.record(delivered, snapshot)
            return outcomes

    def watch_once(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """One round: read, decide, send.  What ``notify watch`` prints each interval."""
        snapshot = state if state is not None else self.current()
        outcomes = self.notify(snapshot)
        return {"snapshot": snapshot, "outcomes": outcomes, "pending": len(outcomes)}
