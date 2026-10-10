# The order-level rail: paper broker, pre-trade gate, kill switch

The research side of this project (`backtest`, `evaluate`, `paper`) simulates
**bars**. This document is about the other layer the brief asks for: an
**order**-level rail with an abstract adapter, a deterministic risk gate in front
of it, and a kill switch that stops new orders for good.

Dependencies: none beyond `pandas`. `selftrade/execution/` imports neither
`requests` nor `socket`, and a test parses every file in the package to keep it
that way. There is no credential, no endpoint and no live account anywhere in it.

```
selftrade/execution/
    base.py          BrokerAdapter + OrderRequest/Order/Fill/Position/AccountSnapshot
    paper_broker.py  PaperBroker: the simulated rail (fills, stops, ledger)
    risk_gate.py     RiskGate: the pre-trade checks, sealed limits included
    kill_switch.py   KillSwitch: the file-backed, fail-safe stop-everything switch
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
| Stop | evaluated **intrabar**; a gap through the stop fills at the (worse) open, never at the stop level |
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
| max positions | `risk_gate.max_positions` | config |
| per-position exposure | **5%** of equity | **sealed** (`MAX_POSITION_PCT`) |
| gross leverage | `risk_gate.max_leverage` | config |
| stop present, and its distance | **5%** max | **sealed** (`HARD_STOP_LOSS_PCT`) + `risk_gate.require_stop` |
| risk per trade | `risk_gate.risk_pct` (default 0.5%) | config |
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

When a limit breaks, the brief's four steps happen in order:

1. the order **is not created** (the broker returns `rejected` with the reason);
2. the reason is written to the `risk_events` table;
3. an alert is raised for the account-level breaches (drawdown, daily loss, kill
   switch);
4. an alert **engages the kill switch**, so the *next* order is refused too.

## 4. The kill switch

`KillSwitch` is a small JSON file with `blocked`, `reason`, `triggered_by`,
`triggered_at` and a bounded history. Three deliberate choices:

- **it persists** - a switch that resets on the next process start is not a switch;
- **it fails closed** - a missing file means "clear", but a *corrupt* file means
  "blocked as a precaution", not "no problem found";
- **the first cause survives** - a later breach is recorded as `trigger_again`
  and does not overwrite the reason that stopped trading first.

```
python -m selftrade switch show
python -m selftrade switch arm --reason "stopping new orders before the weekend"
python -m selftrade switch reset --reason "reviewed, positions confirmed flat"
```

**What it does not do, on purpose:** it does not close open positions. Reducing or
closing is a documented operator procedure, not something an automatic switch
should do silently. While the switch is engaged, no new order passes.

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

- **No live adapter exists yet.** The interface is implemented by the simulated
  broker only; the MT5 rail is a separate, older module with its own two-key gate.
  The Binance Spot Testnet adapter is the next piece of work, and it will be
  written against the documented API - no endpoint here is guessed.
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
  defaults, not derived from the data.
