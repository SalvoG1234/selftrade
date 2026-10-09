# Paper trading

Paper trading simulates the **champion** strategy forward over an out-of-sample
window. It is a bar simulation: **no orders, no connections, no streaming.**

## Windows

Selected by `paper.window`:

| value | window |
| --- | --- |
| `holdout` (default) | the tail `evaluation.holdout_frac` of the series |
| `recent` | the last `paper.window_bars` bars |
| `full` | the whole series |

If the holdout is too small, `holdout` falls back to `recent` and the session
records `window=recent`.

## The cursor

Progress is a **persistent cursor** in `state/selftrade.db` (table
`paper_sessions`). Each run advances the cursor by `paper.step_bars` bars.
`paper.max_ticks` caps the number of steps per invocation (0 = unlimited).

## Why stepping cannot drift

The paper module reuses the *same* engine as the backtester
(`selftrade.backtest.simulate`). Each step re-simulates the window prefix
`[0:cursor]` and rewrites the session's derived rows idempotently. Because the
engine is causal, the prefix result never changes when the window grows, so:

- finishing the window in one shot,
- finishing it in many small steps, or
- completing it after a process restart

all yield **identical trades and an identical equity curve**. Tests assert this,
and a further test asserts that the paper result for a genome equals the
holdout metrics computed by evolution for the same genome.

## Commands

```powershell
.\paper.ps1 -Create          # create a session for the champion
.\paper.ps1 -Run             # advance one block
.\paper.ps1 -All             # run to the end of the window
.\paper.ps1 -Status
.\paper.ps1 -Reset           # destructive: requires this explicit flag
.\paper.ps1 -Status -Json
```

Equivalently: `python -m selftrade paper --create` … `--all`.

`--create` requires a champion in the hall of fame (`evolve` first).

## Reading the result

The summary shows cursor progress, cash, total return and trade count. The
dashboard shows the same plus the equity curve and the trade list. Numbers are
a simulation, not a market result.
