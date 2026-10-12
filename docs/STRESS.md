# Stress: how much of a result is the draw, and how much the numbers

`critic` answers *"may this be deployed?"* and `autopsy` answers *"why did it
lose?"*.  Both read **one** realized run.  This module asks the two questions a
single run cannot answer:

* **path risk** — the trades happened in one order out of many.  Resample the
  sequence and the result changes even though the trades did not;
* **parameter risk** — every number in the genome is one sample from a plausible
  range.  Move each one by ±δ through the engine and measure what it costs.

Everything is measured, nothing is forecast.  The command is evidence for a
human, and it is deliberately **not** wired into `evolve`: 28 extra engine runs
per candidate would tax the search for a number the search cannot act on.  Run
it on a champion, on a family, or on a genome you are about to deploy.

```
python -m selftrade stress --champion --window holdout --paths 2000 --seed 7
```

## Path risk: Monte Carlo over the trade sequence

The only input is the ordered list of **closed** trades' `return_pct` (the
`end_of_data` mark is not an exit and is dropped).  Each path compounds those
returns onto the current equity — `equity *= 1 + return_pct` — which is exactly
what the engine does (its `return_pct` is `pnl / equity_before`), so the
realized path reproduces `backtest.simulate`'s final return to floating point.
A test asserts that identity rather than assuming it.

Three resamplers, and they answer different questions:

| `--method` | what it destroys | what it answers |
| --- | --- | --- |
| `bootstrap` (default) | the sample itself (draws with replacement) | "if these trades are representative of the *population*, what does the next sample look like?" |
| `shuffle` | only the order | "if the same trades had arrived in another order, how different is the path?" — its final return is **identical** to the realized one by construction, so it isolates path risk with zero sampling noise |
| `block` (`--block N`) | the order, in runs of N | the honest option when the sequence has streaks, because a bootstrap assumes trades are exchangeable |

Reported per path: `final_return`, `max_drawdown`, `max_losing_streak`,
`worst_trade_return`, as p5/p25/p50/p75/p95 **next to the realized value**, plus
the probabilities that matter against the sealed limits:

* `P(ending negative)`;
* `P(drawdown over the sealed 15%)`;
* `P(drawdown worse than the realized one)`;
* `P(at least 10 losses in a row)`.

### What this cannot say

* **No daily-loss probability from the paths.**  Resampling assumes trades are
  exchangeable, and the calendar is not: a bootstrap destroys which trades shared
  a day.  The *realized* worst calendar day is reported for reference instead, and
  `block` is the right tool when the sequence is streaky.
* It is not a forecast.  `P(drawdown > 15%) = 88.6%` is the share of **resampled
  paths** that would have breached the limit, not a promise about next year.
* Within a path, the engine's intrabar marks are not replayable from trades
  alone, so the path drawdown is the trade-level drawdown — slightly shallower
  than the bar-level number the critic reports (25.95% vs 28.4% on the real
  champion holdout).  Both are printed; neither is presented as the other.

## Parameter risk: ±δ sensitivity

Every numeric parameter of the genome is moved by each `--deltas` value in both
directions (relative for floats, `max(1, round(|v|·δ))` for ints), and the stop /
take-profit / trailing knobs are moved **in magnitude** (`stop_loss_pct` stays
negative by design).  Booleans and choices have nothing to shade and are skipped.

Each perturbation is validated (so the genome's own bounds apply), the engine is
re-run, and the objective is read before and after.  A perturbation whose shift
was **clamped** is reported with the value that actually ran, so the number in
the table is the number that was tested — not the one that was asked for.

The report gives, per parameter, the worst objective, the degradation as a share
of `|baseline|`, the drawdown, whether it breaks the evaluation gates, and
whether it is `fragile` (a ±δ move costing more than 25% of the objective).  A
perturbation is **not** a probability: nothing here says how likely ±10% is, only
what it costs.

`evaluation.metric: return` is read under its other name (`total_return`) through
an explicit alias; without it every perturbation would read `0.0` and be declared
harmless.  A test pins that.

## Reproducibility, stated exactly

`seed` seeds one `numpy.random.Generator` (`default_rng(seed)`) for the paths;
the sensitivity axis uses `default_rng(seed + 1)`, which is unused today (nothing
there is random) but is written down so a later change cannot silently renumber
it.  Same seed, same window, same genome → identical percentiles and
probabilities, and the report carries a **path digest** (SHA-256 of the
percentile table and the probabilities) so "reproducible" is a string you can
compare instead of a claim you have to trust.

```
digest seed 7 : 9e7e555fb60fed04390ac03f0474aa3ed7ccdd280d4d18b0dc2154a33316ec04
digest seed 7 : 9e7e555fb60fed04390ac03f0474aa3ed7ccdd280d4d18b0dc2154a33316ec04
digest seed 8 : c564ed005484b5c0fe560f94404ef494b0448bfbb8c3e50586d52545d73b60b8
```

## Output

Console, `--out report.md` (markdown), or `--json` (the full report, including
every perturbation row).  The lesson — the summary plus the path and parameter
findings — is stored in the SQLite memory with the tags `stress`,
`family:<F>`, `window:<W>`, exactly like the autopsy's.

Real data, the champion holdout (90 trades, 2.0 s):

```
Stress (holdout, trend_momentum, bootstrap, seed 7): realized -15.44% with a 25.95%
drawdown; resampling the same 90 trades puts the drawdown at p95 37.35%, and the
worst ±delta move of the parameters costs 63.9% of the sharpe
  path percentiles (resampled, realized in the last column):
    metric                p5       p25       p50       p75       p95   realized
    final_return        -0.3550  -0.2516  -0.1637  -0.0552   0.1481    -0.1544
    max_drawdown         0.1215   0.1857   0.2386   0.2979   0.3735     0.2595
  probabilities: P(final<0)=83.1% P(drawdown>15.0%)=88.6% P(streak>=10)=90.3%
  sensitivity: objective sharpe = -0.5450, 28 perturbations (3 clamped), 0 gate breaks
```

## Scope and limits, plainly

* Path risk is only as meaningful as the sample: 90 trades is a small draw, and
  the resampler says so by spreading widely.  A 10-trade window will spread
  almost everywhere and should be read as "unknown", not "risky".
* Sensitivity measures the **engine's** response at the given `--deltas`; it is
  not a gradient, and it does not search for the worst combination of two
  parameters moving together.
* Both axes run on the **deployment window** (`holdout` by default), which is the
  same window the critic judges.  Nothing here re-selects a genome or feeds back
  into `evolve`.
