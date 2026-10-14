# selftrade

[![tests](https://github.com/SalvoG1234/selftrade/actions/workflows/tests.yml/badge.svg?style=flat-square)](https://github.com/SalvoG1234/selftrade/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.13%20%7C%203.14-3776ab?style=flat-square)](.github/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-3da639?style=flat-square)](LICENSE)
[![live trading](https://img.shields.io/badge/live%20trading-never%20by%20default-8b1a1a?style=flat-square)](SECURITY.md)

**A self-improving trading-strategy search bot for Windows 11.** It loads historical OHLCV,
generates strategy hypotheses, evaluates them with realistic backtests and out-of-sample
walk-forward, promotes only what passes hard gates and an independent holdout, remembers
successes **and failures**, and papers the champion forward. An optional second rail can send
orders to a MetaTrader 5 demo account (Trading.com) behind a two-key gate.

> **It never trades real money by default and never promises profit.**
> It can run on real market prices (`data --fetch` pulls keyless public klines)
> and `invest` shows what a fixed amount of cash would have become. Those
> numbers are honest - including when they are bad - but they are still
> *historical*: they are not a forecast. Trading is at your own risk; this is not
> financial advice.

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

![The selftrade dashboard on a real run](docs/img/dashboard-overview.png)

*Screenshots in this README are real renders of a running server, not mock-ups. This one is the
Overview page of the project's own state: equity 8 456,00, a **23,87% drawdown against the sealed
15% cap**, 90 paper trades from the champion the holdout rejected, and the six-stage improvement
cycle that produced it. Everything on that screen comes from the database in `state/`.*

## Contents

| | |
| --- | --- |
| **[What the real data actually said](#what-the-real-data-actually-said)** | the out-of-sample result, nothing held back |
| **[Screenshots](#screenshots)** | the dashboard, page by page, and what each one is for |
| **[Quick start](#quick-start)** | a fresh clone to a running dashboard |
| **[What it does](#what-it-does)** · [Commands](#commands) · [Layout](#layout) · [Configuration](#configuration) | the program, the CLI and the files |
| **[Safety and secrets](#safety-and-secrets)** | what cannot happen, and how to check it yourself |
| **[Verification status](#verification-status)** | what was run, what was not, and the full ledger |
| **[The Social Manager](#the-social-manager)** | the second program in this repository: it promotes the project, under a gate |
| **[Documentation](#documentation)** | the docs, grouped by what you are trying to do |
| **[Contributing, security and support](#contributing-security-and-support)** | how to argue with this project, and where |

## Screenshots

Five pages, captured from a running server on `127.0.0.1` whose database is a copy of this
project's own `state/selftrade.db` (7 runs, 592 candidates, 1 paper session, 105 strategy
versions). No image here is a mock-up, a redrawing or a mock data set.

### Risk Center — the stop, and the caps it answers to

![Risk Center](docs/img/dashboard-risk-center.png)

The emergency stop is the only thing on the page that can *refuse* orders, and the limits panel
beside it is measured with the gate's own functions rather than re-derived for display. A breach
arms nothing by itself: it says the next order arms the stop.

### API keys — the one credential a config file cannot hold

![API keys](docs/img/dashboard-api-keys.png)

Where each rail credential comes from (an exported variable always wins over the local file), a
fingerprint instead of a value, and a confirmation-gated write in one place. Storing a key arms
nothing — see [SECURITY.md](SECURITY.md).

### Paper Trading — the rail, the session and the trade log

![Paper Trading](docs/img/dashboard-paper-trading.png)

The forward session trading the champion on the simulated rail, with the read-only Binance probe
that says whether a real key would work.

### AI Lab — the search, and whether its result reproduces

![AI Lab](docs/img/dashboard-ai-lab.png)

The Collect → Hypothesize → Backtest → Validate → Paper-trade → Learn cycle, the reproducibility
record, the critic's verdict and the version history with each stored diff.

### Market — public prices, and nothing else

![Market](docs/img/dashboard-market.png)

Keyless public candles: no account, no key, no order. The page says so, and the CI-visible code
behind it is a single read-only endpoint.

More pages, and what each one deliberately does not show: [`docs/DASHBOARD.md`](docs/DASHBOARD.md).

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
11. **Dashboard** — a login, a **live Binance chart** (public keyless data: real moving candles, not a static curve), a **Binance key panel** (the read-only `binance check` report — permissions, clock, balances, open orders — refreshed on a timer), an **API keys page** (the one credential the program cannot read from a config file: one tile per rail credential with the source **in force** and a SHA-256 fingerprint — never a value — plus a confirmation-gated form that stores it in the gitignored local key file, which the environment still wins over, and an audit row for every change), an **emergency stop** (the kill switch itself, armed or disarmed from the page behind an explicit confirmation, with the state and its audit trail always on screen), a **risk-limits panel** (drawdown, today's P&L and exposure against the sealed caps, with the room left before each one trips, measured by the gate's own limit functions and read-only end to end), an **alerts card** (what the alert channel actually sent, newest first, with a halt the *program* armed marked as `HALT (automatic)`), an **AI Lab** (the Collect → … → Learn cycle, the run's reproducibility record, the navigable **strategy version history** with each stored diff, and the per-experiment table) and the bot panels, with start/pause/resume/stop — where `start` **launches the tracked loop** and the buttons refuse to steer one that is not running; loopback by default, openable on the LAN **only** with the login — see `docs/DASHBOARD.md`.
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
| `verify --ledger [--run] [--write] [--force] [--json]` | the counts in this repository's own documentation, measured instead of typed: collects the suite (about two seconds) and requires every whole-suite count in the README, `CONTRIBUTING.md`, the ledger, the launch kit and the social card to equal what it measured — exit **0** agreeing, **1** drifted, **2** unmeasurable. `--write` repairs them from a real run (it collects first, because the suite contains this check, so a run started against stale documents would go red), `--run` executes the suite without writing. The research numbers (holdout Sharpe, drawdown, cost drag) are deliberately **not** generated: they cost minutes to reproduce and each row already names the command that produced it |
| `verify --card [--write] [--render] [--json]` | the one copy of that count a reader cannot check: `docs/img/social-preview.png` is a picture, and the renderer writes a record into the file itself - the count, the SHA-256 of `docs/img/social-preview.html`, the browser and the date. The check needs no browser: the count is held to the measured suite and the digest to the source as it is now, so repairing the documents and leaving the card alone fails the build instead of shipping an older number. `--write` draws it again (that mode needs a Chromium, named with `--chrome` or `$SELFTRADE_CHROME`); `--render` draws it in CI too and requires 1280x640 and today's count in the rendered text, printing the byte difference as information - a re-drawing is not reproducible even on one machine, so nothing enforces byte equality |
| `notify status\|test\|watch\|reset [--interval S] [--once] [--json]` | the risk alert channel: who would be told and what is pending (`status`, sends nothing), one proof message (`test`), the supervisor that sends on transitions only (`watch`), and forgetting what was announced (`reset`) |
| `broker [--bars N] [--entry auto\|first] [--cash C] [--symbol S] [--json]` | drives the simulated broker through the gate on local data: a blocked oversized order, a compliant fill at the next open, the hard stop exiting, and the ledger reconciliation |
| `switch show\|arm\|reset [--reason T]` | the kill switch: blocks every new order, persists across restarts, and fails closed if its state file is corrupt. `show` prints JSON, including the `detail` the gate stores when **it** armed the stop: the account and the two halting limits with the margin that was left at the refusal |
| `binance check\|doctor\|account\|positions\|bars\|exchange\|history\|open\|order\|protect\|unprotect\|cancel\|calls [--qty Q] [--side S] [--type T] [--price P] [--stop-pct P] [--client-id C] [--live] [--json]` | the Binance rail: Spot Testnet unless `binance.allow_production: true`; dry run unless `binance.live` **and** `--live`; every sent order passes the sealed gate + kill switch, with recorded requests and errors. `--stop-pct -0.02` makes the rail place a resting `SELL STOP_LOSS_LIMIT` right after the fill (validated before the entry goes out, alerted as `UNPROTECTED` if it cannot be placed); `protect`/`unprotect` add or remove that stop for a position, both keys required; `check` is the read-only start for a new key, with the POST count printed (always 0) and `--live` ignored |
| `paper [--create] [--run] [--all] [--reset] [--status] [--session ID] [--champion] [--fund N] [--release] [--reason T] [--json]` | forward paper trading as orders through the risk gate, on the simulated rail; `--fund` allocates capital from the ledger into the new/active session **before** it trades, `--release` returns what an unfunded session holds |
| `capital [status\|deposit\|withdraw\|allocate\|release\|ledger\|policy] [AMOUNT] [--reason T] [--currency C] [--session ID] [--limit N] [--json]` | the simulated capital account: `status` (free balance, what was credited, what is allocated, the session it funds), the movements (each with a required reason, refused when the policy cap, the free balance or a session that already traded says no), the append-only `ledger`, and the `policy` in force. Exit 1 on a refusal, and never a network call |
| `serve [--host H] [--port P]` | dashboard: password login (first login `0000`, changed under Settings) + live Binance chart + Binance key panel + API keys page + emergency stop + risk-limits panel + alerts card + loop controls + bot panels (`--host 0.0.0.0` opens the LAN, and is refused while the bootstrap password is still the one in force; set a real password first) |
| `password status\|set\|reset [--value V] [--confirm] [--json]` | the dashboard login password without a dashboard: `status` says where it comes from (never what it is), `set` prompts twice, `reset --confirm` forgets it and goes back to the bootstrap `0000`. Only a salted PBKDF2 hash is ever written |
| `session status\|start\|pause\|resume\|stop [--generations N]` | the tracked loop: `start` **launches** `evolve --session` detached (pid printed, output to `state/loop.log`), `pause`/`resume`/`stop` steer it and are refused with exit 1 when nothing is running |

Every command prints readable output, supports `--json` where it makes sense,
and returns a non-zero exit code on refusal or error. Destructive commands
(`paper --reset`) require an explicit flag.

## Layout

```
config.yaml              all settings (typed + validated)
selftrade/               the package (see docs/ARCHITECTURE.md for the module map)
socialmanager/           the social manager (docs/SOCIAL-MANAGER.md); its own config is social.yaml
tests/                   one module per source module; stubs for Ollama/REA/MT5
                         tests/test_social_*.py for the social manager, tests/fake_social.py the stubs
docs/                    WINDOWS-SETUP, ARCHITECTURE, PAPER-TRADING, CAPITAL, DASHBOARD,
                         NOTIFICATIONS, INVESTMENT-SIM, AUTOPSY-AND-CRITIC, STRESS,
                         SESSION-LOCK, EXECUTION-AND-RISK-GATE, BINANCE-TESTNET,
                         LOOP-AI-PLAN, EXTENDING, REA-INTEGRATION, RUFLO-SWARM,
                         TRADING-COM-MT5, SOCIAL-MANAGER, SOCIAL-CONNECTORS, SOCIAL-DEPLOY
data/ state/ reports/    git-ignored runtime data
user_data/strategies/    exported freqtrade strategies
setup.ps1 run.ps1 paper.ps1 control.ps1 dashboard.ps1 mt5.ps1
Dockerfile docker-compose.yml .env.example        the social manager's cloud half (docs/SOCIAL-DEPLOY.md)
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
- The **rail credentials** are the second exception, because a key pair has to be typed
  somewhere: the dashboard's **API keys** page stores one under the name the config
  already declares, in `binance.key_file` (`state/secrets.json`, gitignored), written
  atomically and owner-only. An exported variable of the same name **always wins** over
  it, no endpoint ever returns a value (only a source and a SHA-256 fingerprint), and the
  file is **clear text** — an API secret has to be sent to the exchange, so it cannot be
  hashed the way the login password is; the page says so in one sentence a reader can
  check against the code. Storing a key arms nothing: `binance.enabled`, `live` and
  `--live` still gate every order, and the sealed risk gate and the kill switch gate each
  one.
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

**Verified here:** Windows 11, Python 3.14.5 — and the same suite re-run from scratch on Python
3.13.14 in its own virtualenv. The
full ledger — every command with its actual output, including the unflattering ones — is in
[`docs/VERIFICATION.md`](docs/VERIFICATION.md). The short version:

| Check | Result |
| --- | --- |
| `python -m pytest` | <!-- generated: suite -->**1164 pass, exit 0** - 0 failed / 0 errors / 6 skipped, 48 test modules, 268 s, on Python 3.14.5<!-- /generated: suite --> |
| `evolve --generations 12 --no-llm` on 18 000 real bars | fold-mean Sharpe 0.886, then the holdout **rejected the champion** (Sharpe −0.55) |
| `critic --champion` | **Refused** (exit 1): *"28.4% drawdown breaks the sealed 15.0% limit … I will not deploy it."* |
| `risk verify` | exit 0, sealed digest pinned; no config key, genome or environment variable moves the caps |
| CI | the suite on **Windows + Ubuntu** for **Python 3.11, 3.13, 3.14**, plus a sealed-limits check and the session-lock soak |
| dashboard | real screenshots of a running server in [`docs/img/`](docs/img) — the same numbers the README quotes |

New rows land in the ledger, not here, and the two lists below are the part that has to stay on
the front page: what has **not** been proven, and what the program cannot do.


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

## The Social Manager

This repository also contains the program that promotes it: `socialmanager/`, a modular Python
system that reads the repository with the GitHub API, writes bilingual (IT/EN) drafts for GitHub, X
and Instagram, queues them in an editorial calendar, and publishes them **only** through a
deterministic gate. It is a separate program: it does not import the trading code, cannot place an
order, and cannot commit, comment on an issue or merge anything.

```powershell
python -m socialmanager init      # database + social.yaml (draft mode, connectors off)
python -m socialmanager doctor    # what is configured, what is missing, and why
python -m socialmanager sync      # read the repository (read-only)
python -m socialmanager produce   # write the bilingual drafts
python -m socialmanager serve     # the admin dashboard on http://127.0.0.1:8788
```

The defaults are the conservative ones: **draft mode** (nothing is published), every connector off,
replies and issue proposals off, the kill switch one command away, and no credential ever read
from — or written to — the repository. Publishing needs a connector enabled, a token you set, the
13 policy checks to pass, and a human approval for the seven cases that always need one (security,
controversy, financial claims, commitments, permissions, destructive actions, and any change to the
system's own autonomy limits).

**No live X or Instagram call has been made from this repository.** The connectors are written
against the current endpoints and covered by mocks, and they report `ready: false` and stay in
simulation until you configure them; the GitHub connector is the one that has run for real
(read-only, anonymously). The system never invents a metric, a star or a follower, and it never
presents a planned feature as shipped — the drafts in
[`docs/social-samples/`](docs/social-samples/drafts.md) carry their sources for exactly that reason.

Read [`docs/SOCIAL-MANAGER.md`](docs/SOCIAL-MANAGER.md) for the components, the policy checks and
the test report; [`docs/SOCIAL-CONNECTORS.md`](docs/SOCIAL-CONNECTORS.md) for per-platform
permissions, limits and prices; [`docs/SOCIAL-DEPLOY.md`](docs/SOCIAL-DEPLOY.md) for the hybrid
deployment (Docker, the always-on worker, the scheduled GitHub Action, backups and health checks)
and the list of credentials you must provide yourself.

## Documentation

`docs/` is grouped by **what you are trying to do**, not by file age.
[`docs/README.md`](docs/README.md) is the index; these are the entries worth knowing about.

| You want to… | Read |
| --- | --- |
| get it running from a fresh clone | [`docs/QUICKSTART.md`](docs/QUICKSTART.md) · [`docs/WINDOWS-SETUP.md`](docs/WINDOWS-SETUP.md) |
| understand how the search works | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/LOOP-AI-PLAN.md`](docs/LOOP-AI-PLAN.md) |
| see what stops an order | [`docs/EXECUTION-AND-RISK-GATE.md`](docs/EXECUTION-AND-RISK-GATE.md) · [`docs/AUTOPSY-AND-CRITIC.md`](docs/AUTOPSY-AND-CRITIC.md) |
| use the dashboard | [`docs/DASHBOARD.md`](docs/DASHBOARD.md) · [`docs/CAPITAL.md`](docs/CAPITAL.md) |
| judge a number honestly | [`docs/VERIFICATION.md`](docs/VERIFICATION.md) · [`docs/STRESS.md`](docs/STRESS.md) · [`docs/INVESTMENT-SIM.md`](docs/INVESTMENT-SIM.md) |
| paper-trade the champion forward | [`docs/PAPER-TRADING.md`](docs/PAPER-TRADING.md) · [`docs/SESSION-LOCK.md`](docs/SESSION-LOCK.md) |
| reach a real exchange or broker | [`docs/BINANCE-TESTNET.md`](docs/BINANCE-TESTNET.md) · [`docs/TRADING-COM-MT5.md`](docs/TRADING-COM-MT5.md) |
| route the risk alerts somewhere | [`docs/NOTIFICATIONS.md`](docs/NOTIFICATIONS.md) |
| add a strategy, a rail or a page | [`docs/EXTENDING.md`](docs/EXTENDING.md) |
| promote the project, and check what it will say | [`docs/SOCIAL-MANAGER.md`](docs/SOCIAL-MANAGER.md) · [`docs/SOCIAL-CONNECTORS.md`](docs/SOCIAL-CONNECTORS.md) · [`docs/SOCIAL-DEPLOY.md`](docs/SOCIAL-DEPLOY.md) |
| see where this is going | [`docs/ROADMAP.md`](docs/ROADMAP.md) · [`docs/LAUNCH.md`](docs/LAUNCH.md) |

## Contributing, security and support

Negative feedback is the point of publishing this at all: a reproducible counterexample is worth
more here than a feature. The three files below say how that works, and what this project will
not accept (live trading, a claim without a command, a secret in the tree).

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, the test conventions, and the rules a change has
  to respect.
- **[SECURITY.md](SECURITY.md)** — what counts as a vulnerability in a program that is supposed to
  be unable to send an order, and how to report one privately.
- **[SUPPORT.md](SUPPORT.md)** — which channel for which question, and the two commands that
  answer most of them (`doctor`, `risk verify`).
- **[CHANGELOG.md](CHANGELOG.md)** — what has landed, tied to the commit that carries it. No
  version has been tagged yet, and the file says so rather than inventing a release history.
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — the milestones, each with the check that would close
  it.
- Issues: [bug](https://github.com/SalvoG1234/selftrade/issues/new/choose) ·
  [feature](https://github.com/SalvoG1234/selftrade/issues/new/choose) ·
  [design feedback](https://github.com/SalvoG1234/selftrade/issues/new/choose) · questions:
  [discussions](https://github.com/SalvoG1234/selftrade/discussions)

## License

**MIT** — see [LICENSE](LICENSE). Permissive on purpose: clone it, run it, try to
break it, and tell me what you found; that is what this round is for. The license
carries the usual warranty disclaimer, and the plain-words version stands too:
this is research software, it is not financial advice, and it promises no profit.
