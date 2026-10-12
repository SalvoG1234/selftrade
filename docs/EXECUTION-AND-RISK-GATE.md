# The order-level rail: paper broker, pre-trade gate, kill switch

The search side of this project (`backtest`, `evaluate`, `invest`) simulates
**bars**. This document is about the other layer the brief asks for: an
**order**-level rail with an abstract adapter, a deterministic risk gate in front
of it, and a kill switch that stops new orders for good.

`paper` (the forward session) is the caller that uses both: it turns the strategy's
signals into orders, so the gate is on the path instead of in the documentation -
see section 5 and `docs/PAPER-TRADING.md`.

Dependencies: none beyond `pandas`. `selftrade/execution/` imports neither
`requests` nor `socket`, and a test parses every file in the package to keep it
that way. There is no credential, no endpoint and no live account anywhere in it.

```
selftrade/execution/
    base.py          BrokerAdapter + OrderRequest/Order/Fill/Position/AccountSnapshot
    paper_broker.py  PaperBroker: the simulated rail (fills, exit plans, ledger)
    risk_gate.py     RiskGate: the pre-trade checks, sealed limits included
    kill_switch.py   KillSwitch: the file-backed, fail-safe stop-everything switch
    runner.py        run_orders: the forward session - a genome driven through the
                     broker, order by order, with the gate on the path (what `paper`
                     now runs; equivalence with the bar engine is asserted in tests)
```

## 1. The contract

`BrokerAdapter` is an ABC with the seven calls the brief names:

| Call | Returns |
| --- | --- |
| `get_account_balance()` | `AccountSnapshot`: cash, equity, realized/unrealized P&L, day P&L %, drawdown, open positions, fees |
| `get_positions()` | `list[Position]` |
| `get_market_data(symbol, limit)` | the most recent OHLCV bars the adapter can see |
| `submit_order(request)` | an `Order`; a refused order comes back with `status=rejected` and `reject_reason` |
| `cancel_order(order_id)` | the canceled order, or `BrokerError` if it was already filled |
| `get_order_status(order_id)` | one order, or `None` |
| `get_trade_history(limit)` | fills, oldest first |

Extras: `open_orders()`, `reconcile()`, `describe()`. `mode` is `"paper"` unless an
adapter explicitly says otherwise - only a live adapter may say `"live"`, and the
MT5 rail is the one that does.

## 2. The simulated broker

`PaperBroker` runs on a local OHLCV frame and is deliberately built to agree with
the research engine instead of being a second, friendlier reality:

| Rule | Detail |
| --- | --- |
| Fill | a signal submitted while looking at bar *i* fills at the **open of bar *i+1*** - the same rule as `backtest.simulate` |
| Costs | `fee_bps + slippage_bps` **per side**, applied against you to the effective price |
| Size | `qty` in the base asset, or `quote_qty` (the amount to spend) resolved **at the fill price**: `qty = quote_qty / effective_price`, which is exactly how the engine sizes a position from an equity fraction |
| Exit plan | `stop_loss_pct`, `take_profit_pct` and `trailing_pct` on the order are armed on the fill and evaluated **intrabar** with the engine's ladder: a gap through the stop fills at the (worse) open, the stop is decided before the target, and the trailing level follows the best price seen since the entry |
| Cash | a buy must be payable: this rail does not invent cash |
| Shorts | a sell larger than the position becomes a short; the gate caps the exposure (an `allow_short` decision is the caller's, as in the backtester) |
| Idempotency | the same `client_id` returns the same order instead of doubling the position (protection from duplicate orders) |
| Ledger | every fill is replayed by `reconcile()` to rebuild positions, cash and fees; any drift is reported, not corrected silently |

The gate is optional *in the constructor* and absent by default only so the broker
can be tested in isolation; every real caller (the CLI, the MT5 rail) passes one.

## 3. The pre-trade gate

`RiskGate.check(request, account, positions, price=…, last_order_at=…, now=…,
kill_switch=…)` returns a `Decision` carrying `ok`, `reasons`, `alerts` and the
full `checks` list with the numbers that produced the verdict - so a refusal can
be explained instead of just denied.

| Check | Limit | Where the limit lives |
| --- | --- | --- |
| kill switch | engaged → every order refused | `state/kill_switch.json` |
| allowed symbol | `risk_gate.allowed_symbols` (empty = all) | config |
| qty / reference price | positive, known | code |
| minimum notional | `risk_gate.min_notional` | config |
| max positions | `risk_gate.max_positions` (a new symbol only) | config |
| per-position exposure | **5%** of equity, measured on the exposure the order **leaves** on the book | **sealed** (`MAX_POSITION_PCT`) |
| gross leverage | `risk_gate.max_leverage` | config |
| stop present, and its distance | **5%** max, for orders that **open or increase** exposure | **sealed** (`HARD_STOP_LOSS_PCT`) + `risk_gate.require_stop` |
| risk per trade | `risk_gate.risk_pct` (default 0.5%) on the exposure the order **adds** | config |
| account drawdown | **15%** | **sealed** (`MAX_DRAWDOWN_PCT`) |
| daily loss | **2%** | **sealed** (`MAX_DAILY_LOSS_PCT`) |
| order frequency | `risk_gate.min_seconds_between_orders` | config |

So the brief's split holds: the user can set every threshold, but only the ones
under `risk_gate:` in `config.yaml` - plus `--set risk_gate.max_positions=3` - and
`RiskGate.from_config(settings.risk_gate)` builds the gate from exactly those. The
three that must not move with a config file are in the sealed layer and are read
from there unconditionally. `--set risk_gate.MAX_DRAWDOWN_PCT=99` fails as an
unknown key, exactly like a typo.

The portfolio-wide limits come from `selftrade.risk_rules` (see
`docs/AUTOPSY-AND-CRITIC.md`): frozen dataclass, pinned SHA-256, no config key, no
genome and no environment variable. There is no flag here to widen them, and
`MODEL_CAN_OVERRIDE` is `False` and asserted.

One distinction runs through those checks, and it is not cosmetic: an order that
**opens or increases** exposure is new risk, while an order that **closes or trims**
a position is not. The exposure cap and the leverage check therefore look at the
exposure the order *leaves* on the book (a sell of the whole position leaves 0%), and
the stop requirement applies to the exposure the order *adds*. Without that, the same
5% cap that lets an entry through would block the exit that flattens it - risk
control that forbids de-risking. A reversal is treated as what it is: the excess over
the current position is the new risk, and it is capped as such.

When a limit breaks, the brief's four steps happen in order:

1. the order **is not created** (the broker returns `rejected` with the reason);
2. the reason is written to the `risk_events` table;
3. an alert is raised for the account-level breaches (drawdown, daily loss, kill
   switch);
4. an alert **engages the kill switch**, so the *next* order is refused too.

The alert in steps 3 and 4 is a value on the `Decision`; it becomes a *message* only if
the optional alert channel is on (`docs/NOTIFICATIONS.md`). Note the asymmetry that
channel exists for: this gate can only report a breach **when an order is checked**, so
an account that sits past a cap while nothing tries to trade — a finished paper window,
a weekend, a stopped loop — produces no halt at all. That is why the channel measures
the account itself (`selftrade.risk_report.limits_report`, the same report the panel
and `risk limits` print) and announces a limit that is already past its cap without
waiting for the gate to see it.

The limits are not re-implemented anywhere. `account_limits(limits, account)` and
`exposure_limits(limits, equity, …)` are the functions this gate uses, and the
dashboard's *Risk limits* panel calls the **same two** on the account it reads from the
state database - so the page cannot report a limit the gate enforces differently, and
the `headroom` it shows is the room before the check above fails. Each entry the two
functions return carries the exact `reason`/`alert` the gate appends to a refused
`Decision` next to the `value`/`limit` the panel renders, which is what makes
`tests/test_dashboard.py` able to assert the two agree value for value. Note which of
the four can halt trading: the drawdown and the daily loss raise the alert that arms
the switch, while the exposure and leverage caps only refuse the order that breaks
them and leave the rail running.

## 4. The kill switch

`KillSwitch` is a small JSON file with `blocked`, `reason`, `triggered_by`,
`triggered_at`, `detail` and a bounded history. Four deliberate choices:

- **it persists** - a switch that resets on the next process start is not a switch;
- **it fails closed** - a missing file means "clear", but a *corrupt* file means
  "blocked as a precaution", not "no problem found";
- **the first cause survives** - a later breach is recorded as `trigger_again`
  and does not overwrite the reason that stopped trading first;
- **the numbers of that moment survive with it** - when the *program* arms the switch
  (`by="risk_gate"`, which is what `paper_broker.submit_order` passes when a sealed
  limit raises an alert) it also stores `arming_detail(account, decision.checks)`: the
  account and the two halting limits with their cap, their `headroom` and their verdict.
  The margin the gate saw at the refusal is a property of that instant - minutes later
  the account has moved - so it is written down where it happens, and the HALT alert
  (`docs/NOTIFICATIONS.md`) and the dashboard's stop card report it. Only scalars and
  one small list of flat rows are kept; a file written before this existed has none,
  and nothing raises over that.

```
python -m selftrade switch show
python -m selftrade switch arm --reason "stopping new orders before the weekend"
python -m selftrade switch reset --reason "reviewed, positions confirmed flat"
```

The switch's own `detail` is not a second opinion about the limits: it is the gate's
`value`/`limit`/`headroom` at one past instant, kept because it cannot be recomputed.

```
python -m selftrade switch show          # blocked, reason, triggered_by, and detail
```

The **dashboard** is a second front-end for that same file, not a second switch: the
*Emergency stop* card's **arm** / **disarm** buttons write `state/kill_switch.json` and
the page re-reads it with the panels, so the browser and `switch show` can never
disagree. Because a click there changes whether orders are accepted, both directions
require a reason and an explicit `confirm: true` that the **server** checks (a POST
without it is refused with `400` and nothing is written), and every action is filed
twice - in the switch's own history and as a `risk_events` row of kind `kill_switch`.
See `docs/DASHBOARD.md`.

**What it does not do, on purpose:** it does not close open positions. Reducing or
closing is a documented operator procedure, not something an automatic switch
should do silently. While the switch is engaged, no new order passes — from the CLI,
the loop, the Binance rail or the dashboard.

## 4b. The forward session (`runner.py`)

`run_orders(df, genome, …, gate=…, kill_switch=…, memory=…)` walks the bars exactly
as `backtest.simulate` does - same signal timing, same sizing, same exit ladder - but
submits an `OrderRequest` for every entry and exit instead of booking the fill itself.
It returns the engine's shape (`equity`, `trades`, `metrics`) plus the order log, the
refusals and the fills, so a caller can see *how* the result was reached.

`paper.run_session` is that caller: it pins the session's settings at creation
(`paper.session_settings`, with the effective size), passes
`RiskGate.from_config(settings.risk_gate)` and the kill switch, and stores the closed
trades and the equity curve as before. Its orders carry a `client_id` that names the
session and the bar, and every refusal is recorded with `paper_session` in the audit
payload.

Two honest notes:

- **It agrees with the engine, not identically to the last bit.** Trade times,
  directions, exit reasons and prices are exact; sizes, PnL and equity agree to
  ~1e-15, because the engine adds `pnl` to a running equity while the broker moves
  cash in and out. A reader who needs more than that wants one ledger, not two.
- **It is slower than the bar engine** (~10x per pass on 3 600 bars: 0.17 s vs
  0.02 s), because an order is an object that can be refused, gated and audited.
  The session pays for the sum of the prefixes on top of that.

## 5. What was actually executed

```
$ python -m selftrade broker --bars 600
paper broker demo on BTC/USDT, bars=600
cash 10,000.00 | fee 10.0bps + slippage 5.0bps per side | mode paper
gate: armed (max_positions=1, leverage<=1.0, risk/trade<=0.500%) | sealed limits still apply: 5.0% / 15.0% / 2.0%
  entry_point        bar=8  how=auto  price=62583.26
  oversized_order    status=rejected  reason=blocked: 50.00% of equity on BTC/USDT breaks the
                     sealed 5.0% per-position cap; the order risks 1.000% of equity, above the
                     0.500% per-trade budget  limit=5.0
  retry_same_client_id same_order=True  status=rejected
  compliant_order    status=new  qty=0.007190421208482907
  fill               status=filled  avg_price=62677.13489000001  bar=9
  hard_stop          triggered=True  price=61331.45680391171  bar=24  bars_available=600
account: equity 9,990.32 cash 9,990.32 realized -9.68 fees 1.34 positions 0 drawdown 0.17%
ledger reconciliation: OK
risk events: 1 logged by this demo, 3 in the database
kill switch: clear
```

Real BTC/USDT 4h prices (the `gate:` line reports what is armed and what stays
sealed); `entry auto` walks to the first bar in the window whose next 60 bars
genuinely test the 2% stop, so the mechanism is exercised on real prices rather
than hoped for - and the choice is printed, never hidden.

With the switch armed, the same command refuses **both** orders and opens nothing:

```
$ python -m selftrade switch arm --reason "armed for the demo"
$ python -m selftrade broker --bars 200
account: equity 10,000.00 cash 10,000.00 realized 0.00 fees 0.00 positions 0 drawdown 0.00%
ledger reconciliation: OK
risk events: 2 logged by this demo, 7 in the database
  [order_blocked] blocked: kill switch is engaged (armed for the demo)
kill switch: ENGAGED
```

## 6. Limits of this layer, stated plainly

- **The simulated broker is the only rail `paper` can use.** The MT5 rail is a
  separate, older module with its own two-key gate, and the Binance Spot Testnet
  adapter (`binance.py`) is beside the package rather than inside it: `run_orders`
  builds a `PaperBroker` today, and wiring a different adapter in would mean the
  session's fills come from somewhere else entirely - a decision to take on purpose,
  with the API keys that go with it.
- **No partial fills, no order book, no queue position.** A market order fills
  whole at the next open; a limit order fills whole when the bar touches it.
- **No margin, financing, borrow costs or liquidation.** A short is just a
  negative position; leverage is only *checked*, never financed.
- **One symbol per broker instance.** Multi-instrument portfolios need one rail
  per instrument or a live adapter that speaks the whole account.
- **`reconcile()` checks the adapter against its own ledger**, which is what a
  reconnect needs, but it cannot detect a broker-side change that also rewrote
  the ledger.
- The gate's default `risk_pct` (0.5%) and `max_positions` (1) are conservative
  defaults, not derived from the data. With the shipped 5% sizing, a stop wider than
  10% of the entry would exceed the default per-trade budget; the sealed hard stop
  already refuses anything past 5%, and the critic refuses that genome at
  deployment, so the two layers agree rather than contradict.
- The session's trailing stop can only *tighten* the stop: the effective level is the
  tighter of the hard stop and the trailing level, as in the engine. A wide
  `trailing_pct` is therefore harmless, and no separate limit is checked for it.
- The dashboard's risk panel measures the **active paper session**, and it derives the
  open exposure as `equity - cash` because the session stores no positions table
  (`tests/test_paper.py` pins that against the fill ledger). It is a *reading* of the
  last recorded bar, not a live book: between two `paper` steps the numbers stand still,
  and with the Binance rail enabled they say nothing about the real account - signed
  requests against the exchange are the key panel's job. A breached drawdown there is
  historical: the panel reports that the next order arms the stop, it never arms one.
