# Autopsy, critic and the sealed risk layer

These three pieces come from watching a demo video of someone else's
"self-improving AI trading machine" and deciding that the *loop*, not the
marketing, was worth copying. What the video actually shows, frame by frame:

| Moment | On screen |
| --- | --- |
| 4 s | a terminal listing candidate rules with a score: `gen 05 -2.6% × rewrite - size × risk / str_pct`, `gen 07 +11.8% / keep × promote candidate to paper` |
| 12 s | the loop panel: pipeline **Rewrite → Backtest → Autopsy**, generation counter, "code rewrite 4", "best Sharpe 1.77", "last result +9.1%", a per-generation log |
| 20 s | the critic refusing: *"That fails: -66.5% drawdown breaks your -20% limit. I won't deploy it."* |
| 24 s | *"Loaded 214 trades - 126 losers. Found the 5 worst losses."* with RSI / 200-day / earnings-gap / ATR context, then *"clustering failure modes across all 126 losers"* |
| 32 s | `risk_rules.py`, **sealed**: `@dataclass(frozen=True)`, `MAX_DRAWDOWN_PCT = 15.0`, `MODEL_CAN_OVERRIDE = False`, *"sealing rules with SHA-256"* |
| 37-39 s | *"every failed strategy gets saved to a local database where the agent studies it"* + an overfit warning (*"a sample of only 12 trades"*) |

We already had the loop, the walk-forward gates, the read-once holdout and the
SQLite memory. What was missing is implemented here.

## 1. The sealed risk layer (`selftrade/risk_rules.py`)

```powershell
python -m selftrade risk show     # the read-only panel + the SHA-256
python -m selftrade risk verify   # is the seal intact?
python -m selftrade risk seal     # re-seal on purpose
```

The limits are the video's, with the same names:

```
MAX_POSITION_PCT   = 5.0     HARD_STOP_LOSS_PCT = 5.0
MAX_DAILY_LOSS_PCT = 2.0     MAX_DRAWDOWN_PCT   = 15.0
MODEL_CAN_OVERRIDE = False   SHA-256 = f145ca89d49d48ea...
```

Sealing is enforced, not decorative:

1. `RiskLimits` is a **frozen dataclass** - the process cannot mutate it
   (`dataclasses.FrozenInstanceError` in the tests);
2. the limits are **not in `config.yaml`**, so `--set risk_rules.MAX_DRAWDOWN_PCT=99`
   fails as an unknown config key, exactly like a typo;
3. `validate_genome` drops any genome key that tries to name them, so an LLM
   proposal cannot widen them either;
4. the digest is **pinned** in the source and re-verified at every deployment.
   Edit the numbers and the next deployment refuses until you re-seal on purpose;
   a corrupt or tampered seal file refuses too;
5. `SELFTD_ALLOW_RISK_OVERRIDE` in the environment raises instead of working.

**Scope, deliberately:** the limits guard *deployment* - the critic verdict, the
paper session and the live sizing rail (`mt5.compute_lots` clamps `risk_pct` to
`MAX_POSITION_PCT`). The research backtester is **not** clamped, because
exploration has to be free and, more importantly, paper/backtest/`invest` must stay
comparable by construction. The critic therefore *warns* when the configured
research sizing (95%) is above the sealed 5% instead of pretending the two rails
are the same thing.

## 2. The critic (`selftrade/critic.py`)

```powershell
python -m selftrade critic --champion          # exit 0 = deployable, 1 = refused
```

It judges the **out-of-sample** window and returns a verdict with the numbers
attached. Hard checks refuse; warnings are recorded without blocking.

| check | refuses when |
| --- | --- |
| `seal` | the sealed limits changed after sealing, or the seal file is corrupt |
| `max_drawdown` | drawdown > 15% |
| `hard_stop` | the genome has no stop, or a stop looser than 5% |
| `max_daily_loss` | the worst calendar day lost more than 2% |
| `trades` | the strategy never traded out-of-sample |

Warnings: research sizing above the sealed cap, a sample too thin to separate
skill from luck, and results resting on the 3 best trades.

The refusal has **consequences**: `paper --create` runs the critic first and exits
non-zero, recording the refusal as a lesson in the memory. The only way past it is
an explicit `deploy_gate=False` at the API level, which the CLI never passes -
there is no `--force`.

## 3. The autopsy (`selftrade/autopsy.py`)

```powershell
python -m selftrade autopsy --champion --window holdout --out reports/autopsy.md
```

For every closed trade it reads the market **at that trade's entry bar** (RSI, ATR%,
distance from the 200-period average - causal by construction, tested by shifting
the future and re-checking), then:

* ranks the worst N losses with that context in one sentence each;
* **clusters failure modes** across all the losers - counter-trend entry,
  above-median volatility, stopped out within 3 bars, bled out on a late signal
  exit, the stop doing all the work, chased extension - each with the share of
  losers, the same share among winners, and a concrete **fix**;
* compares losers against winners on the same context columns;
* prints the sample-size caveats the video warns about (too few trades, profit
  concentrated in three trades, tail risk, low win rate);
* writes the whole thing as a **lesson into the SQLite memory**, which is what
  makes the next generation different from the last one.

## Real run, 2026-10-10 (BTC/USDT 4h, 18 000 real candles)

The champion of the 12-generation real run was refused:

```
Refused: 28.4% drawdown breaks the sealed 15.0% limit; the worst day lost
4.03%, past the sealed 2.0% daily halt. I will not deploy it.
```

and `paper --create --champion` exits 1 with that reason on stderr.

The autopsy on the same holdout window:

```
Loaded 90 trades - 72 losers (80%); the worst 5 took 1,162.35 out of the account.
Dominant failure mode: entry in above-median volatility (24 of 72 losers, 33%).
```

| # | P&L | return | bars | what the market looked like at entry |
| ---: | ---: | ---: | ---: | --- |
| 1 | -300.99 | -2.8% | 1 | long, RSI 49.4, 8.2% above the 200-period average, ATR 1.3%, out on signal |
| 2 | -298.49 | -3.3% | 8 | long, RSI 63.7, 3.9% above, ATR 2.2% |
| 3 | -207.52 | -2.3% | 21 | long, RSI 76.7, 16.5% above, ATR 1.3% |
| 4 | -194.00 | -1.9% | 1 | long, RSI 52.9, 0.5% above, ATR 1.1% |
| 5 | -161.35 | -1.5% | 3 | long, RSI 52.9, 9.4% above, ATR 1.4% |

Failure modes: above-median volatility (33% of losers vs 28% of winners - the
weakest signal in the set, which is worth saying out loud), late signal exit (4%
vs 72%), chased extension (4% vs 22%), counter-trend entry (1% vs 0%). Caveats:
the worst trade is 3x the median loss, and the win rate is 20%.

Two honest readings of that table: the dominant cluster is *not* strongly
discriminating (33% vs 28%), so "skip high-ATR entries" is a hypothesis, not a
proven fix; and the mode with the real contrast - late signal exits (4% of losers
but 72% of winners) - is telling us the exits are timing-sensitive, not that the
entries are wrong. The lesson written to memory says exactly that.

## Limits

* The critic judges **one genome on one window**. It is a risk gate, not a
  portfolio supervisor: no correlation across positions, no broker-side kill
  switch (MT5 has its own two-key gate), no intraday liquidation logic.
* `MAX_POSITION_PCT` binds the live rail only; the research engine keeps the
  configured size on purpose (see above).
* The autopsy's `high_vol` label uses the window median of ATR% - a diagnostic
  reference computed with hindsight, never a trading input.
* Failure modes are single-condition counts with a winner contrast; they are
  leads for the next generation, not statistical proof of causation.
* Everything here is historical. It is not a forecast and not financial advice.
