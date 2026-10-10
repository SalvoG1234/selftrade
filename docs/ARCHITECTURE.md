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
| `memory.py` | SQLite: runs, candidates, hall of fame, lessons, paper tables. |
| `llm.py` | Optional Ollama client (`/api/chat`, `/api/embed`). |
| `propose.py` | Next-move proposal: LLM when available, deterministic otherwise. |
| `agent.py` | The `Evolver` loop and the markdown/JSON report. |
| `freqtrade_export.py` | Export a self-contained freqtrade strategy. |
| `invest.py` | Money simulation on real prices: equity vs buy & hold, measured cost drag, per-year table, inline-SVG charts, md/HTML/JSON reports. |
| `paper.py` | Forward paper trading with a persistent cursor. |
| `session.py` | Persistent start / pause / resume / stop control. |
| `dashboard.py` | Loopback `http.server` dashboard + JSON API. |
| `mt5.py` | MetaTrader 5 / Trading.com bridge, injection-testable. |
| `rea.py` | Reverse-engineering evidence (optional) + MCP config. |
| `notify.py` | Optional Telegram notifications via environment variables. |
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
