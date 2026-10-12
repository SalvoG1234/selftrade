# Paper trading

Paper trading runs the **champion** strategy forward over an out-of-sample window.
It is a **forward order session**: the strategy's signals become `OrderRequest`s
that pass the pre-trade risk gate and fill on the simulated rail in
`selftrade/execution/`. There is still no connection, no credential and no real
money - `mode` is `paper`, and nothing here reaches a provider.

What it is *not* is a second simulator with its own flattering accounting. The
mechanics are the backtester's:

| Rule | Detail |
| --- | --- |
| Signals | computed on the window, the genome's own (or injected, for tests) |
| Fill | a signal at the close of bar *i* becomes an order that fills at the **open of bar *i+1*** |
| Size | `position_size × equity` at the signal bar, sent as `quote_qty` and resolved at the fill price - the same size the engine computes |
| Costs | `fee_bps + slippage_bps` per side, applied against you |
| Exit plan | the hard stop, take profit and trailing stop are armed on the fill and checked **intrabar**, with the engine's ladder: a gap through the stop fills at the worse open, the stop is decided before the target, the trailing level follows the best price seen |
| Shorts | need `backtest.allow_short` **and** the genome's `trade_shorts` |
| Last bar | a position still open is reported as `end_of_data` and is **not** a closed trade |

`tests/test_execution_runner.py` asserts that agreement on synthetic bars, on
hand-built bars (gap, target, trailing) and on the real CSV: same prices, times,
directions and exit reasons. Sizes, PnL and equity agree to ~1e-15, the difference
being floating-point associativity - the engine adds `pnl` to a running equity while
the broker moves cash in and out.

## What the session adds: the path

Every entry and exit is an order that must pass the gate first. So:

- **A refused order is not traded.** It comes back with its reasons, is written to
  `risk_events` with the session id and the order's `client_id`
  (`ps<session>-<bar>-entry`), and is counted in the status. `paper --run` continues
  instead of falling over.
- **The session is sized to pass the sealed cap.** `position_size` in the session's
  manifest is `risk_rules.effective_position_size(settings)` - the configured
  `backtest.position_size`, clamped by the sealed `MAX_POSITION_PCT` (5%). A 95%
  order can only ever be refused, and a session that never trades measures nothing;
  the manifest keeps `configured_position_size` too, so the clamp is visible rather
  than silent. A session written before this existed (or hand-edited) is clamped at
  run time as well.
- **A genome the sealed layer refuses cannot be paper-traded.** The champion sampled
  with `trend_momentum` seed 3 carries an 11% stop: outside the sealed 5% hard stop,
  so the critic refuses the deployment *and* every one of its entries is refused
  here. Nothing about that genome is quietly traded anyway.
- **The kill switch stops the session.** `switch arm --reason ...` (or the
  dashboard's *Emergency stop* card, which writes the same file) and every new
  order is refused for the rest of the window: the session stays flat, its equity
  does not move, and the refusals are on record. `switch reset` plus
  `paper --reset` resumes exactly as before.
- **The gate cannot be bypassed with a flag.** `risk_gate.enabled: false` in the
  config is the only way off, and it is a config change (`--set` counts), not a
  paper-only switch.

The research backtester is *not* clamped - exploration has to be free - which is why
the two rails differ in exactly one number, the position size, and agree on
everything else (see `docs/AUTOPSY-AND-CRITIC.md` for that boundary).

## Funding the session

A session trades whatever capital it was given. By default that is the config's
`backtest.initial_cash`; an operator can instead **credit a simulated account** and
allocate from it, which is the same engine with more capital behind it:

```powershell
python -m selftrade capital deposit 50000 --reason "seed capital"
python -m selftrade paper --create --fund 50000   # created and funded before it trades
python -m selftrade paper --all
```

Two rules make that safe to read:

* the capital can only be added **before the session has traded**. Every position is
  sized as a fraction of equity and the session re-runs its window prefix, so capital
  added late would re-size the trades already recorded - the ledger would stay true and
  the equity curve would not;
* the *relative* risk does not move: the sealed `MAX_POSITION_PCT` decides the size on
  funded capital exactly as it does on the config's own, so five times the capital takes
  the same trades five times as large, at the same return.

`paper --status` and `GET /api/paper` both say where the capital came from
(`funded_capital`, `capital_source`: `ledger` or `config`). The account, its ledger and
its rules are in `docs/CAPITAL.md`.

## Windows

Selected by `paper.window`:

| value | window |
| --- | --- |
| `holdout` (default) | the tail `evaluation.holdout_frac` of the series |
| `recent` | the last `paper.window_bars` bars |
| `full` | the whole series |

If the holdout is too small, `holdout` falls back to `recent` and the session
records `window=recent`.

## The cursor

Progress is a **persistent cursor** in `state/selftrade.db` (table
`paper_sessions`). Each run advances the cursor by `paper.step_bars` bars.
`paper.max_ticks` caps the number of steps per invocation (0 = unlimited).

## Why stepping cannot drift

Each step re-runs the window prefix `[0:cursor]` - signals, orders, gate, fills,
equity - and rewrites the session's derived rows idempotently. Because both the
strategy and the rail are causal, the prefix result never changes when the window
grows, so:

- finishing the window in one shot,
- finishing it in many small steps, or
- completing it after a process restart

all yield **identical trades and an identical equity curve**. Tests assert this, and
another asserts that the session matches `backtest.simulate` on the same window.

Re-running a prefix also re-records its refusals, so `risk_events` may hold the same
refusal more than once while a session is being stepped; the status reports the count
from the run that just completed, and `paper --status` in a new process falls back to
counting the session's most recent 500 recorded refusals.

## Commands

```powershell
.\paper.ps1 -Create          # create a session for the champion
.\paper.ps1 -Run             # advance one block
.\paper.ps1 -All             # run to the end of the window
.\paper.ps1 -Status
.\paper.ps1 -Reset           # destructive: requires this explicit flag
.\paper.ps1 -Status -Json
```

Equivalently: `python -m selftrade paper --create` … `--all`.

`--create` requires a champion in the hall of fame (`evolve` first) **and** the
critic's approval: a genome that breaks a sealed limit is refused here, with the
reason on stderr and the refusal stored as a lesson. On the real data shipped with
this project, no hall-of-fame genome passes that gate - the refusal is the honest
result, not a bug (`paper --create` exits 1).

## Reading the result

The summary shows cursor progress, cash, **equity** (cash plus an open position),
total return, the trade count and - when there are any - the risk-gate refusals on
record. The dashboard (`serve`) shows the same plus the equity curve and the trade
list.

Every tick also asks the alert channel what is new and prints it, so the session that
stops on a limit says so where a human will see it:

```text
  alert [halt] sent: sent
  alert [warning] NOT sent: telegram returned status 401
```

The rail never depends on that channel: a failure inside it is reported as a
`notify_error` notice instead of stopping the session, and a message that did not go
out stays pending (see `docs/NOTIFICATIONS.md`).

The session stores **trades, the equity curve and the cash** - not positions. Since
the rail marks its book to market, the row it writes is the same one the broker uses:
`equity = cash + positions_value`, so `equity - cash` **is** the open exposure, and
that is what the dashboard's risk-limits panel reports against the sealed per-position
cap. `tests/test_paper.py` pins it: on a session stopped one bar after an entry, the
difference equals the net position rebuilt from the fill ledger
(`net_qty × last close`), not a second guess at the same number.

Measured cost, so it is not a surprise: a 3 600-bar holdout at the shipped
`step_bars: 168` took ~4 s for `--all` (~0.17 s for a single pass), against ~0.02 s
for the bar engine. The stepping design pays for the sum of the prefixes.

Numbers are a simulation on historical prices, not a market result, and no order has
ever been sent anywhere.
