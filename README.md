# selftrade

> ### ⚠ Experimental — published to collect feedback
>
> This is a **working research project, not a product**. The configuration keys, the
> JSON payloads and the file layout still move between commits, and where the
> program cannot measure something it says `N/D` instead of inventing a number.
>
> **I am looking for feedback**, including (especially) negative feedback. If
> something is confusing, wrong, over-claimed, or unsafe in a way I have missed,
> please [open an issue](issues/new) and say so — "this is unclear" and "this
> looks wrong to me" are useful answers, not rude ones. The project needs no API
> keys to be tried (it reads public market data), so it can be reviewed from a
> single clone.

A self-improving trading-strategy search bot for Windows 11.

It loads historical OHLCV, generates strategy hypotheses, evaluates them with
realistic backtests and out-of-sample walk-forward, promotes only what passes
hard gates and an independent holdout, remembers successes **and failures**, and
papers the champion forward. An optional second rail can send orders to a
MetaTrader 5 demo account (Trading.com) behind a two-key gate.

> **It never trades real money by default and never promises profit.**
> It can run on real market prices (`data --fetch` pulls keyless public klines)
> and `invest` shows what a fixed amount of cash would have become. Those
> numbers are honest - including when they are bad - but they are still
> *historical*: they are not a forecast. Trading is at your own risk; this is not
> financial advice.

## What the real data actually said

The default config points at 18 000 real BTC/USDT 4h candles (2018-07-23 →
2026-10-10) and 30 bps round-trip costs. On that data the search found a
`trend_momentum` champion with a fold-mean Sharpe of 0.886, and the untouched
holdout **rejected it** (Sharpe −0.55). Simulating 10 000 USDT over that
genuinely out-of-sample window:

| | strategy | buy & hold |
| --- | ---: | ---: |
| final equity | 8 456 USDT | 8 608 USDT |
| total return | −15.4% | −13.9% |
| max drawdown | **28.4%** | 53.4% |

90 trades, 20% win rate, 2 472 USDT paid in costs. With costs set to zero the
same trades return **+9.3%** - the edge lived entirely in the fees. Over the
full history (in-sample, so optimistically biased) the strategy turned 10 000
into 70 386 against 107 466 for holding. Details: `docs/INVESTMENT-SIM.md`.

Every evaluation now measures the market itself on the same slices, so a fold
metric is readable as excess rather than as skill (`docs/ARCHITECTURE.md`). For
that champion, per fold, Sharpe against simply holding the asset:

| | fold 0 | fold 1 | fold 2 | fold 3 | mean | holdout |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| strategy | 0.585 | 0.966 | 1.503 | 0.486 | **0.885** | **−0.545** |
| buy & hold | −1.100 | 0.539 | 1.516 | 1.431 | 0.596 | 0.009 |
| excess | | | | | **+0.288** | **−0.554** |

So it beat holding by 0.288 Sharpe over the folds it was selected on, and lost
to it by 0.554 on the holdout: the selection advantage did not survive the one
read the search never saw. The benchmark is not part of the fitness and cannot
promote anything.

## Feedback wanted

This project is at the stage where outside eyes help more than more code. If you
have ten minutes, the questions I would most like answered are:

* **Can you make it trade when it should not?** The safety story is a sealed risk
  layer (pinned SHA-256, unreachable by config, genome or environment), a
  file-backed kill switch, and a build that sends no real orders. Try to break that
  claim: a config key, an environment variable, a code path, a default that leaks.
  A reproducible counterexample is the most valuable thing you can send.
* **Where does this README over-claim?** The numbers in it are deliberately
  unflattering (the holdout rejected the champion), but honesty is easy to get
  wrong in the direction of flattering the author. Point at a table, a metric or a
  sentence that does not hold up.
* **Is the reporting honest or self-serving?** Out-of-sample discipline, cost
  modelling, drawdown, the difference between in-sample and holdout figures. Where
  would you demand more evidence before believing a result?
* **Does the evolution loop ask the right question?** The gates, the fitness
  metric, the walk-forward folds, the lessons it stores. What is missing, what is
  wasted effort, what would you replace?
* **Can you use the dashboard?** The wording, the panels, the tooltips on hover,
  the settings page, the things it calls `N/D`.
* **Can you reproduce a run?** From a report in `reports/`, or from the
  reproducibility block, can you re-run it and get the same numbers?
* **What broke or cost you time?** Installation, the first run, a command whose
  error message told you nothing. Rough edges are worth reporting.

Ways to give it: open a GitHub **[issue](issues/new)** (bugs, contradictions, security concerns)
or a **[discussion](discussions)** (design, direction, "why did you do it this way"). In an issue,
please include the command you ran, what you expected, what happened, and the
output — `python -m selftrade doctor` and `python -m selftrade risk verify` are
good starting points because they print the state the program actually sees.

What this is **not**: financial advice, a promise of profit, or a signal service.
Questions of the form "will this make money" are the one kind this project cannot
answer, and the README says why: the honest out-of-sample result on the default
data is a loss.

If you want to point other people at it, [docs/LAUNCH.md](docs/LAUNCH.md) is the launch kit: the
claims that have a command behind them (and the four that must never be made), the rules of the
communities worth posting to, paste-ready drafts, and what to do with the replies.

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\run.ps1 doctor
.\run.ps1 data --fetch --rows 18000            # real public klines -> data/BTCUSDT_4h.csv
.\run.ps1 evolve --generations 12 --no-llm
.\run.ps1 report
.\run.ps1 hall
.\run.ps1 invest --champion --window holdout   # what if we had really invested
.\paper.ps1 -Create ; .\paper.ps1 -All
.\dashboard.ps1
# LAN (phone on the same Wi-Fi): the login is mandatory
.\dashboard.ps1 -BindHost 0.0.0.0
```

## What it does

1. **Data** — real public klines (keyless, `data --fetch`), CSV, freqtrade JSON,
   synthetic, or MetaTrader 5 (validated to one strict OHLCV contract).
2. **Hypotheses** — a local LLM (Ollama) proposes bounded genome changes when
   available; otherwise deterministic mutation. The LLM never writes trading
   logic, and its output is clamped by `validate_genome`.
3. **Evaluation** — bar-by-bar backtest with next-open fills, two-sided costs,
   intrabar stops (stop before target, gap fills), then walk-forward with the
   metric measured on the test slice only.
4. **Gates** — `n_trades >= min_trades` **and** `max_drawdown <= max_drawdown`.
   Failures are recorded, not hidden.
5. **Holdout** — the tail of the series is read **once per run**, at the end,
   with a regime-luck warning when it is far better than the folds.
6. **Memory** — SQLite: runs, candidates, hall of fame, lessons (+embeddings),
   paper sessions/trades/equity, the risk/API logs, and `strategy_versions` — the
   recorded parent → candidate lineage with its diff, motivation and outcome. Every
   run also stores its *provenance*: the commit (and whether the worktree was dirty),
   the dataset digest and the resulting `repro_id`, so a fitness can be traced back to
   the code and the prices that produced it — see `docs/DASHBOARD.md`.
7. **Paper** — forward trading of the champion on an out-of-sample window: the
   strategy's signals become **orders** that pass the pre-trade gate and fill on the
   simulated rail (same mechanics as the backtester — next-open fills, two-sided
   costs, the exit plan checked intrabar), restartable through a persistent cursor,
   sized at the sealed cap and stopped by the kill switch — see
   `docs/PAPER-TRADING.md` — and `docs/CAPITAL.md` for the account that session trades
   and who is allowed to credit it.
8. **Autopsy + critic** — losing trades are dissected (worst losses with their
   entry context, clustered failure modes, overfit caveats) and written to the
   memory as a lesson; a **critic** refuses to deploy anything that breaks the
   **sealed risk limits** (`frozen` dataclass, SHA-256 pinned, no config key, no
   genome and no env var can move them) — see `docs/AUTOPSY-AND-CRITIC.md`.
9. **Stress** — two questions the single realized run cannot answer: how much of
   the result is the **draw** (Monte Carlo resampling of the trade sequence:
   bootstrap / shuffle / block, reproducible percentiles for a fixed seed) and how
   much is the **numbers** (every parameter moved by ±δ through the engine, with the
   clamps reported and the degradation measured) — see `docs/STRESS.md`.
10. **Order-level execution** — an abstract `BrokerAdapter` with a **simulated
   broker** that fills at the next open with two-sided costs, arms hard stops
   (gap included) and reconciles its fills against its own ledger; a
   deterministic **pre-trade gate** refuses an order before it exists on the
   sealed limits (5% per position, 2% daily halt, 15% drawdown) plus
   configurable exposure, leverage and order frequency; refusals are recorded in
   the `risk_events` table and an account-level breach **engages the kill
   switch** — see `docs/EXECUTION-AND-RISK-GATE.md`.
11. **Dashboard** — a login, a **live Binance chart** (public keyless data: real moving candles, not a static curve), a **Binance key panel** (the read-only `binance check` report — permissions, clock, balances, open orders — refreshed on a timer), an **emergency stop** (the kill switch itself, armed or disarmed from the page behind an explicit confirmation, with the state and its audit trail always on screen), a **risk-limits panel** (drawdown, today's P&L and exposure against the sealed caps, with the room left before each one trips, measured by the gate's own limit functions and read-only end to end), an **alerts card** (what the alert channel actually sent, newest first, with a halt the *program* armed marked as `HALT (automatic)`), an **AI Lab** (the Collect → … → Learn cycle, the run's reproducibility record, the navigable **strategy version history** with each stored diff, and the per-experiment table) and the bot panels, with start/pause/resume/stop — where `start` **launches the tracked loop** and the buttons refuse to steer one that is not running; loopback by default, openable on the LAN **only** with the login — see `docs/DASHBOARD.md`.
12. **MT5 (optional)** — data, real spread→bps cost conversion, and orders behind
    a two-key gate with a dry run by default.
13. **Binance (optional)** — a real `BrokerAdapter` against the documented Spot
    API: HMAC-signed requests, `exchangeInfo` filters, idempotent `clientOrderId`,
    weight and `Retry-After` guards, and **every call and error recorded** in
    `api_calls`. The **Testnet** by default (a funded host is a config error); a
    production account is a deliberate opt-in (`binance.allow_production: true`)
    behind the same two-key arming gate, and every order that is about to be sent
    passes the **sealed `RiskGate`** and the kill switch first. Dry-run until
    armed. An order that asks for a stop (`--stop-pct`) is **protected**: the stop is
    validated against the exchange filters **before** the entry is sent, placed as a
    resting `SELL STOP_LOSS_LIMIT` the moment the fill lands, sized from the free
    balance and priced off the average fill, and reported as `UNPROTECTED` (with a
    recorded alert) if it cannot be placed — so `require_stop: true` is satisfiable
    on a real Spot order. A new key starts at `binance check`, a **read-only** probe
    that reads the clock, the signed account, the permissions, the balance and the
    open orders and prints how many `POST` requests it made — **see
    `docs/BINANCE-TESTNET.md`**.
14. **Risk alerts (optional)** — the gate can halt trading on its own, so the halt has to
    travel: with `notify.enabled: true` and two environment variables (only their
    **names** live in the config, and a value is never printed), Telegram carries the
    **halt** (the switch engaged, with the numbers that explain it - and, when the
    **program** armed it rather than you, a headline that says so and the drawdown, the
    day's loss and the margin that was left at the refusal) and the **warning before
    it** — a halting limit close to its cap, **or already past it with nothing
    stopped yet**, the breach that no other part of the program reports because the gate
    only sees it when an order is checked. Each halt is announced once (its signature is
    the switch's own state), each warning once per band and approach, the ledger is
    written **only after a message actually went out** (a failure stays pending and is
    retried), and a halt supersedes the bands while it is engaged. It runs inside every
    `paper` tick, as a standalone `notify watch` supervisor, and on the dashboard's
    risk-limits card (`— N waiting to be sent`) — see `docs/NOTIFICATIONS.md`.
15. **Capital account** — the simulated money the agents trade, as an *append-only
    ledger* instead of an editable number: `capital deposit|withdraw|allocate|release`
    (each with a **reason**, a source and the balance it left behind), `paper --create
    --fund N` to hand capital to a session, and a *Capital account* card on the
    dashboard's Trading page with the balance, what is allocated, and the whole ledger.
    **Nothing is real and nothing is sent**: the ledger has no counterpart on any
    exchange, the real rails do not read it, and — the point of the design — **no agent
    can credit itself**: only the CLI, `paper --fund` and the confirm-gated dashboard
    POST can move capital, while everything else in the package has no route to it. The
    sealed 5% per position and 15% drawdown still bind on funded capital, so five times
    the capital takes the same trades five times as large — see `docs/CAPITAL.md`.

## Commands

| Command | What it does |
| --- | --- |
| `doctor` | diagnoses python/deps, data, Ollama, REA, MT5, memory — never fails on a missing subsystem |
| `data [--rows N] [--fetch] [--fetch-mt5] [--out F]` | real public klines, synthetic data, or an MT5 snapshot |
| `backtest [--family F \| --champion] [--holdout]` | one genome, folds, fitness, gate, optional holdout |
| `evolve [--generations N] [--no-llm] [--no-rea] [--session] [--note T] [--quiet]` | the search loop |
| `report [--json]` | last run |
| `hall [--limit N] [--min-fitness X]` | hall of fame |
| `export [--champion] [--out DIR] [--name C] [--timeframe TF]` | freqtrade strategy file |
| `rea plan\|run\|doctor [--target N] [--live]` | REA evidence (optional) |
| `mcp [--out .mcp.json]` | MCP config for REA + Ruflo |
| `mt5 doctor\|symbols\|bars\|account\|positions\|costs\|order\|close` | Trading.com bridge |
| `invest [--champion \| --family F] [--window W] [--initial-cash C] [--out F] [--json]` | money simulation on real prices: equity vs buy & hold, cost drag, SVG charts |
| `autopsy [--champion \| --family F] [--window W] [--worst N] [--out F] [--json]` | post-mortem of the losers: worst losses with entry context, clustered failure modes, and the lesson stored in the memory |
| `critic [--champion \| --family F] [--window W] [--json]` | judge deployment against the sealed risk limits; exit 1 = refused, and `paper --create` refuses too |
| `stress [--champion \| --family F] [--window W] [--paths N] [--seed S] [--method M] [--block N] [--deltas L] [--no-risk] [--out F] [--json]` | Monte Carlo over the closed trades (reproducible for a seed) plus ±δ parameter sensitivity, and the measured degradation |
| `lock-stress [--rounds N] [--writers N] [--cycles N] [--race-share F] [--replace-share F] [--seed S] [--budget S] [--legacy] [--compare] [--out F] [--json]` | the concurrency soak for the session lock: writers heartbeat the file while an operator writes `stop`, with the Windows lost create race injected — and reports requests overwritten and writes performed **beside** the lock. `--compare` runs the pre-fix reading too and passes only when the shipped lock is clean **and** the broken one is not (exit **2** a regression, **3** inconclusive). Works in a temporary directory and never touches `state/session.json` |
| `risk show\|verify\|seal` | the read-only risk layer and its SHA-256 seal |
| `risk limits [--json]` | the sealed caps against the live paper session, from a terminal: the same measurement the dashboard renders, plus any alert that has not been sent yet. Exit **0** within the limits, **1** a halting limit is breached (the next order arms the stop), **2** nothing to measure |
| `notify status\|test\|watch\|reset [--interval S] [--once] [--json]` | the risk alert channel: who would be told and what is pending (`status`, sends nothing), one proof message (`test`), the supervisor that sends on transitions only (`watch`), and forgetting what was announced (`reset`) |
| `broker [--bars N] [--entry auto\|first] [--cash C] [--symbol S] [--json]` | drives the simulated broker through the gate on local data: a blocked oversized order, a compliant fill at the next open, the hard stop exiting, and the ledger reconciliation |
| `switch show\|arm\|reset [--reason T]` | the kill switch: blocks every new order, persists across restarts, and fails closed if its state file is corrupt. `show` prints JSON, including the `detail` the gate stores when **it** armed the stop: the account and the two halting limits with the margin that was left at the refusal |
| `binance check\|doctor\|account\|positions\|bars\|exchange\|history\|open\|order\|protect\|unprotect\|cancel\|calls [--qty Q] [--side S] [--type T] [--price P] [--stop-pct P] [--client-id C] [--live] [--json]` | the Binance rail: Spot Testnet unless `binance.allow_production: true`; dry run unless `binance.live` **and** `--live`; every sent order passes the sealed gate + kill switch, with recorded requests and errors. `--stop-pct -0.02` makes the rail place a resting `SELL STOP_LOSS_LIMIT` right after the fill (validated before the entry goes out, alerted as `UNPROTECTED` if it cannot be placed); `protect`/`unprotect` add or remove that stop for a position, both keys required; `check` is the read-only start for a new key, with the POST count printed (always 0) and `--live` ignored |
| `paper [--create] [--run] [--all] [--reset] [--status] [--session ID] [--champion] [--fund N] [--release] [--reason T] [--json]` | forward paper trading as orders through the risk gate, on the simulated rail; `--fund` allocates capital from the ledger into the new/active session **before** it trades, `--release` returns what an unfunded session holds |
| `capital [status\|deposit\|withdraw\|allocate\|release\|ledger\|policy] [AMOUNT] [--reason T] [--currency C] [--session ID] [--limit N] [--json]` | the simulated capital account: `status` (free balance, what was credited, what is allocated, the session it funds), the movements (each with a required reason, refused when the policy cap, the free balance or a session that already traded says no), the append-only `ledger`, and the `policy` in force. Exit 1 on a refusal, and never a network call |
| `serve [--host H] [--port P]` | dashboard: password login (first login `0000`, changed under Settings) + live Binance chart + Binance key panel + emergency stop + risk-limits panel + alerts card + loop controls + bot panels (`--host 0.0.0.0` opens the LAN, and is refused while the bootstrap password is still the one in force; set a real password first) |
| `password status\|set\|reset [--value V] [--confirm] [--json]` | the dashboard login password without a dashboard: `status` says where it comes from (never what it is), `set` prompts twice, `reset --confirm` forgets it and goes back to the bootstrap `0000`. Only a salted PBKDF2 hash is ever written |
| `session status\|start\|pause\|resume\|stop [--generations N]` | the tracked loop: `start` **launches** `evolve --session` detached (pid printed, output to `state/loop.log`), `pause`/`resume`/`stop` steer it and are refused with exit 1 when nothing is running |

Every command prints readable output, supports `--json` where it makes sense,
and returns a non-zero exit code on refusal or error. Destructive commands
(`paper --reset`) require an explicit flag.

## Layout

```
config.yaml              all settings (typed + validated)
selftrade/               the package (see docs/ARCHITECTURE.md for the module map)
tests/                   one module per source module; stubs for Ollama/REA/MT5
docs/                    WINDOWS-SETUP, ARCHITECTURE, PAPER-TRADING, CAPITAL, DASHBOARD,
                         NOTIFICATIONS, INVESTMENT-SIM, AUTOPSY-AND-CRITIC, STRESS,
                         SESSION-LOCK, EXECUTION-AND-RISK-GATE, BINANCE-TESTNET,
                         LOOP-AI-PLAN, EXTENDING, REA-INTEGRATION, RUFLO-SWARM,
                         TRADING-COM-MT5
data/ state/ reports/    git-ignored runtime data
user_data/strategies/    exported freqtrade strategies
setup.ps1 run.ps1 paper.ps1 control.ps1 dashboard.ps1 mt5.ps1
```

## Configuration

`config.yaml` is fully typed. A key that does not exist — a typo such as
`backtest.fee_bp` — **stops execution** instead of silently changing the cost
model. Every value is overridable from the CLI:

```powershell
.\run.ps1 evolve --generations 3 --set evaluation.metric=sortino --set backtest.fee_bps=1.2
.\run.ps1 broker --set risk_gate.max_positions=3 --set risk_gate.min_seconds_between_orders=60
```

The `risk_gate:` section holds the pre-trade thresholds the user sets (risk per
trade, positions, leverage, order spacing, liquidity floor, stop required). The
three portfolio limits (5% per position, 2% daily halt, 15% drawdown) are **not**
there: they are sealed in `risk_rules.py` and reachable by no config key, no
`--set`, no genome and no environment variable.

The `funding:` section is the **simulated capital account** (`docs/CAPITAL.md`): the
currency credits are recorded in, the largest single credit (`max_deposit`), the
ceiling on the total credited (`max_total_capital`) and whether withdrawals are
allowed. It is operator policy, not the sealed layer - its job is to stop a typo from
funding a fantasy account - and the sealed 5% / 15% caps bind on funded capital too.

## Safety and secrets

- Passwords and tokens come **only** from environment variables
  (`SELFTD_MT5_PASSWORD`, `SELFTD_TELEGRAM_TOKEN`, …); the repo stores only the
  variable **names**. A test asserts a password never appears in any output. The one
  exception is the dashboard's own login password, which the operator changes from the
  dashboard: it is stored as a salted hash in `state/dashboard_auth.json`, and the API
  reports only where it comes from.
- MT5 orders need **both** `mt5.live: true` in config and `--live` on the CLI,
  plus `require_demo`, `require_stop`, `allowed_symbols`, `max_lots`,
  `max_positions` and `max_spread_points`. Real accounts are refused even when
  armed. The dry run never calls `order_send`.
- The dashboard requires a **password login** (`dashboard.auth`), mandatory for any
  non-loopback bind. The first login is the bootstrap `0000`, changed in Settings — or,
  when the password comes from an environment variable, the variable wins and the page
  refuses to change it. What is stored is one **salted PBKDF2 hash**
  (`dashboard.password_file`), never the password; changing it signs out every other
  session, and the session is a signed, HttpOnly, `SameSite=Strict` cookie. Loopback
  default, no remote assets, no telemetry, no third-party auth.
  A **network** bind is refused outright while the bootstrap password is still in force —
  stored, or set to `0000` through the environment: a login whose password is printed in
  this file steers the loop, arms the stop and moves capital, so `serve --host 0.0.0.0`
  says how to set a real one and opens nothing. Loopback keeps the bootstrap login, which
  is what it is for.
- `paper` sends **orders** — to the simulated rail only: no connection, no
  credential, no streaming, and `mode` stays `paper`. A refused order is recorded
  and not traded; `risk_gate.enabled: false` in the config is the only way off.
- `selftrade/execution/` never imports `requests` or `socket` (a test parses the
  package to enforce it) and `mode` is `paper`; the rails that talk to a provider
  (`binance.py`, `mt5.py`) live beside it, behind a two-key gate. The kill switch
  blocks new orders but does **not** close open positions - that is a documented
  operator procedure.
- **Binance keys are the user's to create** (GitHub login at
  https://testnet.binance.vision/ for the testnet). Until then no **signed**
  Binance request has ever been made here: the signature is verified against the
  documented HMAC recipe, not against a live `200`. No order has been placed or
  cancelled, on the testnet or anywhere else.
- A **funded** Binance account needs `binance.allow_production: true` **and** the
  exported keys **and** `--live`; a production URL alone is a config error, and a
  look-alike host is refused even with the flag. On every order that is about to be
  sent, the sealed `RiskGate` (5% per position, 2% daily halt, 15% drawdown) plus
  the kill switch run **inside the adapter**: a refusal is recorded in
  `risk_events` and nothing leaves the machine. `risk_gate.require_stop: true`
  refuses a stopless real order by design, and a Spot stop cannot ride along with the
  entry — so the rail places it itself right after the fill, once the stop it would
  need has been validated against the exchange's filters. The position is never
  opened without a place to stop out, and a stop that cannot be placed is `UNPROTECTED`
  and alerted, not silently skipped.
- REA analyses only targets that declare `authorized: true`.

## Verification status

**Verified in this environment (Windows 11 / Git Bash, Python 3.14.5).**

| Check | Result |
| --- | --- |
| `python -m pytest` | **785 pass, exit 0**, 32 test files. An earlier environmental failure in `tests/test_mt5.py` (the test expecting the "MetaTrader5 package is missing" error on a machine that has the package) does not reproduce in this run: `pytest -q tests/test_mt5.py` is green too, and the same suite passes in CI on Windows and Ubuntu. |
| `data --fetch --rows 18000` | 18 000 **real** BTC/USDT 4h candles from the public mirror, 2018-07-23 → 2026-10-10, close 7 733.74 → 82 819.29, written to `data/BTCUSDT_4h.csv` (50 s, 18 paged requests) |
| `evolve --generations 12 --no-llm` on that data | 120 candidates, 12 report generations; fold-mean Sharpe 0.886 for the champion, **holdout read #1 Sharpe −0.55 → the holdout rejected it** |
| `invest --champion --window holdout` | 10 000 → 8 456 USDT (−15.4%, CAGR −9.70%, DD 28.4%) vs buy & hold 8 608 USDT (−13.9%, DD 53.4%); 90 trades, 20% win rate, 2 472 USDT of costs; zero-cost counterfactual +9.3% |
| `invest --champion --window full` | in-sample (labelled as biased): 10 000 → 70 386 USDT vs buy & hold 107 466; 150 908 USDT of costs |
| investment charts | 4 inline-SVG charts (equity vs buy & hold, underwater, price with entries/exits, per-year bars) rendered in the preview panel from a single HTML file with no remote assets |
| `risk show` / `risk verify` | the sealed panel printed with its SHA-256; the seal file is written on first use and re-verified afterwards |
| `critic --champion` on the real data | **Refused** (exit 1): **"28.4% drawdown breaks the sealed 15.0% limit; the worst day lost 4.03%, past the sealed 2.0% daily halt. I will not deploy it."** |
| `autopsy --champion` on the real data | *"Loaded 90 trades - 72 losers (80%); the worst 5 took 1,162.35 out of the account. Dominant failure mode: entry in above-median volatility (24 of 72 losers, 33%)."* + the worst-5 table with RSI/200-day/ATR context |
| `stress --champion --paths 2000 --seed 7` on the real data (2.0 s) | the 90-trade holdout becomes p5 −35.50% / p50 −16.37% / p95 +14.81% (realized −15.44%); drawdown p95 37.35% vs realized 25.95% and P(drawdown over the sealed 15%) = 88.6%; the worst ±δ parameter move (`ema_slow`, −25%) takes the Sharpe from −0.545 to −0.893 (63.9% worse) with 0 gate breaks and 3 clamped perturbations |
| `stress` reproducibility | the same seed reproduces the percentiles, the probabilities **and** the path digest (`9e7e555f…`) bit for bit; a different seed changes it; a `--method shuffle` run holds the realized return exactly (p5 = p95 = −15.44% to ~1e-15) and only moves the path |
| `stress` test module | 31 tests: seed reproducibility, the shuffle identity, engine parity of the realized path, streaks, clamping, gate breaks and the alias for `metric: return` |
| `lock-stress` on Windows 11 (default) | 15 rounds × 3 writers × 3 stop-cycles: 968 writes, **365 create races injected** and 3 creates denied by the operating system itself, **0 stops overwritten and 0 writes beside the lock**, 9.7 s |
| `lock-stress --compare` | the lock as shipped **clean** (45 stop-checks) vs the pre-fix reading **defect** (**2 stops lost, 53 writes without the lock**, writers colliding on one temporary file) — *validated*. A pair where both halves come out clean is reported *inconclusive*, never as a pass; the race switched off entirely still survives, met and counted |
| `lockstress` test module | 16 tests, including the discriminant: with a real holder on another thread and the race injected at 100 %, the pre-fix reading is **counted** as writing beside the lock (1 acquisition — the holder's — and 1 unlocked write) while the shipped reading waits, takes the lock and loses nothing |
| `paper --create --champion` | refused by the critic (exit 1) with the reason on stderr; the refusal is recorded as a lesson |
| `broker --bars 600` on the real data | 50% order **rejected** on the sealed 5% cap (and the retry with the same `client_id` returned the same refusal); the 4.5% order filled at the next open (62 677.13, costs included) and the 2% hard stop exited it at bar 24 for −9.68; `reconcile: OK`; 1 risk event written |
| `switch arm` + `broker` | both orders refused ("kill switch is engaged"), 0 positions, 0 fills, `reconcile: OK`; `switch show` exits 1 while engaged |
| switch coverage (tests) | the state the gate keeps when it arms: `detail` holds the account and only the halting limits, a second trigger keeps the **first** halt's numbers, a reset forgets them, junk (nested dicts, 500-character strings, empty lists) is trimmed rather than stored, and a state file written before `detail` existed reads as "no numbers" |
| `binance doctor --set binance.enabled=true` | talked to the **real** Spot Testnet (`GET /api/v3/time` → ephemeral clock offset 2224 ms), mode `paper`, 0 credentials, 0 orders |
| `binance exchange` on the testnet | real `BTCUSDT` rules: `status TRADING`, stepSize 0.00001, tickSize 0.01, minNotional 5.0, order types incl. `STOP_LOSS`/`STOP_LOSS_LIMIT`, and the real `REQUEST_WEIGHT` limit 6000/min |
| `binance bars` on the testnet | real 4h klines, last close 82 821.78 (agreeing with the production mirror in `data/`) |
| `binance` refusals | disabled → exit 1 with "nothing was sent"; `--live` without either key → exit 1; a production `base_url` → config error (exit 3); no credentials → exit 1, nothing sent |
| `binance` production opt-in | `--set binance.base_url=https://api.binance.com/api` alone → **config error (exit 3)**; with `binance.allow_production=true` + `binance.enabled=true`, `binance doctor` reported `base_url https://api.binance.com/api`, `production True`, `armed False`, `mode paper`, `risk_gate True`, `kill_switch True` and reached the real production `GET /v3/time` (clock offset 1443 ms) — **read-only, no credentials, no order**; `--set …base_url=https://api-binance.example.com/api` → exit 3 "look-alike host" |
| `binance` arming gates | armed without keys → exit 1 **"refusing to arm … without credentials"**; `--live` with `binance.live: false` → exit 1 *"…(allow_production is on: this would be REAL money)"* |
| sealed gate on a sent order (tests, injected transport) | an armed order outside `allowed_symbols` → **REJECTED** with the reason, **zero `POST /v3/order`**, and a `risk_events` row carrying the checks; kill switch engaged → REJECTED, zero POST; unreadable account → REJECTED "cannot read the account"; a compliant armed order → **FILLED** with exactly one POST |
| protective stop on a real order (demo, real testnet filters, no keys) | `binance order --qty 0.001 --stop-pct -0.30 --set binance.enabled=true` → **refused before anything was signed**: **"the stop 58147.75000000 is further below the market (83068.22) than the documented askMultiplierDown 0.8 allows"** — the entry never went out, so no unprotected position can exist; `--stop-pct 0.02` → *"has to be negative"*; `--side sell --stop-pct -0.02` → **"only a BUY can be protected"** |
| protective stop coverage (tests, injected transport) | the stop is placed **after** the fill as `SELL STOP_LOSS_LIMIT` (`timeInForce=GTC`, `price == stopPrice`, `newOrderRespType=RESULT`), sized from the **free** balance (0.999 out of 1.0 filled) and priced off the **average fill**; a previously placed stop is found, never stacked twice; a fill that cannot be protected is alerted and noted `UNPROTECTED`; the algo ceiling is read from the exchange; the dry run records both legs and sends neither; `protect`/`unprotect` work across two processes; and the **default gate** (`require_stop: true`) now accepts an entry that carries its stop |
| `binance check` with a **bogus key** (real testnet, 1 signed GET, read-only) | the exchange answered for real: `-2014 API-key format invalid.` — the report named the failing step (`FAIL signature`), the code, the next step (**"the API key is malformed…"**) and **0 POST**; with no credentials exported it made **0 calls** (exit 1) and with the rail disabled it exits 1 with the hint to enable it |
| `binance check` coverage (tests, injected transport) | 16 tests: 4 GET / 0 POST with no secret in the report, a `-2015` refusal that stops the signed reads, missing credentials with zero calls, a withdrawable key warned, `canTrade=false` → `trade_ready False`, an unpriceable balance still printing the raw rows, a clock past `recvWindow`, the strict open-orders read, and the CLI's readable/JSON output and exit codes |
| sealed gate on a **CLI** order (tests, transport patched out) | `python -m selftrade binance order … --live` goes through the same gate: kill switch armed → exit 1, zero POST; default `risk_gate.require_stop` → exit 1 "no stop loss on the order"; `require_stop: false` → exit 0 with exactly one POST; `--live` without `binance.live: true` → exit 1 before a client exists |
| `evolve` report | now contains `## Critic` (verdict + checks + warnings) and `## Autopsy` (worst losses, failure modes, caveats) |
| `doctor` | python/deps, data (12 000 rows), Ollama (UP), REA (disabled), MT5 (disabled), memory — no fatal errors |
| `data --rows 12000` | wrote 12 000 bars, 2019-01-01 → 2020-05-14 (UTC) |
| `evolve --generations 2 --no-llm` | 2 generations, 12 candidates, report + JSON written; holdout read once; report says the proposals were deterministic |
| `evolve` **with live Ollama** (`qwen2.5-coder:1.5b`) | `llm=on`; a real `[llm]`-origin candidate beat the seed (0.102 vs 0.072); report reads `origins {'seed': 3, 'llm': 1}`; LLM-written lesson stored with a 768-dim embedding |
| `session` control (live run, 400 generations) | `pause` froze `generations_done` at 14 across repeated polls; `resume` advanced it to 22; `stop` closed at generation 31 **and wrote the report** |
| `report` / `hall` | last-run summary and 3 hall-of-fame champions printed |
| `export --champion` | freqtrade strategy file written; header declares the exit-mechanics difference |
| `paper --create --champion` | **refused** by the critic (exit 1) with the reason on stderr: 28.4% drawdown on the holdout — no hall genome passes on the real data, which is the deployment gate doing its job |
| `paper` (order-level session, real 3 600-bar holdout, step 168, 3.6 s) | 90 trades, −0.80%, cash 9 920.36; **identical to `backtest.simulate` at the same size** — same entry/exit prices, times, directions and exit reasons, equity within 1.3e-15 — and 0 refusals |
| `paper` with the kill switch armed | the whole window ran with **every** order refused ("kill switch is engaged"): 0 trades, equity flat at 10 000.00, each refusal recorded with `paper_session` and the order's `client_id` (`ps3-3576-entry`); after `switch reset` + `paper --reset` the same session trades again (90 trades) |
| `paper --set backtest.position_size=0.95` | unchanged (90 trades): the rail clamps to the sealed 5%; the manifest keeps `position_size` and `configured_position_size` apart |
| `paper` with a genome whose stop is 11% | every entry refused ("a 11.01% stop is outside the sealed hard stop of 5.0%"), 0 trades, refusals recorded — the same genome `paper --create` already refuses via the critic |
| dashboard login over HTTP (`curl`) | `/` → 200 `text/html` with the sign-in form; `/api/whoami` → `authenticated: false`; `/api/market` without a cookie → **401**; wrong password → 401; the right password → 200 with `Set-Cookie: st_session=…; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400`, and the protected endpoints then answer |
| dashboard live chart, real Binance data | `/api/market?symbol=BTC/USDT&interval=1m&bars=60` → `ok: true`, 60 real candles and a 24h ticker (last 83 033.19, high 83 080.98, quote volume 743 336 196); `ETH/USDT 5m` → 30 candles; a bad symbol → `ok: false` with the reason, no exception; the two upstream calls run in parallel, so a fresh refresh took **1.75 s** (2.6–4.9 s sequentially) |
| dashboard in the browser | the sign-in screen is shown, the password opens the dashboard, the chart draws 180 candles with volume and a live price line, and it **moves**: 83 036.31 → 83 039.72 with `fetched_at` advancing every poll; all API calls 200, no console errors |
| dashboard first login, in the browser | a fresh state dir accepts **`0000`** and nothing else; the Settings card banners "the bootstrap password is still in use"; after the change (current + new + repeat + confirmation) it reads `stored · changed …`, the banner is gone, `0000` is answered *wrong password*, the new one signs in, and the change **survived a server restart** |
| password change over HTTP (`curl`) | no confirmation → 400 `confirm_required`; wrong current → 401; 3 characters → 400 "at least 4"; the same as the current → 400; the real one → 200 with a fresh `Set-Cookie` and `sessions_signed_out: true`, and **every file untouched until the successful one** — the store held a salted hash (240 000 rounds) and not the password, which appears in no response |
| password precedence, in the browser | with `SELFTD_DASHBOARD_PASSWORD` set, the card reads "managed by the environment", the three fields are disabled, a change is refused with 400 (*nothing written*) and the console banner says the variable wins |
| `password` on the command line | `status` on a fresh store → `default`; `set --value 12` → exit 1; `set --value …` writes the hash and prints that the password itself is not stored; `reset` without `--confirm` → exit 1, with it → back to the bootstrap `0000` |
| dashboard over HTTP (`curl`) | every GET endpoint → 200 with data; `/` → 200 `text/html`; `POST /api/control` wrote the request into `state/session.json` |
| dashboard Binance key panel over HTTP (`curl`) | `/api/binance` → 200 with the whole report (`provider binance-testnet`, `host_is_testnet true`, `mode paper`, `armed false`, `read_only true`); a second call inside the 60 s window → `cached: true` (1.8 ms) and `?force=1` → `cached: false` (real re-probe); with the rail **disabled** → `verdict "not checked"`, **disabled in config** and the hint, **0 calls**; with the rail enabled and no keys exported → `problems found`, *no credentials*, **0 calls / 0 POST**; the CLI `binance check` printed the **same** report (one shared `key_health`) |
| dashboard Binance key panel in the browser | the panel draws the verdict chip (`not checked` muted / `problems found` red), the credentials as `MISSING`, the failing step, `0 call(s) (none) · 0 POST · read-only yes`, the rate budget and the hint that enables the rail; the only request it makes is `GET /api/binance` → 200, all other panels 200, **no console errors** |
| dashboard emergency stop over HTTP (`curl`, real server on loopback) | `GET /api/killswitch` → 200, `blocked: false`, path `.tmp-ks/kill_switch.json`, no file on disk; `POST` **without** `confirm` → **400** `confirm_required` and still no file; `POST {"action":"arm","reason":"halting until I read the news","confirm":true}` → 200 `blocked: true`, `triggered_by: dashboard`, `event_id: 1`, the trail in `history`; then **`python -m selftrade switch show`** (a different process) printed the same `blocked: true`, reason and `triggered_by: dashboard` (exit 1) — the browser flipped the switch the gate and the loop read; `POST {"action":"reset",…}` → `blocked: false` (`event_id: 2`) and `switch show` → exit 0 with `events: 2`; `risk_events` held both rows with kind `kill_switch` |
| dashboard emergency stop in the browser | before signing in both buttons are **disabled** (the state is unknown); after the login the card read `clear — orders allowed` with `arm` enabled and `disarm` disabled; **arm emergency stop** opened the confirmation (**"This blocks every new order (paper and Binance) until you disarm"**) with the reason field focused, and a reason + **confirm** sent `POST /api/killswitch` → 200 and turned the card red `ENGAGED — new orders are refused` (`blocked yes`, the reason, a new `trigger` row, `arm` disabled / `disarm` enabled); the server was then **restarted** on the same state file and the page showed `ENGAGED` again **before any click** (the switch outlives the process); **disarm** (reason + confirm) returned it to `clear — orders allowed` with `disarmed — recorded as risk event #4`; `GET /api/killswitch` → 200 on every panel refresh, one `POST` → 200, **no console errors** |
| emergency stop coverage (tests) | 13 tests: a clear state with no file and no audit row, `confirm` enforced (missing and a string `"true"`), arming **stopping the real `RiskGate`** and filing the `risk_events` row, disarming audited too, an unknown action refused with nothing written, a second click that cannot bury the first reason, **six simultaneous clicks losing no event** (5/5 failing with the panel's write lock removed), a reason collapsed and capped at 300 chars, a locked database that still arms the switch and says so (`event_id: null`, `event_error`), 401 without the cookie, a cross-origin POST refused with 403, and the page rendering the card |
| dashboard risk-limits panel over HTTP (`curl`, real server, the project's own paper session) | `GET /api/risk` → 200 against session #1 (`BTC/USDT-holdout`, 3 600 recorded points): equity 8 456.00 against a peak of 11 107.06, `drawdown_pct 23.87`, `would_arm: true`, `breached: ["max_drawdown"]`, `nearest` = `max_drawdown` with `headroom -8.87 pp` — the panel reported the **real** holdout drawdown, the same 28.4% the critic refused on that genome, and the same limits the gate enforces |
| dashboard risk-limits panel in the browser | the card read `RISK LIMITS — BREACHED — THE NEXT ORDER ARMS THE EMERGENCY STOP` with the row `max_drawdown (halts) \| 23,87% \| 15,00% sealed \| BREACHED: -8,87pp \| arms the kill switch`, `max_daily_loss` at `2,00pp` of room, `position_pct`/`leverage` `refuses the order`, the sealed digest `f145ca89d49d…` `pinned yes`, and the headline **room before the gate arms the stop: nearest is max_drawdown with 0.00 pp left**; **arming from the card above** (reason **"drawdown 23.9% past the sealed 15% on session #1"**) turned the stop red `ENGAGED` with `armed — recorded as risk event #14` while the limits panel **stayed** red — the breach is a property of the account, not of the switch; a second server on an **empty** database rendered `RISK LIMITS NOT MEASURABLE` with **"no paper session to measure"** and **no rows at all**; `GET /api/risk` → 200 on every panel refresh, **no console errors** |
| dashboard risk-limits panel, cross-process (`curl` + CLI) | after arming from the page, `python -m selftrade switch show` in **another process** printed `blocked: true`, `triggered_by: dashboard` and the drawdown reason (exit 1), and the demo database held the `kill_switch` audit row; the **real** `state/kill_switch.json` was untouched (the demo ran on `.tmp-risk/`), so a panel read wrote nothing anywhere |
| risk-limits panel coverage (tests) | 8 tests: the panel measures the recorded curve (peak, drawdown, day P&L, `equity - cash` exposure) with the right headroom and units, the caps reported are the **sealed** ones with their digest pinned, `arms` true for the two halting limits and false for the exposure caps, the panel's values **equal the gate's own checks** on the same account (and its breach makes a real `PaperBroker` arm the switch and reject the order), a profitable day *adds* room instead of eating the cap, a session with no curve measured at its initial cash, no session reported as unmeasurable with zero rows, a read that changes nothing (`risk_events` empty, no switch file), and 401 without the cookie |
| paper session invariant behind the exposure row (test) | `equity - cash` on the recorded session equals the position rebuilt from the **fill ledger** (`net_qty × last close`), on a session stopped one bar after an entry — the derivation the panel reads, pinned against the broker's own books |
| Binance key panel coverage (tests, injected probe) | 10 tests: `/api/binance` returns the check report, one probe per refresh window with `?force=1` bypassing the cache, a disabled rail probed with zero requests, missing credentials reported without a request, a probe that raises reported as `not checked`, 401 without the login cookie, and the page rendering the card; plus 4 tests on `key_health` itself (disabled rail, a healthy read-only probe with `armed false`/0 POST, no credentials, a production host still refused) |
| strategy versions and provenance (tests) | `tests/test_repro.py` (14): `repro_id` is deterministic, changes with either half and needs both; the code fingerprint reads a real commit and its short form, notices a **dirty** worktree, and reports itself as unavailable when git cannot answer; the dataset fingerprint hashes the **bytes** of a CSV (and counts rows without the header), falls back to a canonical frame hash when there is no file, and changes when either changes; a dirty tree emits the id but `reproducible: false` plus the note, and two fingerprints of the same commit+dataset agree. `tests/test_memory.py` (+9) covers the in-place migration of a **v1** database (its row survives and reads `N/D`), the stored provenance columns and the version rows (parent, diff, delta, refusals, relabelling a champion). `tests/test_agent.py` (+8): every candidate becomes one version numbered in evaluation order, generation-0 seeds have no parent, later versions name the elite they were carried from or mutated out of, the stored diff **agrees with the two stored genomes**, the champion is labelled once, `delta_fitness` is the step against the parent, the run stores its provenance, the same code+data share the id and a different dataset does not, and the report carries both |
| `evolve --generations 2 --no-llm` with versioning (real data, run #6) | 8 candidates → 8 rows in `strategy_versions`, `version` 1..8 in evaluation order; `repro 225d5ce23c183368 \| commit 1fecb34 (dirty) \| data 6f7bd0e4b9a4`; the champion (v1, fitness −0.274) is labelled `champion`, the two elite carry-overs keep their parent with an **empty** diff and `delta_fitness 0.0`, and the two deterministic mutations show `bb_period 54→61`, `bb_std 3.2430→3.1722` with Δ −2.64 and −0.66; the run row holds `commit_sha 1fecb34…`, `dataset_sha 6f7bd0e4…`, `dataset_rows 18000`, `reproducible 0` because the worktree is dirty — and the report carries the same record plus the version table |
| AI Lab version history in the browser (real server, real database) | the Reproducibility card reads `225d5ce23c183368`, `1fecb34 master worktree dirty`, `6f7bd0e4b9a4 · 18.000 rows`, `data\\BTCUSDT_4h.csv`, `8 versions recorded` and the dirty-tree note, with the chip `not reproducible from the commit` (never a green badge for a dirty tree); the Strategy versions table lists all 8 rows (`8 recorded · champion v1`), sorted by version, with `outcome` chips, `vs parent` deltas and the compact diff; searching `bb_std` narrows it to the one version that touched that parameter, `deterministic` to 2, `champion` to 1, and the `change: changed params` filter to 2; expanding a row shows the diff table (`bb_period 54→61`, `bb_std 3.2430→3.1722`), the parent/child genome hashes, the parent fitness, the gate reason, the timestamp, the hypothesis and both genomes as JSON; a root seed reads `root genome — nothing to diff`; the run selector labels each run with its `<repro_id>` |
| MT5 with a fake terminal (real CLI) | `costs` → 20.0 points = 1.818 bps + suggested fee/slippage; dry run built the full request with **0** `order_send` calls; `--live` alone still 0; both keys → exactly **1** send; `doctor` connected |
| MT5 offline suite | 39 tests: data/symbols/costs/dry run/two-key gate/real-account refusal/sizing/password hygiene |
| CLI suite | 20 tests: parsing, `--config`/`--set` from any position, exit codes, every command handler |
| `risk limits` against the project's own state | the **real** holdout session: `BREACHED: max_drawdown - the next order is refused and arms the kill switch`, `max_drawdown 23.87% of the 15.00% cap (room -8.87pp)`, the sealed caps and digest, the session and its 3 600 points, **exit 1**; and `PENDING [warning] [past] max_drawdown is past its cap: the next order arms the stop` — the breach the gate has not seen yet, printed even with the alert channel off |
| risk alert channel, live against the real state (transport swapped for a local server) | round 1 sent the `past`-band warning for the 23.87% drawdown and wrote the ledger; round 2 sent **nothing** (same band); round 3 armed the stop on a daily loss with the transport answering **500** → `sent=False, recorded=False`, the ledger still had no `halt` key; round 4 delivered the halt that had never gone out and round 5 did not repeat it; round 6 (stop disarmed, account still past the cap) sent nothing, because the breach had already been reported — 3 requests for 6 rounds, 0 duplicates |
| `notify test` / `notify watch --once` against the **real** Telegram API, bogus token | a real HTTPS request left the machine: `notify test: NOT sent (telegram returned status 401)` and `[halt] NOT sent: telegram returned status 401 (…)`, both **exit 1**; `notify_state.json` was **not** created, so the undelivered halt stayed pending instead of being marked as told |
| `notify status` with the channel off and a breach on the books | reported `NOT ready: notify.disabled`, the two variable **names**, the switch state, and `PENDING [warning] … would be dropped: set notify.enabled=true and export both variables` — the notice is shown even when nothing can carry it, and `notify watch` refuses to start (`exit 1`) rather than supervising a dead channel |
| the paper tick on an automatic halt (tests, real `RiskWatch`, transport replaced) | the rail that stopped trading reports it: one notice of kind `halt`, and the message opens **"selftrade HALT (automatic) - the gate stopped trading, not you"**, quotes *"drawdown 23.87% of the 15.00% cap - 8.87pp past it"* and **"today's loss 0.40% of the 2.00% cap - +1.60pp of margin left"**, and still carries the live measurement of the session |
| an automatic halt, from the real arming path (a `PaperBroker` whose account is 23.87% down, the gate, the real switch file) | the order came back `rejected` (*"blocked: account drawdown 23.87% breaks the sealed 15.0% limit: trading halts"*), `switch.is_blocked()` became true with `triggered_by: risk_gate`, and the alert that followed opened with **"selftrade HALT (automatic) - the gate stopped trading, not you"** and carried **drawdown 23.87% of the 15.00% cap - 8.87pp past it** and **today's loss 0.40% of the 2.00% cap - +1.60pp of margin left**, then the live measurement of the real paper session |
| the alert history, in the dashboard and the terminal | `/api/alerts` on a scratch state with two real deliveries returned `sent 2` and, newest first, `halt`/`band: null`/`automatic: true` (headline **"selftrade HALT (automatic) - the gate stopped trading, not you"**) then `warning`/`band: "past"`/`reason max_drawdown`; the card rendered `2 sent (kept up to 50) · …notify_state.json` with the two rows, `channel off (notify.disabled)` and **no console errors**; `notify status` printed both `SENT` lines under the `PENDING`/kill-switch state, and `notify status --json` carried them under `sent` |
| risk alert channel coverage (tests) | 25 tests: the halt announced once, a new halt announced again, a **halt armed by the program** distinguished from one the operator armed (the first names the gate and quotes the drawdown, the day's loss and the margin that was left; the second claims nothing), an automatic halt without recorded numbers still naming the program, a warning once per band and re-armed after recovery, a `past` limit announced with nothing engaged and with `warn_below_frac: 0`, the halt superseding the bands, a `past` limit announced again after a disarm, a ledger written by an older version (or one that cannot be parsed) read instead of raising, a disabled channel reporting but never sending or remembering, a 500 staying pending and going out on the next round, an unreadable account not stopping the halt, a quiet round making no request and no write, a bounded history of what actually went out (a failed send is not in it, a `notify reset` keeps it, `SENT_LOG_LIMIT` stops it), and the token never leaving the process |

**Not verified here.**

- A **real** MT5 terminal / Trading.com account: the package is Windows-only and
  no terminal or credentials exist in this environment. All MT5 logic is
  verified against a fake terminal only.
- A **complete** LLM evolution loop was not run to completion: CPU inference is
  slow (~5 s/token on the 7B model, ~43 s for 8 tokens), so the first attempt was
  stopped by the operator and recorded as `aborted`, and the successful live run
  used the small 1.5B model with a bounded `llm.num_predict`. Set
  `llm.timeout`/`llm.num_predict` appropriately, or use a GPU, for large models.
- **freqtrade** was not installed; export parity is proven by executing the
  exported file's pure `compute_signals` and comparing it with the package.
- The **PowerShell scripts** (`dashboard.ps1` and friends) were not executed here
  (no `pwsh`); they are thin wrappers around the same commands, and the dashboard
  itself was driven in a real browser (login + live chart) as well as over HTTP.
- The dashboard was verified on **loopback**; the LAN bind is covered by tests
  (login mandatory, refusal without it), not by a second machine on the network.
- The Binance key panel met the **real** testnet only in its zero-request states
  (rail off, credentials missing) and, through the CLI, with a deliberately bogus
  key; an `ok` report with real permissions, balances and open orders needs your
  own testnet key - see `docs/BINANCE-TESTNET.md`.
- **No real Telegram message has ever been sent**: no bot token exists here, so the
  alert channel was verified in its zero-request states, against the real API with a
  **bogus** token (a real 401, reported and never raised), and through a local server
  with the transport injected — the delivered case is covered by tests, not by a
  message in a real chat. Only the two variables' **names** live in `config.yaml`.
- The emergency stop was driven in the browser against a **loopback** server whose
  state file (`--set session_file=…`) lived in a scratch directory; what it stops is
  the file the gate, the paper loop and the Binance rail read, verified through
  `switch show` in a second process and by a `RiskGate` test. Arming it is a
  **refusal**, not a way to close an open position.
- **REA** was not installed; the authorization gate, dry run and evidence
  storage are tested with an injected runner.

**Known limits.**

- The backtester does **not** model overnight swap/financing or broker contract
  specifications; weekend gaps must be present in the data for gap fills. The
  same is true of the simulated broker: no partial fills, no order book, no
  margin, and leverage is only *checked*, never financed.
- The paper session runs on the **order-level rail** in `selftrade/execution/`:
  its signals become orders, they pass the gate, and they fill in the simulated
  broker. The Binance adapter exists and is verified against the public testnet
  endpoints, but it is **not** wired into `paper`, and no real order has ever been
  sent anywhere.
- The paper session is sized at the **sealed** 5% per position, not at
  `backtest.position_size` (95%): an order above the cap can only ever be refused,
  so the session is clamped to pass it. The research backtester keeps the
  configured size, which is where paper and backtest stop being identical (they
  still agree trade for trade at the same size - `tests/test_execution_runner.py`).
  The dashboard's risk-limits panel measures that same paper session: with **no**
  session it reports `not measurable` instead of zeros, exposure is derived from
  `equity - cash` (the session keeps no positions table - pinned by
  `tests/test_paper.py` against the fill ledger), and the Binance rail is not
  measured there at all. A drawdown beyond the sealed cap is a **historical**
  reading: the panel says the next order arms the stop, it does not arm one itself.
  The alert channel reads that same measurement, so a breach can be announced while
  nothing has stopped yet - and one alert ledger belongs to one watcher: two
  supervisors on the same state directory are not coordinated.
- Each paper step re-runs the window prefix, so `paper --all` on the 3 600-bar
  holdout at the shipped `step_bars: 168` takes ~4 s here: the work is the sum of
  the prefixes, and the order path is ~10x the bar engine per pass.
- The Binance adapter has **no protective stop**: a Spot stop is a separate
  conditional order, so an order asking for `stop_loss_pct` is refused rather than
  faked. It also has no streaming, no partial-fill bookkeeping, and its position
  cost basis is the weighted average of the buys from `/api/v3/myTrades` (a spot
  balance carries no cost basis). Testnet balances and history are reset roughly
  monthly.
- Synthetic numbers are not market results.
- MT5 bar times are treated as UTC epochs; a broker server offset is not
  corrected.
- `evaluation.folds` folds are consecutive chunks; the train slice is context
  (indicator warm-up), not a fitted model — the strategies have no fitted
  parameters.
- `data --fetch` uses one public keyless klines endpoint (Binance's
  market-data mirror). There is no retry/backoff and no second exchange: if it
  is unreachable the command fails with a message instead of falling back to
  synthetic data.
- `data/*.csv` is git-ignored, so a fresh clone must run `data --fetch` (or
  `data --rows N`) before `evolve`; `doctor` reports the missing file.
- The `invest` simulation is **one asset, one genome, one history**: no
  survivorship handling, no cross-asset diversification, no walk-forward
  re-selection inside the window, and selection bias for any window that
  overlaps the bars the genome was chosen on (`--window full` says so in the
  report).
- On CPU-only Ollama, prefer a small model and a low `llm.num_predict`; a call
  that exceeds `llm.timeout` returns `None` and the run falls back to
  deterministic mutation (the report records the origin mix, so this is visible).

## License

**MIT** — see [LICENSE](LICENSE). Permissive on purpose: clone it, run it, try to
break it, and tell me what you found; that is what this round is for. The license
carries the usual warranty disclaimer, and the plain-words version stands too:
this is research software, it is not financial advice, and it promises no profit.
