# Trading.com / MetaTrader 5

Trading.com exposes **WebTrader + MetaTrader 5** and **no public REST or
WebSocket API**. The only automatable path is MT5: the `MetaTrader5` Python
package (Windows only) talking to a running, logged-in terminal.

```
selftrade -> mt5.py -> MT5 terminal -> broker
```

## Install

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithMt5
# or: .\.venv\Scripts\python.exe -m pip install -r requirements-mt5.txt
```

The package is Windows-only and is **never required by the test suite** — the
bridge is written for injection (`module=`) and the tests use a fake terminal.

## Config

```yaml
data:
  source: mt5          # or keep csv and use `mt5 bars` snapshots
mt5:
  enabled: true        # talks to the terminal
  live: false          # ARMS the bridge for order sending
  require_demo: true   # a real account is refused
  require_stop: true
  login: 12345         # not a secret
  password_env: SELFTD_MT5_PASSWORD  # the NAME of the env var only
  server: mt5-us.trading.com
  symbol: EURUSD
  suffix: ""           # e.g. ".pro" if the broker appends one
```

The **password is read only from the environment variable** named by
`mt5.password_env`. It is never printed, logged, stored in the database or
written to a report.

```powershell
$env:SELFTD_MT5_PASSWORD = "..."   # session scope
```

## Data in

`data.source: mt5` produces the **same** OHLCV frame as every other source
(UTC index, `tick_volume` → `volume`, validated by `ensure_ohlcv`). For offline,
reproducible research, take a snapshot:

```powershell
.\mt5.ps1 bars --out data/EURUSD_1h.csv
.\run.ps1 evolve --set data.source=csv --set data.csv=data/EURUSD_1h.csv
```

Symbol resolution tries `symbol + suffix` first, then the plain name, then asks
the terminal for symbols starting with the name (and lists candidates on
failure). FX/CFD do not tick on weekends: an empty result is reported as a
likely closed market, not as a crash.

## Cost model (critical)

The repo defaults (`fee_bps: 5`, `slippage_bps: 2`) are crypto defaults. On
EURUSD (1 pip ≈ 0.9 bps) they are ~6× the real cost and **hide every edge**.

```powershell
.\mt5.ps1 costs --symbol EURUSD
```

It reads the live spread and converts it:

```
spread_points * point / mid * 10000 = spread in bps
```

Because the backtester charges `fee_bps + slippage_bps` **per side**, setting
*both* to half the spread makes the round trip cost exactly one spread:

```powershell
.\run.ps1 backtest --family trend_momentum \
  --set backtest.fee_bps=0.909 --set backtest.slippage_bps=0.909
```

The backtester **does not** model overnight swap/financing. Weekend gaps must be
present in the data so the stop fills at the gap price, not at the stop level.

## Orders out — the two-key gate

An order leaves the machine only when **both** keys are present:

1. `mt5.live: true` in `config.yaml` — arms the bridge; **and**
2. `--live` on the command line — presses the trigger for that invocation.

```powershell
.\mt5.ps1 order --symbol EURUSD --side buy --sl-points 500              # dry run
.\mt5.ps1 order --symbol EURUSD --side buy --sl-points 500 --live       # sends, if armed
```

Independent stops, all enforced:

- `require_demo: true` — a real account is refused **even when armed**;
- `require_stop: true` — no order without a stop loss;
- `allowed_symbols` — allow-list (empty = all);
- `max_lots`, `max_positions`;
- `max_spread_points` — refuse news/rollover spreads;
- symbol `trade_mode` must not be disabled/close-only;
- broker `volume_min` / `volume_step` are respected.

The **dry run is the default**: it builds the complete request (`action, symbol,
volume, type, price, sl, tp, deviation, magic, comment, type_time,
type_filling`) and never calls `order_send`. A test proves `order_send` is not
called. Every rejection lists its reasons readably and exits non-zero.

## Sizing

Lots are derived from `risk_pct` of equity divided by the stop distance, using
the broker's `trade_tick_value` / `trade_tick_size`. If the broker does not
publish them, the bridge falls back to `fixed_lots` and **says so in the notes**
— it never invents a number. `sl_fallback_points` is used only for sizing when
no stop distance is given.

## Doctor

```powershell
.\mt5.ps1 doctor
```

Reports the package, connection, account (login/server/balance, DEMO or not),
the resolved symbol and spread, the order gates, and the *name* of the password
environment variable — never its value.
