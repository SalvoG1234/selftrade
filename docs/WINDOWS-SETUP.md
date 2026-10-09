# Windows 11 setup

## Prerequisites

- Python **3.11+** (64-bit) on `PATH` (`python` or the `py` launcher).
- Optional: [Ollama](https://ollama.com) for LLM proposals.
- Optional: MetaTrader 5 from Trading.com for the FX/CFD bridge.
- Optional: Node.js (`npx`) for REA.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
# with the MT5 bridge:
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithMt5
```

`setup.ps1` creates `.venv`, installs `requirements.txt` (+ `requirements-dev.txt`
with pytest), optionally `requirements-mt5.txt`, and creates `data/`, `state/`,
`reports/`, `user_data/strategies/`.

Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

## Run

```powershell
.\run.ps1 doctor
.\run.ps1 data --rows 12000
.\run.ps1 evolve --generations 5 --no-llm
.\run.ps1 report
.\run.ps1 hall
```

Or call the module directly:

```powershell
.\.venv\Scripts\python.exe -m selftrade doctor
```

## Overriding config

Every value is overridable from the CLI with `--set section.key=value`:

```powershell
.\run.ps1 evolve --generations 3 --set backtest.fee_bps=1.2 --set evaluation.metric=sortino
```

A misspelled key (e.g. `backtest.fee_bp`) is an error, not a silent change.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The suite runs offline: Ollama, REA and MT5 are exercised against local stubs,
so no network, no terminal and no Node.js are required.

## Execution policy

If PowerShell blocks the scripts:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

or run each script with `-ExecutionPolicy Bypass`.
