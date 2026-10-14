# Documentation

Twenty-five documents, grouped by **what you are trying to do** rather than by when they were
written. The front page ([`../README.md`](../README.md)) keeps the honest results, the safety
story and the summary of what has been verified; this directory is the detail behind it.

If you read only three things, read *[Quick start](#start)*, *[What stops an order](#safety)* and
*[Judging a result](#research)* — in that order, they answer "does it run?", "can it hurt me?"
and "is the number real?".

## Start

| Document | What it is for |
| --- | --- |
| [`QUICKSTART.md`](QUICKSTART.md) | a fresh clone to a running dashboard, with the troubleshooting that actually comes up |
| [`WINDOWS-SETUP.md`](WINDOWS-SETUP.md) | Python 3.11+, the virtualenv, PowerShell execution policy, what `setup.ps1` does |

## Architecture

| Document | What it is for |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | the search loop, the fitness, the gates, the walk-forward folds and the holdout |
| [`LOOP-AI-PLAN.md`](LOOP-AI-PLAN.md) | the design notes for the AI loop *(Italian)* |
| [`SESSION-LOCK.md`](SESSION-LOCK.md) | the session file, its lock, and the soak that runs two writers against each other |
| [`STRESS.md`](STRESS.md) | resampling a result: how much of it is the draw, and how much is the numbers |
| [`EXTENDING.md`](EXTENDING.md) | adding a strategy family, a rail or a dashboard page |

## Safety

| Document | What it is for |
| --- | --- |
| [`EXECUTION-AND-RISK-GATE.md`](EXECUTION-AND-RISK-GATE.md) | the order-level rail: simulated broker, pre-trade gate, kill switch |
| [`AUTOPSY-AND-CRITIC.md`](AUTOPSY-AND-CRITIC.md) | the autopsy of losing trades and the critic that refuses to deploy |
| [`NOTIFICATIONS.md`](NOTIFICATIONS.md) | the risk alerts: the halt, and the warning before it |
| [`../SECURITY.md`](../SECURITY.md) | what counts as a vulnerability here, and how to report one privately |

## Using it

| Document | What it is for |
| --- | --- |
| [`DASHBOARD.md`](DASHBOARD.md) | every page, every endpoint, and what each page deliberately does not show |
| [`CAPITAL.md`](CAPITAL.md) | the simulated account, who may credit it, and what a movement means |
| [`PAPER-TRADING.md`](PAPER-TRADING.md) | trading the champion forward on the simulated rail |
| [`INVESTMENT-SIM.md`](INVESTMENT-SIM.md) | `invest`: what a fixed amount of cash would have become |
| [`BINANCE-TESTNET.md`](BINANCE-TESTNET.md) | the Spot Testnet rail: two keys, one opt-in host, and the read-only probe |
| [`TRADING-COM-MT5.md`](TRADING-COM-MT5.md) | the Windows-only MetaTrader 5 bridge, demo accounts only |
| [`REA-INTEGRATION.md`](REA-INTEGRATION.md) | the optional reverse-engineering evidence tooling |
| [`RUFLO-SWARM.md`](RUFLO-SWARM.md) | the optional MCP wiring that exposes the same evidence to an agent swarm |

## The Social Manager

A second program in this repository (`socialmanager/`, 235 of the suite's tests) that promotes the
project on GitHub, X and Instagram in Italian and English, under a deterministic gate. It cannot
order, commit or publish without passing that gate.

| Document | What it is for |
| --- | --- |
| [`SOCIAL-MANAGER.md`](SOCIAL-MANAGER.md) | the components, the 13 policy checks, the three autonomy levels, the seven cases that always need a human, and the test report |
| [`SOCIAL-CONNECTORS.md`](SOCIAL-CONNECTORS.md) | per-platform setup, permissions and scopes, published limits and prices, and what has *not* been verified against a live account |
| [`SOCIAL-DEPLOY.md`](SOCIAL-DEPLOY.md) | the local and cloud halves: the container, the scheduler, the database, backups, health checks and the credentials you must provide |
| [`social-samples/drafts.md`](social-samples/drafts.md) | the bilingual drafts it produced, clearly marked as drafts, each with its sources |
| [`social-samples/calendar-initial.json`](social-samples/calendar-initial.json) | the initial editorial calendar it proposes |

## Research and reporting

| Document | What it is for |
| --- | --- |
| [`VERIFICATION.md`](VERIFICATION.md) | **the ledger**: every command that was actually run, with its output, including the failures |
| [`ROADMAP.md`](ROADMAP.md) | the milestones, each with the check that would close it |
| [`LAUNCH.md`](LAUNCH.md) | what may be claimed in public, where to say it, and what must never be claimed |
| [`MAINTAINING.md`](MAINTAINING.md) | the GitHub settings only the owner can make: About text, topics, social preview, and the release checklist |

## Conventions used across these documents

* **`N/D` means "cannot be measured", never "zero".** The program prints `N/D` rather than
  filling a cell, and so do the documents.
* **Every number has a command.** If a document quotes a result, it names the command that
  produces it; [`VERIFICATION.md`](VERIFICATION.md) is the file where the outputs live.
* **Negative results stay in.** The holdout rejected the shipped champion, and the critic refused
  to deploy it. Those are recorded here the same way the good rows are.
* **Nothing here promises profit.** This is a research project, not advice, and it does not send
  real orders.
