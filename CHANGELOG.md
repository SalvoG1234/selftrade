# Changelog

**This project has no tagged releases yet.** `selftrade.__version__` is `0.1.0`, the repository
was published as an experimental research project, and everything below has landed on `master`.
There is therefore one section, not a version history — inventing release numbers for work that
was never released is the kind of claim the rest of this repository tries to avoid.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and each line names the
commit that carries it, so any entry can be checked with `git show <hash>`. The detailed record
is the git history itself; this file is the short version.

## [Unreleased]

### Added

- **API keys page** — the one credential a config file cannot hold. `GET /api/keys` reports, per
  rail credential, the source **in force** (an exported variable always wins over the local file)
  and a SHA-256 fingerprint; `POST /api/keys` stores or forgets one behind an explicit
  confirmation and files a `key_store` risk event. No endpoint ever returns a value, and the
  page states that the file is clear text. *(uncommitted at the time of writing)*
- **The account top-up has its own menu entry** — `Capital`, with the ledger, the policy caps and
  the confirm-gated movements (`d76744a`).
- **The dashboard is ten pages in five groups**, and the emergency stop has its own *Risk Center*
  rather than sharing a rail page with everything else (`d71961b`).
- **The benchmark sits beside the out-of-sample number** — every fold and the holdout are measured
  against buy & hold, so a metric reads as excess rather than as skill; the lab says `N/D` rather
  than inventing a comparison when no benchmark was recorded (`ec1e0e6`, `b0e9dc3`).
- **A runnable soak for the session lock** — `python -m selftrade lock-stress` runs the two writers
  against each other with the Windows lost-create race injected (`3380521`, `ac71291`).
- **A simulated broker, a pre-trade risk gate and a kill switch** (`1fecb34`).
- **An autopsy and a critic over a sealed risk layer** — the limits are a frozen dataclass pinned
  by SHA-256, unreachable from config, genome or environment (`6369d9e`, `904595e`).
- **The search on real prices, and `invest`** — what a fixed amount of cash would have become,
  costs included (`ea7d2b8`).
- **The program itself**: data loaders, strategy families, walk-forward evaluation, gates, memory,
  the CLI, the reports and the dashboard (`c7b7df6`).
- **A launch kit** — the claims that have a command behind them, the ones that must never be made,
  and where the project is worth posting (`9c6fdce`).

### Changed

- **CI now tests the declared support** instead of one interpreter: the suite runs on Windows and
  Ubuntu for Python 3.11, 3.13 and 3.14, plus a sealed-limits check and the lock soak. Dependency
  update pull requests are configured monthly. *(this change)*
- **The README is a front page again**: a table of contents, real dashboard screenshots, an honest
  badge set, and the long verification ledger moved to `docs/VERIFICATION.md` — moved, not
  rewritten, and the negative results stay on the front page.
- **CI actions were updated** off the deprecated majors (`8acb5d3`).

### Fixed

- **An operator stop could be lost to a Windows lock race** — the stop is now created under the
  same lock the writers take (`5ae99ce`).
- **The LAN bind is closed while the bootstrap password `0000` is in force**, and a credential
  store that cannot be read is answered instead of trusted (`2b28878`).
- **The claims table's header** declared two columns while its rows carried three, so GitHub
  dropped the command column — the column that makes the file checkable (`8533d60`).
- **The backtest JSON payload was not always machine-readable** (`c37d0d5`).
- **The missing-MetaTrader5 message is tested**, rather than the host it happens to run on
  (`b927fb9`).

### Security

- Live trading stays off: the rails are a simulated broker, a read-only Binance probe and an MT5
  bridge behind two keys plus `--live`, and no code path in CI sends a real order.
- The sealed risk limits cannot be moved by config, a genome or an environment variable.
- Rail credentials are stored owner-only in a gitignored file, an exported environment variable
  always overrides it, and no API returns a value.
