# selftrade

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
   paper sessions/trades/equity.
7. **Paper** — forward simulation of the champion on an out-of-sample window,
   restartable through a persistent cursor, using the same engine as the
   backtester.
8. **Dashboard** — loopback HTML + JSON API with start/pause/resume/stop.
9. **MT5 (optional)** — data, real spread→bps cost conversion, and orders behind
   a two-key gate with a dry run by default.

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
| `paper [--create] [--run] [--all] [--reset] [--status] [--session ID] [--champion] [--json]` | forward paper trading |
| `serve [--host H] [--port P]` | local dashboard |
| `session status\|start\|pause\|resume\|stop [--generations N]` | control the tracked loop |

Every command prints readable output, supports `--json` where it makes sense,
and returns a non-zero exit code on refusal or error. Destructive commands
(`paper --reset`) require an explicit flag.

## Layout

```
config.yaml              all settings (typed + validated)
selftrade/               the package (see docs/ARCHITECTURE.md for the module map)
tests/                   one module per source module; stubs for Ollama/REA/MT5
docs/                    WINDOWS-SETUP, ARCHITECTURE, PAPER-TRADING, DASHBOARD,
                         INVESTMENT-SIM, EXTENDING, REA-INTEGRATION, RUFLO-SWARM,
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
```

## Safety and secrets

- Passwords and tokens come **only** from environment variables
  (`SELFTD_MT5_PASSWORD`, `SELFTD_TELEGRAM_TOKEN`, …); the repo stores only the
  variable *names*. A test asserts a password never appears in any output.
- MT5 orders need **both** `mt5.live: true` in config and `--live` on the CLI,
  plus `require_demo`, `require_stop`, `allowed_symbols`, `max_lots`,
  `max_positions` and `max_spread_points`. Real accounts are refused even when
  armed. The dry run never calls `order_send`.
- The dashboard binds loopback only and sends no telemetry.
- `paper` is a bar simulation: no orders, no connections, no streaming.
- REA analyses only targets that declare `authorized: true`.

## Verification status

**Verified in this environment (Windows 11 / Git Bash, Python 3.14.5).**

| Check | Result |
| --- | --- |
| `python -m pytest` | **284 passed** (one test module per source module) |
| `data --fetch --rows 18000` | 18 000 **real** BTC/USDT 4h candles from the public mirror, 2018-07-23 → 2026-10-10, close 7 733.74 → 82 819.29, written to `data/BTCUSDT_4h.csv` (50 s, 18 paged requests) |
| `evolve --generations 12 --no-llm` on that data | 120 candidates, 12 report generations; fold-mean Sharpe 0.886 for the champion, **holdout read #1 Sharpe −0.55 → the holdout rejected it** |
| `invest --champion --window holdout` | 10 000 → 8 456 USDT (−15.4%, CAGR −9.70%, DD 28.4%) vs buy & hold 8 608 USDT (−13.9%, DD 53.4%); 90 trades, 20% win rate, 2 472 USDT of costs; zero-cost counterfactual +9.3% |
| `invest --champion --window full` | in-sample (labelled as biased): 10 000 → 70 386 USDT vs buy & hold 107 466; 150 908 USDT of costs |
| investment charts | 4 inline-SVG charts (equity vs buy & hold, underwater, price with entries/exits, per-year bars) rendered in the preview panel from a single HTML file with no remote assets |
| `doctor` | python/deps, data (12 000 rows), Ollama (UP), REA (disabled), MT5 (disabled), memory — no fatal errors |
| `data --rows 12000` | wrote 12 000 bars, 2019-01-01 → 2020-05-14 (UTC) |
| `evolve --generations 2 --no-llm` | 2 generations, 12 candidates, report + JSON written; holdout read once; report says the proposals were deterministic |
| `evolve` **with live Ollama** (`qwen2.5-coder:1.5b`) | `llm=on`; a real `[llm]`-origin candidate beat the seed (0.102 vs 0.072); report reads `origins {'seed': 3, 'llm': 1}`; LLM-written lesson stored with a 768-dim embedding |
| `session` control (live run, 400 generations) | `pause` froze `generations_done` at 14 across repeated polls; `resume` advanced it to 22; `stop` closed at generation 31 **and wrote the report** |
| `report` / `hall` | last-run summary and 3 hall-of-fame champions printed |
| `export --champion` | freqtrade strategy file written; header declares the exit-mechanics difference |
| `paper --create/--run/--all` | 2400/2400 bars, 10 trades, +2.97%; a second session reproduced the evolution holdout exactly (25 trades, +7.88%) |
| dashboard over HTTP (`curl`) | all 10 GET endpoints → 200 with data; `/` → 200 `text/html`; `POST /api/control` wrote the request into `state/session.json` |
| MT5 with a fake terminal (real CLI) | `costs` → 20.0 points = 1.818 bps + suggested fee/slippage; dry run built the full request with **0** `order_send` calls; `--live` alone still 0; both keys → exactly **1** send; `doctor` connected |
| MT5 offline suite | 39 tests: data/symbols/costs/dry run/two-key gate/real-account refusal/sizing/password hygiene |
| CLI suite | 20 tests: parsing, `--config`/`--set` from any position, exit codes, every command handler |

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
- The **PowerShell scripts** and the **browser dashboard** were not executed
  here (no `pwsh`, no browser session); the dashboard is verified over HTTP with
  a real server and the scripts are thin wrappers around the same commands.
- **REA** was not installed; the authorization gate, dry run and evidence
  storage are tested with an injected runner.

**Known limits.**

- The backtester does **not** model overnight swap/financing or broker contract
  specifications; weekend gaps must be present in the data for gap fills.
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

See `LICENSE` if present; otherwise this is a personal research project.
Nothing here is financial advice.
