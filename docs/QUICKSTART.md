# Quick start

From a fresh clone to a running dashboard on **Windows 11**. Everything here needs no API key:
the price feed is keyless public data, and no step in this file can send an order.

If Python or PowerShell themselves are the problem, read
[`WINDOWS-SETUP.md`](WINDOWS-SETUP.md) first — this page assumes Python 3.11+ (64-bit) is on
`PATH`.

## 1. Install

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

That creates `.venv`, installs `requirements.txt` + `requirements-dev.txt`, and makes the
`data/ state/ reports/ user_data\strategies/` directories. Two switches exist if you want them:
`-NoDev` (skip pytest) and `-WithMt5` (the Windows-only MetaTrader 5 bridge, which the rest of
the program does not need).

Then ask the program what it can see:

```powershell
.\run.ps1 doctor
```

`doctor` never fails on a missing subsystem — it reports. On a fresh clone it will say the data
file is missing and the database is empty, which is exactly right at this point.

## 2. Real prices

```powershell
.\run.ps1 data --fetch --rows 18000
```

18 000 real BTC/USDT 4h candles from the public mirror into `data/BTCUSDT_4h.csv`
(2018-07-23 → 2026-10-10 in the recorded run, ~50 s, 18 paged requests, no key). Use
`data --rows 12000` for a synthetic frame if you are offline, and read
[`INVESTMENT-SIM.md`](INVESTMENT-SIM.md) before comparing the two — synthetic numbers are not
market results.

## 3. Search

```powershell
.\run.ps1 evolve --generations 12 --no-llm
```

Twelve generations of bounded genome changes, evaluated with real costs, walk-forward and one
read of the untouched holdout at the end. `--no-llm` keeps it deterministic and offline; drop it
if you have [Ollama](https://ollama.com) running and want the model to propose the mutations
(it proposes, it never writes trading logic, and the output is clamped).

Then read what came out:

```powershell
.\run.ps1 report     # the last run, including the critic's verdict and the autopsy
.\run.ps1 hall       # the hall of fame
```

**Expect the champion to be rejected.** On the shipped data the search finds a fold-mean Sharpe
of 0.886 and the holdout then rejects it (Sharpe −0.55), and the deployment rail refuses it:

```powershell
.\run.ps1 critic --champion     # exit 1
# Refused: 28.4% drawdown breaks the sealed 15.0% limit; the worst day lost
# 4.03%, past the sealed 2.0% daily halt. I will not deploy it.
```

That is the program working, not a broken install: it is the same number the README leads with.
An install where every run is "approved" is one you should not trust.

## 4. What if we had really invested it

```powershell
.\run.ps1 invest --champion --window holdout
```

Simulates 10 000 in cash over the genuinely out-of-sample tail, beside buy & hold, with the same
costs: 8 456 against 8 608 in the recorded run, a 28.4% drawdown against 53.4%, and 2 472 paid in
fees. It writes an HTML report with four charts under `reports/`.

## 5. Paper-trade it forward

```powershell
.\paper.ps1 -Create
.\paper.ps1 -All
.\paper.ps1 -Status
```

The forward session turns the strategy's signals into **orders** that pass the pre-trade gate and
fill on the simulated broker. `-Create` on the shipped champion is **refused** by the critic with
the same 28.4% drawdown message and exit 1 — the deployment gate is wired into this rail too.
`docs/PAPER-TRADING.md` covers funding a session from the capital account and resetting it.

## 6. The dashboard

```powershell
.\dashboard.ps1
```

Loopback only (`http://127.0.0.1:8787`). The first login is the bootstrap password **`0000`**, and
the Settings page banners until you change it. `-Port 8788` if the port is taken; the LAN needs
`-BindHost 0.0.0.0` **and** a real password, because the bind is refused while `0000` is still in
force.

```powershell
.\dashboard.ps1 -BindHost 0.0.0.0 -Port 8788
```

Pages, endpoints and what each page deliberately refuses to show:
[`DASHBOARD.md`](DASHBOARD.md).

## 7. The tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

They use no network and no real broker: the transports, the terminal and the loop are injected.
CI runs the same suite on Windows and Ubuntu, plus the sealed-limits check and the session-lock
soak (`python -m selftrade lock-stress`).

## Where things live

| Path | What it is | Safe to delete? |
| --- | --- | --- |
| `data/*.csv` | price data (`data --fetch` regenerates it) | yes |
| `state/selftrade.db` | runs, candidates, lessons, paper sessions, audit rows | yes, but you lose the history |
| `state/secrets.json` | rail credentials, **clear text**, gitignored | yes (that removes the stored key) |
| `state/kill_switch.json` | the emergency stop's state | only if the switch is not armed |
| `state/dashboard_auth.json` | one salted hash for the dashboard login | yes (back to `0000`) |
| `reports/` | generated markdown/JSON/HTML reports | yes |
| `.venv/` | the virtualenv | yes (`setup.ps1` rebuilds it) |

Nothing in `data/`, `state/`, `reports/` or `user_data/strategies/` is tracked by git, and
`.gitignore` keeps it that way — see [`../SECURITY.md`](../SECURITY.md).

## When something goes wrong

| Symptom | What it is | What to do |
| --- | --- | --- |
| `virtualenv not found. Run .\setup.ps1 first.` | the `.ps1` wrappers run `.venv\Scripts\python.exe` and nothing else | `setup.ps1` |
| `... cannot be loaded because running scripts is disabled` | PowerShell execution policy | the `-ExecutionPolicy Bypass` form used above |
| `doctor` says the data file is missing | a fresh clone has no `data/*.csv` (it is gitignored) | `.\run.ps1 data --fetch --rows 18000` |
| `no champion in the hall of fame yet` | nothing has been evolved | `.\run.ps1 evolve --generations 5 --no-llm` |
| `paper --create` exits 1 with a drawdown | the critic refusing the deployment — by design | read the message; that is the gate, not a bug |
| the dashboard login rejects everything | `state/dashboard_auth.json` is unreadable (a half-written or edited file) | `.\run.ps1 password --reset --confirm`, then `0000` |
| the dashboard refuses to open on the LAN | the bootstrap password is still in force | change it under Settings, then bind again |
| Ollama not reachable | the LLM proposer is off | the run continues with deterministic mutation; the report records the mix |
| a metric reads `N/D` | it cannot be measured on what exists (no benchmark, no session, no baseline) | not an error; the measurement's precondition is missing |

## What this quick start will not do

It will not make the strategy profitable, and it will not tell you that it is. The first honest
result is a loss, recorded in [`../README.md`](../README.md); the same numbers are in
[`VERIFICATION.md`](VERIFICATION.md) with the commands that produced them. If a step here ever
starts printing better numbers than that file records, that is a bug worth reporting.
