# Architecture

`selftrade` searches for trading strategies, evaluates them honestly, and
remembers what happened — good *and* bad. It never trades real money by default.

## Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | Typed, validated settings. Unknown keys stop execution. CLI overrides. |
| `data.py` | The OHLCV contract (`ensure_ohlcv`), real public-klines fetcher (`fetch_klines`, paged, keyless), synthetic generator, CSV / freqtrade / MT5 loaders. |
| `indicators.py` | Causal EMA, RSI, ATR, Bollinger, Donchian, realized vol. |
| `params.py` | Parameter spaces for the four families and the risk rules. |
| `genome.py` | The genome as pure data: validate/clamp, sample, mutate, crossover, complexity. |
| `strategies/` | `base.py` + the four families. |
| `backtest.py` | Bar-by-bar engine, fills, costs, intrabar risk, metrics. |
| `evaluate.py` | Walk-forward folds, fitness, hard gates, the read-once holdout. |
| `memory.py` | SQLite: runs, candidates, hall of fame, lessons, paper tables, risk events, the strategy lineage, and the append-only `cash_ledger` (schema v3). |
| `llm.py` | Optional Ollama client (`/api/chat`, `/api/embed`). |
| `propose.py` | Next-move proposal: LLM when available, deterministic otherwise. |
| `agent.py` | The `Evolver` loop and the markdown/JSON report. |
| `freqtrade_export.py` | Export a self-contained freqtrade strategy. |
| `risk_rules.py` | The sealed risk layer: frozen limits, SHA-256 pin, `MODEL_CAN_OVERRIDE = False`, reachable by no config key, genome or env var. |
| `critic.py` | The deployment verdict: hard checks against the sealed limits plus recorded warnings. |
| `ledger.py` | The machine-written half of `docs/VERIFICATION.md`: it measures the suite (a two-second `pytest --collect-only`, or a real run read back from the JUnit report) and holds every whole-suite count in the repository's documents to that measurement — the counts are written into marked fragments instead of typed, and the count a person forgot is caught wherever it lives. Deliberately does **not** generate the research numbers: those are outputs of `evolve`/`invest`/`stress`, and each row already names the command that produced it. |
| `autopsy.py` | Post-mortem of the losers: worst losses with causal entry context, clustered failure modes, overfit caveats, lesson for the memory. |
| `stress.py` | The other two questions about a single run: Monte Carlo over the closed trade sequence (`bootstrap`/`shuffle`/`block`, percentiles reproducible for a seed, path digest) and ±δ parameter sensitivity with the clamps reported and the degradation measured; report + lesson, never fed back into `evolve`. |
| `invest.py` | Money simulation on real prices: equity vs buy & hold, measured cost drag, per-year table, inline-SVG charts, md/HTML/JSON reports. |
| `paper.py` | Forward paper trading with a persistent cursor: the strategy's signals become **orders** on the execution rail, through the pre-trade gate, sized at the sealed cap. Reports whether its capital came from the capital ledger or the config (`funded_capital`, `capital_source`), and carries `--fund`/`--release`. |
| `capital.py` | The simulated capital account: the append-only ledger (`deposit`, `withdraw`, `allocate`, `release`), the policy caps from `funding:`, and the rules that make a credit auditable (a signed amount, a required reason, a recorded source and the balance it left behind). It moves capital **only** between the account and a paper session, only through the operator front ends, and imports nothing that can reach a network or an exchange. |
| `execution/` | The order-level rail: `base.py` (`BrokerAdapter`, orders, fills, positions, snapshots), `paper_broker.py` (next-open fills, two-sided costs, `quote_qty` sizing, the armed stop/target/trailing plan, ledger reconciliation), `risk_gate.py` (the deterministic pre-trade checks over the sealed limits), `kill_switch.py` (file-backed, fail-safe, persistent, and it keeps the margin the gate saw at the moment it armed), `runner.py` (the forward session that drives a genome through the broker - the path `paper` takes, and the reason the gate is on it). No network imports, `mode = "paper".` |
| `session.py` | Persistent start / pause / resume / stop control: the file the loop's `checkpoint` reads at every generation boundary. Two processes edit it (the loop's heartbeat, the operator's request), so each write is a read-modify-write under a cooperative lock file — a `stop` that arrived last must not be overwritten by the snapshot the loop read before it, and nothing here may wedge a run (a stale lock is broken, a slow holder is written past). |
| `lockstress.py` | The concurrency soak for `session.py`: several writers heartbeating one session file while an operator writes `stop`, with the Windows **lost create race** injected (only while another writer really holds the lock, so a writer that waits is never made to wait for a lock nobody owns) and the rename-side sharing violation injected too. Counts requests overwritten, writes performed beside the lock, writers overlapping on one temporary file, and creates the operating system denied by itself; `LegacyLockSession` carries the pre-fix reading as a control, so `compare` can pass only when the shipped lock is clean **and** the broken one is not. Never touches the operator's `state/session.json`. |
| `loop.py` | The tracked loop as a **process**: starts `python -m selftrade evolve --session` detached (its pid comes back, its output goes to `state/loop.log`), refuses to steer a loop that is not running instead of writing a request nobody reads, and probes a pid without ever signalling it. Shared by the dashboard's buttons and `session` on the command line. |
| `credentials.py` | The dashboard's login password, and the only writable secret in the project: one **salted PBKDF2-HMAC-SHA256** record (240 000 rounds, a fresh salt per change) in `state/dashboard_auth.json`, written atomically and owner-only. The bootstrap password is `0000` until someone changes it; an unreadable file fails **closed** (every password refused, with the recovery command in the `note`) rather than falling back to it. `describe()` publishes where the password comes from - never the hash, the salt or the password. |
| `dashboard.py` | `http.server` dashboard: password login (signed HttpOnly cookie, mandatory for a non-loopback bind; the password comes from an env var - which wins - or from `credentials.py`, changed by `POST /api/password` behind the current password, a confirmation and a signing-key rotation that signs every other session out), a live Binance chart (`/api/market`, public keyless candles + 24h stats, cached), the emergency stop, the risk-limits panel, the alerts history (`/api/alerts`), the capital account (`/api/capital`, confirm-gated), the bot panels and the loop controls (which **start and steer the tracked loop**, refusing a pause/stop while none runs). Protected API, same-origin POST checks, no remote assets. |
| `binance.py` | Binance **Spot Testnet** adapter implementing `BrokerAdapter`: HMAC-signed requests, `exchangeInfo` filters, idempotent client order ids, weight/`Retry-After` guards, every call and error recorded. Testnet-only, dry-run until armed, transport injectable. |
| `mt5.py` | MetaTrader 5 / Trading.com bridge, injection-testable. |
| `rea.py` | Reverse-engineering evidence (optional) + MCP config. |
| `risk_report.py` | One measurement of the account against the sealed caps, for every reader: the dashboard's *Risk limits* panel, `risk limits` in a terminal and the alert channel. Hands the reconstructed account to the gate's own `account_limits`/`exposure_limits`, so no reader can report a limit the gate enforces differently, and says which bands (`close`, `past`) each halting limit is in. Read-only. |
| `notify.py` | The optional risk alert channel (Telegram, env-var names only, never a value): *when* the operator hears about something, and never twice. Sends the **halt** (kill switch engaged, with the numbers) and the **warning** before it (a halting limit close to - or already past - its cap), keeps a ledger of what was actually delivered, retries what was not, and never raises. |
| `cli.py` | `python -m selftrade <command>`. |

## The data contract

Every loader funnels through `data.ensure_ohlcv`, which enforces a tz-aware UTC
`DatetimeIndex` and float64 `open, high, low, close, volume`. It **rejects**
empty frames, missing columns, non-temporal indexes, `high < low`,
`high < max(open, close)`, `low > min(open, close)` and fewer than
`data.min_rows` rows. Duplicates keep the last row; a missing volume becomes 0.
A median bar spacing much larger than the configured timeframe is a warning,
not an error. Because MT5 data goes through the same gate, the pipeline does
not know or care where prices come from.

## The genome

A genome is JSON data, never code:

```json
{"family": "rsi_reversion",
 "params": {"rsi_period": 14, "rsi_buy": 30, "rsi_exit": 60, "...": 0},
 "risk":   {"stop_loss_pct": -0.05, "take_profit_pct": null,
            "trailing_pct": null, "trade_shorts": false}}
```

`validate_genome` **clamps** every value to its range, applies per-family
constraints (`ema_fast < ema_slow`, `rsi_buy < rsi_exit`,
`exit_period <= entry_period`), and drops unknown keys. This is why an LLM
cannot emit an illegal genome.

**Complexity** = the number of family knobs set away from their default, plus
the number of enabled risk rules (take-profit, trailing, shorts). It enters the
fitness as a penalty, so the search prefers simpler strategies.

## The backtest engine

Rules, in order of what happens on a bar:

1. A signal is computed at the **close** of bar `i` and filled at the **open**
   of bar `i + 1`.
2. Costs (`fee_bps + slippage_bps`) are charged **on every side**, against you.
3. Stop / take-profit / trailing are evaluated **intrabar**, with the **stop
   firing before the target** when both are touched in the same bar.
4. A gap through the stop fills at the worse **open**, not at the stop level.
5. Position size is `position_size` of current equity.
6. Shorts require **both** `backtest.allow_short` and `risk.trade_shorts`.
7. A position still open on the last bar is marked `end_of_data` and is **not**
   a closed trade (so it does not inflate `n_trades`).

Metrics: `total_return`, `sharpe`, `sortino`, `calmar`, `max_drawdown`,
`n_trades`, `win_rate`, `profit_factor`, `avg_trade_return`. Sharpe/Sortino are
annualised with `TIMEFRAME_BARS_PER_YEAR[timeframe]`.

The engine does **not** model overnight swap/financing, and it does not model
broker-specific contract specifications.

## Walk-forward, fitness and gates

`make_folds` splits the development series into `evaluation.folds` consecutive
chunks; each chunk's first `train_frac` is development context and the rest is
the (subsequent) test window. The metric is measured on the **test** slice only.

```
fitness = mean(test_metric over folds)
          - complexity_penalty * complexity(genome)
          - INSTABILITY_WEIGHT * std(test_metric over folds)
```

`INSTABILITY_WEIGHT` is `0.5` (`selftrade/evaluate.py`).

**Hard gates** — a candidate must satisfy `n_trades >= evaluation.min_trades`
**and** `max_drawdown <= evaluation.max_drawdown`. A candidate that fails is
still recorded in `candidates` with the reason; it never reaches the hall of
fame.

**Holdout** — the tail `evaluation.holdout_frac` of the series is wrapped in
`Holdout`, which raises on a second read. It is read exactly once per run, at
the end. A test asserts zero reads during evolution and exactly one at the end.

**The buy & hold benchmark** — every evaluation also measures the market itself,
because a metric with no reference reads as skill when the asset simply rose.
`evaluate.baseline_metrics(df, settings)` runs
`invest.buy_hold_series(df, initial_cash, (fee_bps + slippage_bps) / 10_000)` on
**the same slice** the strategy was measured on — each fold's test window, and the
holdout — and returns the same metric set `compute_metrics` returns for a strategy
(no round trips: `n_trades = 0`), so the two columns are directly comparable:

```
baseline_mean  = mean(buy & hold metric over the same test slices)
metric_excess  = metric_mean - baseline_mean
```

Motivation: a positive out-of-sample Sharpe on an asset that doubled is not
evidence of an edge, and a search that cannot see the benchmark will happily
report one. `metric_excess` is the number to quote; `baseline_mean` is the number
it is measured against.

Limits, stated rather than implied: the benchmark is **one** long holding from the
first open to the last close of the slice, with the entry cost paid once and no
exit cost (nothing is sold), so it is not a strategy and not a competitor — the
fitness, the gates and the elite never read it. Its drawdown is measured within
its own recorded curve exactly as a strategy's is, so a flat market reads `0.0`
there and the entry cost shows up as a level, not as a fall. A test patches the
benchmark to nonsense and asserts every number the search uses is unchanged.

Where it is read: `selftrade backtest` prints it (`market (buy & hold)` and
`holdout market (buy & hold)`) and returns it in JSON as `baseline_folds`,
`baseline_mean`, `metric_excess` and `holdout.baseline`; the evolve report states
it in the holdout section. The other baseline already in the tree — the buy &
hold series in `selftrade invest`, with its cost-drag and cost-sensitivity tables —
answers a different question (what an investor's account would have looked like)
and lives in that report.

## The evolution loop

```
seed population -> evaluate (walk-forward) -> elite -> propose next
   -> journal every candidate -> end-of-generation lesson
   -> champion -> holdout (read once) -> report (md + json)
```

Elite candidates are carried into the next generation; the rest are proposals
from `propose.py`. Every candidate is written to `memory.candidates` with its
origin (`seed` / `deterministic` / `llm`), its per-fold metrics, its fitness and
its gate verdict. The run completes with Ollama off, and the report says so.

## Reproducibility

The RNG is seeded from `evolution.seed`. Given the same seed and no LLM, the
population, the metrics and the champion are identical. A test enforces this.
