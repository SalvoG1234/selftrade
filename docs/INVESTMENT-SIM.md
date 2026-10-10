# Investment simulation (`invest`)

`invest` answers one question with numbers and charts:

> If we had put a fixed amount of cash behind this genome on these prices, what
> would the account have looked like — and what did it cost us?

```powershell
.\run.ps1 data --fetch --rows 18000                  # real public klines
.\run.ps1 evolve --generations 12 --no-llm           # find a champion
.\run.ps1 invest --champion --window holdout         # honest out-of-sample sim
.\run.ps1 invest --champion --window full --json     # machine-readable, in-sample
```

Options: `--champion | --family F`, `--window holdout|full|recent`,
`--initial-cash C`, `--out FILE.md` (the `.html` and `.json` are written next to
it), `--json`. Default output: `reports/invest_<UTC stamp>.md`.

## Windows

| Window | Bars used | Honest? |
| --- | --- | --- |
| `holdout` (default) | the tail `evaluation.holdout_frac` of the series | Yes — the evolution was forbidden to look at these bars and read them once, at the end |
| `full` | every bar available | No — the genome was *selected* on part of it; the report prints an IN-SAMPLE banner |
| `recent` | the last `paper.window_bars` bars | Depends — the report flags it in-sample whenever the window reaches back before the holdout boundary |

The in-sample flag is computed, not hard-coded: the window is compared with the
boundary where the holdout starts (`holdout_frac`). `in_sample: true` therefore
means "part of this window is the data the champion was picked on". A genome that
was only *sampled* (origin `sampled`) additionally gets a note saying the bias is
the window's, not a selection made here.

## What is measured, and how

The simulation calls `backtest.simulate` — the same engine the search optimised.
There is no second, more flattering accounting path.

* **Costs.** `backtest.fee_bps + backtest.slippage_bps` per side, charged against
  you on entry and on exit (the default config is 10 + 5 bps = 30 bps round trip).
* **Cost drag.** The same genome is run again with costs set to zero. The
  difference in final equity is what fees and slippage actually took out of the
  account — a measured number, not an estimate.
* **Cost sensitivity.** The run is repeated at 0×, 1×, 2× and 3× the configured
  costs. If the edge only exists at 0×, that is visible immediately instead of
  being implied.
* **Benchmark.** Buy & hold, on the same cash and the same per-side entry cost:
  buy at the first open, hold to the last close, no exit cost.
* **Risk.** Sharpe, Sortino, Calmar, max drawdown, time in market
  (`Σ bars_held / bars`), win rate, profit factor, average trade, per-calendar-year
  returns for both strategy and buy & hold.
* **Provenance.** Data file path and its **sha256**, symbol, timeframe, exact
  window, bar counts, first/last price and the genome JSON — so a number can be
  traced back to the file that produced it.

## Charts

Four inline-SVG charts are embedded in one standalone HTML file: no image files,
no CDN, no JavaScript.

1. equity curve, strategy vs buy & hold;
2. the underwater (drawdown) curve;
3. price with entry/exit markers;
4. per-year grouped bars, strategy vs buy & hold.

Series are downsampled to 480 points for the chart while the JSON keeps every bar.

## Real run, 2026-10-10 (the honest result)

Data: 18 000 BTC/USDT 4h candles, 2018-07-23 → 2026-10-10, fetched from the
public keyless klines mirror, `sha256 d74b8f9d…`. Costs 15 bps/side.
Search: 12 generations, 120 candidates, `--no-llm`. Champion: `trend_momentum`
(fold-mean Sharpe 0.886). **The untouched holdout rejected it** (Sharpe −0.545).

**Out-of-sample window: 2025-02-17 → 2026-10-10, 3 600 bars, 10 000 USDT.**

| | strategy | buy & hold |
| --- | ---: | ---: |
| final equity | 8 456.00 USDT | 8 608.42 USDT |
| total return | −15.4% | −13.9% |
| CAGR | −9.70% | −8.71% |
| max drawdown | 28.4% | 53.4% |
| Sharpe | −0.55 | — |

90 trades, 20.0% win rate, profit factor 0.73, 25.1% of bars in market,
**2 472.44 USDT paid in costs (24.7% of the starting cash)**.

Cost sensitivity on that window: 0× → 10 928.44 (+9.3%), 1× → 8 456.00 (−15.4%),
2× → 6 543.17 (−34.6%), 3× → 4 879.45 (−51.2%). The whole edge lived in the fees.

**In-sample window (`--window full`, 18 000 bars, 10 000 USDT):** strategy
70 386.30 USDT (+603.9%, CAGR +26.80%) against buy & hold 107 466.21 USDT
(+974.7%, CAGR +33.50%), with 150 907.58 USDT of costs. This window contains the
bars the genome was selected on, so it is optimistically biased — the report says
so in a red banner, and it is not a forecast.

What the two windows agree on: on BTC 4h, a fast trend-following overlay beat
holding on **risk** (roughly half the drawdown) and lost on **return**, once
realistic costs were charged. That is the number to carry forward, not the
in-sample one.

## Limits

* One asset, one genome, one history. No survivorship handling, no
  cross-asset diversification, no re-selection inside the window.
* Costs are a flat bps model; the backtester does not model funding, borrow, or
  market impact, and it fills at the next open (no order-book simulation).
* `paper` (forward, bar by bar, restartable) remains the next step after this
  simulation; `invest` is a re-run over history, not a live record.
* Past performance on historical data is not a forecast. Nothing here is
  financial advice.
