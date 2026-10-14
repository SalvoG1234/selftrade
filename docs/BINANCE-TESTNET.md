# Binance Spot Testnet: the first real API rail

`selftrade/binance.py` implements `BrokerAdapter` against the **Spot Testnet**.
It is the only module in the project that signs a request for an exchange, and it
is deliberately not inside `selftrade/execution/`: that package is the offline
contract and its simulation, and a test keeps it free of any network import.
Adapters live beside `mt5.py`.

Everything it does comes from the official documentation, listed at the bottom.
Nothing here is inferred from a tutorial or from memory: where the docs are
silent, this file says so and refuses to guess.

## 1. The documented facts this module is built on

| Fact | Source of truth |
| --- | --- |
| Base URL `https://testnet.binance.vision/api`; endpoint paths stay `/api/v3/...` | Testnet REST docs (the page says to replace `https://api.binance.com/api` with the testnet base) |
| The testnet serves `/api` only, never `/sapi` | Testnet REST docs |
| Auth header `X-MBX-APIKEY`; signed requests carry a `signature` parameter | Testnet REST docs |
| The signature is the **HMAC-SHA-256 hex digest** of the request's parameter string (the query string, with the body concatenated and no separator) | Testnet REST docs (which include a worked HMAC example) |
| `timestamp` is required and is in **milliseconds**; `recvWindow` defaults to **5000** and is capped at **60000** | Testnet REST docs |
| `GET /api/v3/time` → `{"serverTime": ...}`, weight 1, used to keep the clock honest | Testnet REST docs |
| Error `-1021`: "Timestamp for this request is outside of the recvWindow." | Errors docs |
| Errors are `{"code": ..., "msg": "..."}`; e.g. `-1121` invalid symbol, `-2010` insufficient balance / duplicate order, `-2013` no such order | Errors docs |
| Order placement `POST /api/v3/order`: `symbol`, `side`, `type` required; `LIMIT` needs `timeInForce`, `quantity`, `price`; `MARKET` needs `quantity` or `quoteOrderQty`; `newClientOrderId` and `newOrderRespType` optional | Testnet REST docs |
| Look up and cancel `GET`/`DELETE /api/v3/order` by `orderId` **or** `origClientOrderId`, with `symbol` | Testnet REST docs |
| `/api/v3/account` returns `balances[].{asset,free,locked}`; `/api/v3/myTrades` needs `symbol`; `/api/v3/klines` needs `symbol` + `interval` (`limit` default 500, max 1000) | Testnet REST docs |
| Filters: `PRICE_FILTER`, `LOT_SIZE`, and `MIN_NOTIONAL` / `NOTIONAL` (two distinct types) | Filters docs |
| Weight usage is reported in `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` headers; a `429` carries `Retry-After`; repeated violations can give `418` | Testnet REST docs |
| A Spot stop loss is a **separate** conditional order (`STOP_LOSS` with `stopPrice`; `STOP_LOSS_LIMIT` also needs `price`, `quantity`, `timeInForce`); there is no documented way to attach one to the entry order | Testnet REST docs / filters docs |
| Testnet keys come from https://testnet.binance.vision/ with a **GitHub** login; virtual balances are reset roughly monthly, clearing orders and history; the testnet is not always synchronised with production | Testnet general information / testnet site |

## 2. Two keys to send, and the host is a third, opt-in one

Nothing is sent to the exchange unless **both** keys are turned:

```yaml
binance:
  enabled: true     # key 1: the rail exists
  live: true        # key 2: the rail may send
```

```powershell
.\run.ps1 binance order --qty 0.001 --live    # key 2 is repeated on the command line
```

- Missing either key: the order is **built, signed and recorded** with status
  `dry_run`, and the transport is never called for it. A dry run is neither a fill
  nor a refusal, and the status says so.
- Armed: the request is sent, and the answer is mapped from whatever the exchange
  reports (`executedQty`, `cummulativeQuoteQty`).
- `mode` is `paper` while unarmed, `testnet` when armed against the testnet, and
  `live` when armed against a funded host. It is never `live` by accident: a base
  URL that is not the testnet host is refused at construction *and* by the config
  validator unless `binance.allow_production: true` opts in deliberately, so
  `binance.base_url: https://api.binance.com/api` on its own is a config error
  (exit 3), exactly like a typo.

Credentials come from the environment variables **named** in the config
(`SELFTD_BINANCE_API_KEY`, `SELFTD_BINANCE_API_SECRET`), and, when one of those is
empty, from the local key file `binance.key_file` (`state/secrets.json` by default)
that the dashboard's **API keys** page writes — the **environment always wins**, so a
machine that exports the pair can never be surprised by a file someone left behind.
Either way the value is never written to the config file, the repository, a recorded
call or a log line; the `signature` is redacted in the audit trail and the API key
header is not stored at all. A test asserts that neither value appears in anything the
adapter records, and that no API endpoint ever returns one.

**To get keys**: log in to https://testnet.binance.vision/ with GitHub, generate
an API key there, and either export both values in your own shell or paste them once
into the dashboard's **API keys** page (confirmation first, and a `key_store` audit row
after) — see `docs/DASHBOARD.md`. Never paste them into this repository, and never into
a chat.

```powershell
# either: export them for the process (the environment wins over any stored copy)
$env:SELFTD_BINANCE_API_KEY = "..."
$env:SELFTD_BINANCE_API_SECRET = "..."

# or: let the dashboard store them, then check from the terminal
.\run.ps1 binance check --set binance.enabled=true
#   ok   credentials  both credentials are available (SELFTD_BINANCE_API_KEY from the
#                     local key file, SELFTD_BINANCE_API_SECRET from the local key file)
```

The step now names **where each credential came from**, because "the rail can sign" and
"you exported it yourself" are different states for an operator to tell apart.

## 2a. Start here: `binance check`

The first thing a freshly exported key should touch is the read-only probe, and it
is the only command that *cannot* send anything: no order path exists from it,
nothing is armed (`--live` is ignored, with a note on stderr), and the report
prints how many `POST` requests the transport actually made - zero, every time.

That same report is what the dashboard's **Binance key** panel shows, on a timer:
`binance check` and `/api/binance` both call `key_health`, so the terminal and the
browser cannot drift apart - see `docs/DASHBOARD.md`.

```
$ python -m selftrade binance check --set binance.enabled=true
binance account check (Spot Testnet) - read-only: no order can come out of this command
  host          https://testnet.binance.vision/api
  mode          paper   armed=False
  credentials   SELFTD_BINANCE_API_KEY=set, SELFTD_BINANCE_API_SECRET=set
  ok   credentials  both credentials are available (SELFTD_BINANCE_API_KEY from the environment, SELFTD_BINANCE_API_SECRET from the environment); their values are never read back or printed
  ok   clock        server time - local time = +1046 ms (recvWindow 5000 ms)
  FAIL signature    Binance refused the signed call: -2014 API-key format invalid.
  ok   read_only    2 call(s) this run (GET): 0 POST
  rate budget      0/1200 weight, cooldown 0.0s
  problem: the key was not accepted (-2014 API-key format invalid.)
  hint:    the API key is malformed: check for a truncated or padded value
  verdict: problems found
```

That run used a deliberately bogus key against the **real** testnet, and it is the
first signed request this project ever made. What each step means:

| Step | The read behind it | What a failure means |
| --- | --- | --- |
| `credentials` | the two environment variables named in the config | nothing is asked of Binance at all - the command exits 1 without a single call |
| `clock` | `GET /v3/time` | the local clock is further than `recvWindow` (5000 ms by default) from the exchange |
| `signature` | the signed `GET /v3/account`, which changes nothing | the key or the secret is wrong, the key belongs to another host, its IP allow-list excludes this machine, or its permissions do not cover the call |
| `permissions` | `canTrade`, `canWithdraw`, `canDeposit`, `accountType`, `permissions[]` | a warning, not a failure: a key that can **withdraw** should have that permission revoked, and `canTrade=false` means it can read but not trade |
| `balance` | the balance rows in the same payload, priced with the last close | that step fails if the pricing call fails (`exchangeInfo`/klines), and the raw balances are printed anyway |
| `open_orders` | `GET /v3/openOrders` (read strictly, so a failure is visible) | "no open orders" and "could not read the open orders" never look the same |
| `read_only` | the transport's own log | a `POST` here would fail the step and the verdict; it cannot happen, and that is checked, not promised |

Exit codes: `0` when the probe worked (warnings are printed but do not fail it),
`1` when a step failed, when a credential is missing, or when the rail is disabled
in the config. `--json` prints the whole report.

## 2b. A funded account (opt-in, real money)

This project ships a testnet rail. A **funded** account is reachable, but only by
saying so three times, and every real order still passes the sealed gate:

```yaml
binance:
  enabled: true              # key 1: the rail exists
  live: true                 # key 2: the rail may send
  allow_production: true     # key 3: the host may be a funded one
  base_url: https://api.binance.com/api
```

```powershell
$env:SELFTD_BINANCE_API_KEY    = "..."   # never in the repo, never in a chat
$env:SELFTD_BINANCE_API_SECRET = "..."
.\run.ps1 binance order --qty 0.001 --live    # key 2 repeated on the command line
```

What the opt-in does and does not do:

- without it a production URL is a **config error** (exit 3) and, in code, a
  `HostRefusedError`; `allow_production` also refuses a *look-alike* host
  (`https://api-binance.example.com`) instead of trusting an arbitrary URL;
- it does **not** arm anything: sending still needs `enabled` + `live` + `--live`,
  and without credentials the adapter refuses to arm at all;
- **every order that is about to be sent** passes the sealed `RiskGate` and the
  kill switch first (see section 4). A refusal is recorded in `risk_events` and the
  request never leaves the machine. If the account cannot be read, the answer is a
  refusal - a gate that cannot see the book does not guess. That same switch can be
  armed or disarmed from the dashboard's *Emergency stop* card (`POST /api/killswitch`,
  with a reason and a server-checked confirmation): it writes the very file this rail
  reads, so the stop reaches a live order without going through the CLI.

**The one that used to stop you, on purpose.** `risk_gate.require_stop` defaults to
`true` and refuses an order that opens exposure without a stop.  A Spot stop is a
*separate* conditional order (`STOP_LOSS` with `stopPrice`, or `STOP_LOSS_LIMIT`),
and no API attaches one to the entry - so this rail places it itself the moment the
fill lands (section 2c).  Without a stop the gate still refuses, and so does the
adapter: it will not open a real position it cannot protect.

Warning, plainly: with `allow_production: true` and the keys exported, this harness
can move **real money** on a real account. Test on the testnet first, start with a
minimum-notional order, keep `risk_gate.max_positions` at 1, and remember the kill
switch (`python -m selftrade switch arm`) blocks new orders but does **not** close
an open one.

## 2c. The protective stop: `require_stop` is now satisfiable

`risk_gate.require_stop` defaults to `true` and refuses any order that opens exposure
without a stop.  On Spot there is no way to attach one to the entry, so the rail
places it itself, immediately after the fill:

```powershell
.\run.ps1 binance order --qty 0.001 --stop-pct -0.02 --live
```

1. **Before the entry is sent**, the stop the plan implies is computed
   (`reference * (1 + stop_pct)`, rounded down to the price tick) and checked against
   the exchange's own rules: the symbol has to offer `STOP_LOSS_LIMIT`, the price must
   satisfy `PRICE_FILTER` (tick, min, max), the size `LOT_SIZE`, the notional
   `NOTIONAL`, and the SELL side's `PERCENT_PRICE_BY_SIDE`
   (`askMultiplierDown`/`askMultiplierUp`).  If any of it would be refused, **the entry
   is refused too**: no position is opened without a place to stop out.
2. **On a fill**, the resting stop goes out as a `SELL STOP_LOSS_LIMIT` with
   `timeInForce=GTC`, `price == stopPrice` (the plan's level) and
   `newOrderRespType=RESULT` - the docs say a non-`MARKET`/`LIMIT` order defaults to
   `ACK`, which would carry no status back.  The size is the **free balance** of the
   base asset, not `executedQty`: Binance takes its commission out of the received
   asset, and a stop for the full size would be refused.
3. **The level is recomputed from the average fill price**
   (`cummulativeQuoteQty / executedQty`), so it follows where the risk actually started.
4. **The id is deterministic** (`<entry client id>-stop`) and it is looked up before
   anything is placed.  Binance documents a `newClientOrderId` as "A unique id among
   open orders ... Orders with the same newClientOrderID can be accepted only when the
   previous one is filled": the adapter does not lean on that, it asks first, so a
   retry finds the stop instead of stacking a second one on the same position.
5. **If the stop cannot be placed**, nothing is pretended: the entry order comes back
   with `UNPROTECTED: <why>` in its note, an `order_blocked` event is written, and
   `binance protect` is the way back once the cause is fixed.
6. **The algo budget comes from the exchange**: a resting stop is an "algo" order and
   `MAX_NUM_ALGO_ORDERS` (5 for `BTCUSDT` on the testnet) is a real ceiling, counted
   from the open orders Binance reports - not from a local guess.

Refused on purpose, with the reason:

| Plan | Why it is refused |
| --- | --- |
| `--stop-pct 0.02` (positive) | a long's stop sits *below* the entry, so the value has to be negative |
| `--stop-pct` with `--side sell` | a Spot stop is a SELL of the base asset, and a short side has no position to sell |
| `--take-profit-pct` / `--trailing-pct` | they need an OCO (whose legs cancel each other) or a watcher that moves the order; this rail automates neither yet, so it refuses rather than fakes |
| a stop the filters refuse | see the live refusal below: the entry never goes out |

```
$ python -m selftrade binance order --qty 0.001 --stop-pct -0.30 --set binance.enabled=true
binance paper order rejected-selftd-20261010223600 -> rejected
  refused: the protective stop this order needs would be refused: the stop 58147.75000000
           is further below the market (83068.22) than the documented askMultiplierDown 0.8 allows
```

That refusal came from the **real** testnet's filters, before anything was signed
(note that the reported market price is the live one: 83 068.22).

Protecting a position this rail did not open - and doing it across two terminals:

```powershell
.\run.ps1 binance protect --client-id <entry> --stop-pct -0.02 --live
.\run.ps1 binance unprotect --client-id <entry> --live
```

Both actions need **both** keys (`binance.live: true` and `--live`): unlike `order`,
where an unarmed run is a documented dry run, touching the protection of a real
position has no meaningful rehearsal.  The stop is found again by its deterministic
id, so `protect` in one terminal and `unprotect` in another work: they are two
processes, and neither holds the other's memory.

Trade-off, documented rather than hidden: `STOP_LOSS_LIMIT` with `price == stopPrice`
bounds the worst fill, but a gap through the level can leave the limit unfilled - that
is the difference between it and a market `STOP_LOSS`, which always executes at
whatever price is there.  The rail chose the bounded one, and says so.

## 3. What is recorded

Every call lands in the `api_calls` table (and in the process's own list), with

| column | meaning |
| --- | --- |
| `provider`, `method`, `path` | e.g. `binance-testnet`, `POST`, `/v3/order` |
| `params` | the request as sent, with `signature` replaced by `<hmac-sha256>` |
| `status`, `ok`, `code`, `msg` | the HTTP status and, on failure, Binance's own code and message |
| `dry_run` | true when the call was signed but deliberately not sent |
| `latency_ms`, `response` | how long it took, and a slimmed copy of the answer |
| `note` | why a call was refused locally (`cooldown`, `weight`, or the `-1021` retry) |

```
$ python -m selftrade binance calls
5 recorded Binance call(s), newest first
  [ok] GET /v3/exchangeInfo status=200 code=-
  [ok] GET /v3/klines status=200 code=-
  [ok] GET /v3/time status=200 code=-
```

## 4. Guards, in order

1. **Host**: the testnet by default; a funded host only with
   `binance.allow_production: true` and a known Binance host name (construction and
   config both enforce it).
2. **Kill switch + sealed risk gate**: for an order that is about to be **sent**,
   the adapter runs `RiskGate.check` itself - the sealed 5% / 2% / 15% limits, the
   configured exposure caps and the kill switch - and refuses before the request
   leaves the machine. A gate that cannot read the account, or throws, refuses too.
2b. **The protective stop** (section 2c): validated against the exchange filters
   before the entry goes out, placed right after the fill, counted against the
   symbol's algo budget, and reported as `UNPROTECTED` (with an `order_blocked`
   event) when it cannot be placed.
3. **Local rate-limit ceiling**: the weight Binance reports in its own headers is
   tracked; at `binance.max_weight_per_min` the adapter stops itself instead of
   earning a `429`. A real `429`/`418` sets a cooldown from `Retry-After`.
4. **Exchange filters**: `LOT_SIZE.stepSize` rounds quantity **down**,
   `minQty`/`NOTIONAL` refuse dust, `tickSize` rounds the limit price,
   `status` must be `TRADING` and the order type must be listed for the symbol.
   Without `exchangeInfo` nothing is guessed and nothing is sent.
5. **Duplicate protection**: before sending, the adapter asks the exchange whether
   this `clientOrderId` already exists. If that question cannot be answered, the
   order is **refused** rather than risked twice.
6. **Clock**: `-1021` triggers exactly one re-sync and one retry. The rejected
   attempt stays in the audit trail.
7. **Reconciliation**: `reconcile()` asks the exchange about our own order ids and
   reports any status that moved away from our view.

## 5. What was executed, and what could not be

Verified against the **live testnet** (public endpoints, no keys, no orders):

```
$ python -m selftrade binance doctor --set binance.enabled=true
binance spot testnet
  base_url           https://testnet.binance.vision/api
  armed              False
  mode               paper
  credentials        {'SELFTD_BINANCE_API_KEY': False, 'SELFTD_BINANCE_API_SECRET': False}
  mode_note          dry run: requests are signed and recorded, never sent
  ephemeral clock offset 2224.0 ms

$ python -m selftrade binance exchange --set binance.enabled=true
{"symbols": [{"symbol": "BTCUSDT", "status": "TRADING",
  "min_qty": "0.00001000", "step_size": "0.00001000", "tick_size": "0.01000000",
  "min_notional": "5.00000000",
  "order_types": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS",
                  "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"]}],
 "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "limit": 6000},
                {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 50},
                {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 160000},
                {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300000}]}

$ python -m selftrade binance bars --set binance.enabled=true --limit 5
{"symbol": "BTC/USDT", "interval": "4h", "rows": 5,
 "first": "2026-10-09 20:00:00+00:00", "last": "2026-10-10 12:00:00+00:00",
 "last_close": 82821.78}
```

The clock offset of ~2.2 s is why `GET /api/v3/time` matters: a naive client would
be inside the default 5 s `recvWindow` by luck alone.

Refusals, all with their exit code, none of them touching the network:

| Command | Result |
| --- | --- |
| `binance doctor` (default config) | exit 0, "disabled in config ... nothing was sent" |
| `binance account` / `positions` / `bars` / `exchange` / `history` / `open` while disabled | exit 1, same message |
| `binance order --qty 0.001 --live` (no `enabled`) | exit 1, "disabled in config" |
| `... --live --set binance.enabled=true` (no `binance.live`) | exit 1, "refusing --live ... two keys" |
| `--set binance.base_url=https://api.binance.com/api` | exit 3, config error: "must point at a testnet host" |
| `binance account --set binance.enabled=true` (no credentials) | exit 1, "missing credentials: export SELFTD_BINANCE_API_KEY ..." — nothing sent |
| `--set binance.allow_production=true --set binance.base_url=https://api-binance.example.com/api` | exit 3, "refusing a look-alike host that could be pointed anywhere" |
| funded host + `binance.live: true` + `binance order --qty 0.001 --live` (no credentials) | exit 1, "refusing to arm the binance adapter without credentials" — nothing sent |

The 77 tests in `tests/test_binance_testnet.py` run with an **injected transport**:
no test in this project makes a network call. They verify the signature recipe
against an independently computed HMAC, the millisecond timestamp after a clock
offset, the `-1021` resync-and-retry-once path, the filter arithmetic, error code
handling, the weight and `Retry-After` guards, idempotency, reconciliation, and
that a dry-run order never produces a `POST`.

Seven of them drive the **CLI itself** (`python -m selftrade binance order`, and
`protect`/`unprotect`), with the transport patched out, because the wiring is what
decides whether the guards protect a typed order or only a library call: with the kill
switch armed the CLI exits 1 and no `POST` is made; with the default
`risk_gate.require_stop` a stopless order is refused (exit 1, no `POST`); with the stop
requirement lifted exactly one `POST` goes out; `--live` without `binance.live: true`
exits 1 without ever building a client; and `protect`/`unprotect` need both keys.

The protective stop has its own group: the stop goes out **after** the fill as a
`SELL STOP_LOSS_LIMIT` sized from the free balance and priced off the average fill, a
second stop is never stacked on the same entry, an existing stop is found across
processes, a fill that cannot be protected is alerted and marked `UNPROTECTED`, the
algo budget is respected, and the **default gate now accepts** an entry that carries
its stop - which is the whole point of the change.

The **one** signed request this project has ever made is the `binance check` run
reproduced in section 2a: a deliberately bogus key was sent to the real testnet and
came back `-2014 API-key format invalid.`, with 0 `POST` and 2 calls total. That
proves the signing path reaches the exchange and that the exchange parses it - a
malformed key is refused as a *key* problem, not as a malformed request.

**Not verified, and it matters**: **no successful signed request** has been made, so
the signature has never been confirmed by a real `200` from `/api/v3/account` -
that needs a key only the user can create (a GitHub login on the testnet site), and
no testnet order has ever been placed or cancelled. Everything a correct key would
return is covered by the injected-transport tests, not by the live exchange.

## 6. Limits of this adapter

- **No take profit, no OCO, no trailing stop.** The protective stop is a single
  `STOP_LOSS_LIMIT`; a target would need an OCO list (so that one leg cancels the
  other) and a trailing stop needs something to move the order as the price moves.
  Both are refused with the reason, and neither is faked.
- **A stop is a resting order, and it can stay unfilled.** If the market gaps straight
  through `stopPrice`, a limit leg at that level may not execute; the alternative
  (`STOP_LOSS`, market) always executes but at whatever price is there. The rail chose
  the bounded one deliberately (section 2c).
- **The stop protects one entry, not a position average.** A second entry gets its own
  stop for its own size; nothing nets the two into one protective order.
- **No streaming.** No user-data stream, no websocket: everything is REST polling.
- **Only the request weight is enforced locally.** `exchangeInfo` also reports the
  order-count and `RAW_REQUESTS` limits; this adapter tracks
  `X-MBX-USED-WEIGHT-1M` against `binance.max_weight_per_min` (1200 by default,
  against the exchange's 6000) and leaves order-rate discipline to the risk gate's
  `min_seconds_between_orders`.
- **No partial-fill bookkeeping.** `PARTIALLY_FILLED` maps to `NEW`; the filled
  quantity is whatever the last response said.
- **No cost basis from the account.** A spot balance has none, so a position's
  average price is the weighted average of the buys seen in `/api/v3/myTrades`,
  falling back to the last close - documented, not invented.
- **Drawdown is local.** Peak equity is what this adapter has itself observed since
  it started; the API publishes no all-time peak.
- **The testnet is not production.** Binance documents that the testnet is not
  always synchronised with the live exchange, that balances and orders are reset
  roughly monthly, and that it does not document any history depth for klines -
  which is why research data still comes from the production public mirror
  (`data --fetch`).
- **No rerouting.** There is no retry for a failed `POST` beyond the `-1021`
  clock case: a network failure during an order is reported, never repeated
  blindly.

## Sources

- [Official Spot Testnet REST documentation](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/testnet/rest-api.md)
- [Official Spot Testnet general information](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/testnet/general-info.md)
- [Official Spot filters documentation](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/filters.md)
- [Official Spot errors documentation](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/errors.md)
- [Binance Spot Test Network (key generation)](https://testnet.binance.vision/)
