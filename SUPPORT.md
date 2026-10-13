# Support

This is a research project published to collect feedback, not a supported product. What follows
is what you can expect, and what makes a question answerable.

## Before asking

Two commands print the state the program actually sees. Most reports are answered by their output:

```powershell
.\run.ps1 doctor          # interpreter, data file, database, config, what is missing
.\run.ps1 risk verify     # the sealed limits, the levers that could move them, the seal on disk
.\run.ps1 switch show     # is trading stopped, by whom, and why
```

Then read the two documents that answer most setup questions: [`docs/QUICKSTART.md`](docs/QUICKSTART.md)
(first run, start to finish) and [`docs/WINDOWS-SETUP.md`](docs/WINDOWS-SETUP.md) (Python, the
virtualenv, PowerShell execution policy).

## Which channel

| What you have | Where it goes |
| --- | --- |
| Something is broken, contradicts the documentation, or cost you time | [Bug report](https://github.com/SalvoG1234/selftrade/issues/new/choose) |
| A capability the program should have and does not | [Feature request](https://github.com/SalvoG1234/selftrade/issues/new/choose) |
| "Why is it built this way?", a design you disagree with, a claim you do not trust | [Discussion](https://github.com/SalvoG1234/selftrade/discussions), or the design-feedback form |
| A way to send a real order, move the sealed limits, or reach a credential | [SECURITY.md](SECURITY.md) — private reporting, not a public issue |

## What to include

The command you ran (exactly), what you expected and where that expectation came from, what
happened instead, and the full output — the **first** error included, repeated lines trimmed.
Say whether this is a fresh clone and which platform you are on (Windows 11 is the target;
Ubuntu also runs in CI).

## What is not supported here

* **Financial advice, profit expectations, or signals.** The honest out-of-sample result is in
  the README, and it is a loss. Nobody here will tell you whether this will make money, because
  the project cannot answer that.
* **Live trading.** There is no live rail to debug: this build sends no real orders, by design
  and by test.
* **A guaranteed fix or response time.** Issues are read and the safety-related ones take
  precedence, but there is no on-call rotation behind this repository.
* **MetaTrader 5 without a terminal.** The bridge is Windows-only and needs a demo account; the
  fake-terminal tests are what CI runs.

## Known limits, stated up front

The README's *Verification status* section and [`docs/VERIFICATION.md`](docs/VERIFICATION.md)
list what has been run and, explicitly, what has not: no real MT5 terminal, no real Telegram
message, no real API key with real balances, no second machine on the LAN. A question about one
of those is still welcome — it just starts from "unverified", not from "broken".
