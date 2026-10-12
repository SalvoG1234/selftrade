# Risk alerts — the halt, and the warning before it

`selftrade` can refuse to trade on its own. The gate refuses an order, the paper rail
arms the emergency stop, and the Binance rail passes every order through the same two.
All of that happens **inside the program**, which is exactly the problem: the one moment
the program needs a human is the one moment nobody is looking at it.

`selftrade/notify.py` is the optional channel that closes that loop. It is not a
general-purpose notifier — it sends two things and nothing else, both about the risk
layer:

| When | Message | Why it is worth a message |
| --- | --- | --- |
| the kill switch is **engaged** | `selftrade HALT — every new order is refused` + the numbers behind it | a stop nobody hears about is the same as no stop at all |
| a limit that can **halt** trading is inside the warning band, **or already past its cap** | `selftrade WARNING — …` | the halt cannot report this: it has not happened yet |

A fill, an equity point, a completed session and a refused order are **not** messages.
The channel is deliberately small: attention is the scarce resource, and a channel that
cries about routine events is one that gets muted before the day it matters.

## The two bands

`halting_bands(report, below)` (in `selftrade/risk_report.py`) answers *which* limits are
news, using the very report the dashboard panel and `risk limits` print:

| Band | State of the limit | Sent? |
| --- | --- | --- |
| `close` | still allowed, but at most `notify.warn_below_frac` of the cap is left (25% by default) | yes, if the threshold is above 0 |
| `past` | **breached** — the cap is already behind the account | **always**, even with `warn_below_frac: 0` |
| none | comfortable, or one of the two caps that only refuse a single order | never |

Only the two limits that can halt trading are bands at all. `position_pct` and
`leverage` can be breached for a while without anything stopping, and shouting about
that would train the reader to ignore the channel.

The **`past`** band is the one this program needed most. A limit that is already
breached does not produce a message anywhere else: the gate arms the switch the next
time it *checks an order*, which may be minutes away — or never. A finished paper
window, a weekend, a stopped loop or a session left running on a stale feed sits past
its cap indefinitely, and nothing in the system says so. Here is the real holdout
session in this repository, past the sealed drawdown cap, with the channel switched off:

```text
$ python -m selftrade risk limits
selftrade risk limits (sealed caps vs the active paper session)
BREACHED: max_drawdown - the next order is refused and arms the kill switch
  ! max_drawdown 23.87% of the 15.00% cap (room -8.87pp) - arms the kill switch [sealed]
    max_daily_loss 0.00% of the 2.00% cap (room +2.00pp) - arms the kill switch [sealed]
    position_pct 0.00% of the 5.00% cap (room +5.00pp) - refuses the order [sealed]
    leverage 0.00x of the 1.00x cap (room +1.00x) - refuses the order [config]
  measured on paper session #1 (3600 point(s), as of 2026-10-10 08:00:00+00:00)
  alerts: notify.disabled - `python -m selftrade notify status` says why
  PENDING [warning] [past] selftrade WARNING - max_drawdown is past its cap: the next order arms the stop
```

Three rules keep the volume at "rare" instead of "constant":

1. **the same halt is announced once.** The signature is the switch's own
   `triggered_at | triggered_by | reason`, so re-reading the state — every `paper` tick,
   every poll of `notify watch` — sends nothing. A *new* halt (after a reset and a
   re-arm) is news again;
2. **a warning is announced once per band, per approach.** The ledger remembers the
   band each limit was announced in; a limit that recovers is forgotten, so its next
   approach warns again, and moving from `close` to `past` (or back) is news again.
   Asking the same question twice never sends twice;3. **the ledger is only written after the message actually went out.** A network
   failure leaves the notice pending: the next round retries it and keeps reporting
   it as undelivered, instead of quietly marking it as told.

The ledger itself (`state/notify_state.json`, next to the session file) holds the
`halt` signature last delivered, the band each limit was announced in, and a **bounded
history** of what went out — the newest 50 notices, each with when, the kind, the band,
the reason, whether the *program* armed it, and the first line of the message. It is read
leniently: a file an older version wrote, or one that cannot be parsed at all, is a quiet
ledger rather than an exception — the worst case is one repeated message after an upgrade,
and a file on disk must never be able to take the channel down.

A halt **supersedes** the bands while it is engaged: its message already carries all
four limits with their room, and a second notice about the same state is noise.

## When the *program* arms it, not you

A halt is not one situation but two, and the reader behaves differently in each. If you
armed the stop yourself, the message is a receipt. If you touched **nothing**, the stop
was the program's own decision, it is still engaged, and every order is being refused
right now — that one leads with the fact and with the margin that was left at the
instant the gate refused the order:

```text
selftrade HALT (automatic) - the gate stopped trading, not you
stop armed by risk_gate: blocked: account drawdown 23.87% breaks the sealed 15.0% limit: trading halts
at 2026-10-11T12:30:26+00:00

the numbers the gate saw when it refused the order at 2024-01-01 00:00:00+00:00, equity 10,000.00:
  drawdown 23.87% of the 15.00% cap - 8.87pp past it
  today's loss 0.40% of the 2.00% cap - +1.60pp of margin left

where the account stands now:
BREACHED: max_drawdown - the next order is refused and arms the kill switch
  ! max_drawdown 23.87% of the 15.00% cap (room -8.87pp) - arms the kill switch [sealed]
    max_daily_loss 0.00% of the 2.00% cap (room +2.00pp) - arms the kill switch [sealed]
    position_pct 0.00% of the 5.00% cap (room +5.00pp) - refuses the order [sealed]
    leverage 0.00x of the 1.00x cap (room +1.00x) - refuses the order [config]
  measured on paper session #1 (3600 point(s), as of 2026-10-10 08:00:00+00:00)

arming only refuses orders - no position was closed. `python -m selftrade switch show`
reads the same file; `switch reset` disarms.
```

Three parts, in the order the reader asks them: **was this me?** (the headline: the arm
came from ``risk_gate``, which is what the paper rail passes when a sealed limit breaks,
and never from ``operator`` or ``dashboard``), **with how much room?** (the numbers the
gate saw at the refusal — see below) and **where is the account now?** (the live
measurement, which the panel and `risk limits` print too).

The middle part cannot be reconstructed afterwards, so the switch **keeps it**:
`KillSwitch.trigger(..., by="risk_gate", detail=arming_detail(account, decision.checks))`
stores the account and the two halting limits — with their `value`, cap, `headroom` and
verdict — under a `detail` key, and `notify.arming_lines` builds the block from it.
Only the numbers travel: scalars and one small list of flat rows, ~200 characters per
string, and a file written before this existed reads as "no numbers" instead of raising.
`switch show`, `/api/killswitch` and the dashboard's stop card all read the same field.

A limit that was *breached* shows how far past it the account was; the one that was still
inside its cap shows the margin that remained — which is exactly the "how close was the
other one?" question a reader of an automatic halt has. If the numbers were not recorded
(a switch file from an older version, or an arm with no gate behind it) the headline still
names the program as the cause and the live measurement is what follows.

**Where this applies:** the *paper* rail arms the stop when a sealed limit raises an
alert (`PaperBroker.submit_order` → `RiskGate` → `KillSwitch`), which is the rail `paper`
and `broker` drive. The Binance rail reaches the same gate before every order but does not
arm the switch: it records the alert and refuses that order. So an *automatic* halt — and
this notification — comes from the paper rail today; an operator arm from the CLI or the
dashboard is the other case the headline covers.

## What a message looks like

Real output, built from the repository's own paper session (an operator arm, so no
"automatic" headline):

```text
selftrade HALT - every new order is refused
stop armed by risk_gate: daily loss limit breached: new orders halted
at 2026-10-11T12:12:57+00:00

BREACHED: max_drawdown - the next order is refused and arms the kill switch
  ! max_drawdown 23.87% of the 15.00% cap (room -8.87pp) - arms the kill switch [sealed]
    max_daily_loss 0.00% of the 2.00% cap (room +2.00pp) - arms the kill switch [sealed]
    position_pct 0.00% of the 5.00% cap (room +5.00pp) - refuses the order [sealed]
    leverage 0.00x of the 1.00x cap (room +1.00x) - refuses the order [config]
  measured on paper session #1 (3600 point(s), as of 2026-10-10 08:00:00+00:00)

arming only refuses orders - no position was closed. `python -m selftrade switch show`
reads the same file; `switch reset` disarms.
```

```text
selftrade WARNING - max_drawdown is past its cap: the next order arms the stop
max_drawdown is 23.87% against the 15.00% cap, -8.87pp
nothing has halted yet: no order has been checked since the breach. The gate refuses
the next one and arms the kill switch when it sees it
```

The last line matters as much as the numbers: **a stop refuses orders, it does not
close a position.** A reader who thinks the halt flattened the position will be
surprised by the next account statement, so every halt message says that it did not.

## Setting it up

The channel is **opt-in twice**: `notify.enabled: true` in `config.yaml`, plus two
environment variables. The config holds only the **names** of those variables:

```yaml
notify:                      # risk alerts: the halt, and the warning before it
  enabled: false             # opt-in; needs both env vars below, and sends nothing otherwise
  telegram_token_env: SELFTD_TELEGRAM_TOKEN     # only the NAME of the variable
  telegram_chat_id_env: SELFTD_TELEGRAM_CHAT_ID # only the NAME; values are never printed
  warn_below_frac: 0.25      # warn when drawdown/daily loss have <=25% of their cap left (0 = off)
  watch_interval_s: 30.0     # `notify watch` cadence; halts are announced once, never re-sent
```

Create a bot with **@BotFather** (`/newbot`) and copy the token; send that bot any
message and read the chat id from `https://api.telegram.org/bot<token>/getUpdates`.
Then export both, in the shell that runs the bot (Git Bash and PowerShell shown):

```bash
export SELFTD_TELEGRAM_TOKEN='123456:ABC...'
export SELFTD_TELEGRAM_CHAT_ID='424242'
python -m selftrade --set notify.enabled=true notify test
```

```powershell
$env:SELFTD_TELEGRAM_TOKEN = '123456:ABC...'
$env:SELFTD_TELEGRAM_CHAT_ID = '424242'
python -m selftrade --set notify.enabled=true notify test
```

The value never enters `config.yaml`, a report, a log or this program's memory: only the
*names* travel, and `readiness()` reports `env SELFTD_TELEGRAM_TOKEN is not set` rather
than anything a reader could reuse. `tests/test_notify.py` asserts a token cannot reach
the sent body, the return message or stdout.

## The commands

| Command | What it does | Exit |
| --- | --- | --- |
| `notify status [--json]` | would I be told, **what is waiting right now** and the last few notices that actually went out — it sends nothing | 0 |
| `notify test` | one message, so the channel is proven *before* the day it matters | 1 if it did not go out |
| `notify watch [--interval S] [--once]` | the small supervisor: re-reads the switch and the limits every `notify.watch_interval_s`, sends on transitions only | 1 if anything failed |
| `notify reset` | forget what was announced: the next halt is news again. The history of what was sent is **kept** — a reset re-arms the channel, it does not rewrite what happened | 0 |

```text
$ python -m selftrade notify status
risk alerts: NOT ready: notify.disabled (channel: none)
  token env SELFTD_TELEGRAM_TOKEN | chat env SELFTD_TELEGRAM_CHAT_ID | state/notify_state.json
  warns when a halting limit has <= 25% of its cap left, and always when one is already past its cap; `notify watch` every 30s
  kill switch: ENGAGED - stopping new orders before the weekend
  PENDING [halt] selftrade HALT - every new order is refused
          would be dropped: set notify.enabled=true and export both variables
  SENT 2026-10-11T14:48:08+0200 [halt] (automatic) drawdown 23.87% breaks the sealed 15.0% limit
  SENT 2026-10-11T14:48:08+0200 [warning] [past] max_drawdown
```

(One coherent state, not three runs: an operator's arm is already *sent* nowhere because
the channel is off, and the two `SENT` lines are an earlier automatic halt and its
warning, from the same hour.)

The `SENT` lines are the history (the last three; `--json` carries all of them under
`sent`), and they are the honest half of the answer: `PENDING` is what *would* go out,
`SENT` is what did. When the stop is engaged and already announced, the command says so
— *"this halt has already been announced (see the last SENT line below)"* — instead of
claiming that no halt is engaged.

`notify status` is the one command worth learning: it shows the state of the channel,
the state of the switch and the list of notices that have not gone out yet, and it
prints the pending message even when the channel is **off** — with the line that says it
would be dropped. `notify watch` refuses to start at all until the channel is ready,
rather than running a supervisor that silently sends nothing.

## Where it runs

* **in the paper rail** (`selftrade/paper.py`): every tick asks `risk_notices(settings)`
  and prints `alert [halt] sent: …` / `NOT sent: …`. The channel is wrapped: an exception
  inside it becomes a printed `notify_error` notice instead of stopping the session;
* **as a standalone supervisor** (`notify watch`), for a machine that trades without a
  terminal attached;
* **on the dashboard**: the risk-limits card carries an `alerts:` chip — `telegram
  ready`, `off (notify.disabled)`, or `ENABLED but …` — next to `— N waiting to be
  sent`, the same list `notify status` prints; and the **Alerts** card under it lists what
  actually went out (when, kind, band, why, first line), newest first, marking a halt the
  program armed as `HALT (automatic)` (see `docs/DASHBOARD.md`).

## What it cannot do

* it **never closes a position**: arming a stop refuses new orders. Reducing or
  flattening is a documented operator procedure, not something a notification does;
* it only speaks **Telegram**. There is no email, SMS or webhook;
* it has **no queue and no retry timer**: a failed send is retried on the *next* round
  (each `paper` tick, or each `watch` interval) and reported as undelivered until it
  succeeds;
* **one watcher per state directory.** The ledger is read-modify-write with no lock
  across processes, so two supervisors on the same file can each send the same halt.
  Run one — the paper rail and `notify watch` share the same ledger and are already
  deduplicated *between ticks*, not between simultaneous processes;
* it costs **one database read and one file read per round** (~1 ms on a 3 600-point
  session, against ~170 ms for the paper tick itself) and a request only when there is
  news. With nothing to send it makes no HTTP request and writes no file at all.

## Coverage

* **semantics** (`tests/test_notify.py`, 25 tests): the halt is announced once and a new
  halt is news again; a warning fires once per band and re-arms after recovery; a limit
  past its cap is announced with nothing engaged, even with `warn_below_frac: 0`; a halt
  supersedes the bands; a past limit is announced again after a disarm (when the breach
  was never announced before it); a ledger an older version wrote - or one that cannot be
  parsed at all - is read instead of raising, so a file on disk can never take the
  channel down; a disabled channel reports the pending notice but sends nothing and
  remembers nothing; a 500 leaves the notice pending and it goes out on the next round; a
  halt with an unreadable account still goes out with the reason; an automatic halt says
  the program armed it and quotes the drawdown, the day's loss and the margin that was
  left, while a halt the operator armed claims nothing and shows only the live numbers;
  an automatic halt with no recorded numbers still names the program; a quiet round sends
  nothing and writes nothing; the ledger's bounded history holds only what actually went
  out (a failure is absent, a reset keeps it, the oldest fall off at the limit); the token
  never appears in the body, the message or stdout;
* **wiring**: `paper` (the notice reaches the printed status), the CLI (status/test/watch/
  reset, the PENDING and SENT lines of `notify status`, the PENDING lines of
  `risk limits`, exit codes), the dashboard (the chip, the pending list, the **Alerts**
  card over `/api/alerts`, and GETs that leave the ledger alone);
* **live in this environment** (real state files, the transport swapped for a local
  server — the documented injection point):

  ```text
  ready: True | switch engaged: False
  -- round 1: the account is 23.87% down against the sealed 15% and nothing has stopped
       warning: sent=True (sent)
       ledger: {'announced': {'max_drawdown': 'past'}, 'at': '2026-10-11T14:12:50+0200'}
  -- round 2: one second later: the same band is not news again
       nothing to send
  -- round 3: the gate arms on a daily loss - Telegram answers 500, so nothing is recorded
       halt: sent=False (telegram returned status 500)
       ledger has no halt: True
  -- round 4: the transport works again: the halt that never went out goes out now
       halt: sent=True (sent)
  -- round 5: and it is not repeated
       nothing to send
  -- round 6: the operator disarms the stop: the halt is old news, the breach was already told
       nothing to send
  requests made: 3 | chat id used: 424242
  ```

  and, on the **automatic** halt, the whole path in one run: switch clear → the real
  `PaperBroker` refuses an order on the sealed drawdown → the gate arms the switch
  (`by risk_gate`, with the numbers) → the channel delivers the HALT once, the ledger
  records it, a second round sends nothing and `outstanding()` is empty.

  And, against the **real** `api.telegram.org` with a deliberately bogus token, the
  failure is reported instead of raised: `notify test: NOT sent (telegram returned status
  401)`, exit 1, with no ledger written (`notify watch --once` printed
  `[halt] NOT sent: telegram returned status 401 (…)`, exit 1, and the notice stayed
  pending).

**Not verified here:** no real bot token exists in this environment, so no message has
ever been delivered to a real Telegram chat, and the delivered case was exercised
against a local server through the injected transport.
