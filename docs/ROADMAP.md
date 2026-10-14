# Roadmap

Not a feature list. Every milestone below is **closed by a check someone else can run**, and the
ones that are blocked say what they are blocked on. If a milestone cannot state its check, it does
not belong here — this project has no roadmap item that reads "improve the strategy".

Status legend: **done** (the check has been run and recorded), **open** (the check can be run
here now), **blocked** (it needs something this environment does not have).

| # | Milestone | Closed when | Status |
| --- | --- | --- | --- |
| 1 | **CI proves the declared support** | the workflow is green on a pushed commit for Python 3.11, 3.13 and 3.14 on both Windows and Ubuntu, and the run's URL is in `docs/VERIFICATION.md` | open — the workflow is written; the first run happens on the next push |
| 2 | **A clean-clone reproduction of the headline number** | a fresh clone, `data --fetch`, `evolve --generations 12 --no-llm`, `invest --champion --window holdout` reproduces the holdout rejection (Sharpe −0.55) from a **clean** worktree, with the `repro_id` recorded | open — the recorded run was made on a dirty worktree, so its `reproducible` flag is `0`; that is the honest gap |
| 3 | **The docs cannot rot** | `tests/test_docs.py` fails on any relative link or image that does not resolve, or on a document missing from `docs/README.md`; `python -m selftrade verify --ledger` fails when any whole-suite count in the documentation disagrees with the measured suite; `python -m selftrade verify --card` fails when the social card's own record (the count, and the source it was drawn from) no longer matches this tree, which is the one copy of that number a text check cannot see; all three run in CI | done — the link test caught the links added with the screenshots, and the ledger command found four counts in the launch kit that had been stale since they were typed, which is exactly the failure a human eye had missed; the card check exists because the same failure survives one layer down, in the picture |
| 4 | **The Binance rail seen working with a real key** | with the user's own Testnet key, `binance check` returns `ok` with permissions, balances and `0 POST`, and the output is a row in `docs/VERIFICATION.md` | blocked on a real Testnet key (the stored-key path was verified with a deliberately bogus one: a real `-2014` from the exchange) |
| 5 | **The alert channel delivered a real message** | a Telegram bot token exists, `notify test` sends, `notify_state.json` records it, and the dashboard's alerts card shows the delivery | blocked on a bot token |
| 6 | **MT5 against a real demo terminal** | `mt5 doctor` connects to a Trading.com demo account, a dry run builds the request, and only the two-key path sends exactly one `order_send` | blocked on Windows + a demo account (the fake-terminal tests are what CI runs) |
| 7 | **The LAN bind, from another device** | a phone on the same Wi-Fi opens the dashboard, is refused without the password, signs in with it, and the session is visible in `state/` | open — needs a second device on the same network |
| 8 | **A run reproduced by someone who is not the author** | an issue or discussion reports `doctor` output plus a result that matches this repository's recorded one | blocked on a reader |
| 9 | **Every `N/D` in the UI is explained** | each panel's `N/D` state names its missing precondition in the tooltip, and a test asserts there is no bare `N/D` in the dashboard markup | open — a sweep of the dashboard's empty states |
| 10 | **A first tagged release** | the version in `selftrade/__init__.py` moves, `CHANGELOG.md` gets a dated section, and the tag is pushed | deliberately not done: nothing here has been released, so inventing a history would be the over-claim the project avoids |

## Things that are explicitly **not** on this roadmap

* **Live trading.** There is no milestone for sending real orders. The rails are a simulated
  broker, a read-only probe and a demo-gated MT5 bridge, and that is the design.
* **A profitability target.** No milestone says "beat buy & hold". The honest result is currently
  a loss, and the project's claim is about the *process* being trustworthy, not about the return.
* **More strategy families.** A new family is a pull request with a test, not a roadmap line.
* **A hosted service.** This is a local tool by construction; the dashboard is loopback-first and
  the LAN bind is the supported extent of it.

## Mirroring this in GitHub

The milestones above are written to be pasted into the repository's **Milestones** as-is. Keeping
them there rather than in a wiki is deliberate: GitHub renders open/closed state next to issues,
so "how much of this is actually verified" is answerable without reading a document.

Titles to create, in the order above:

1. `CI proves the declared support`
2. `Clean-clone reproduction of the headline number`
3. `Docs cannot rot`
4. `Binance rail with a real key`
5. `Alert channel delivered a real message`
6. `MT5 against a real demo terminal`
7. `The LAN bind, from another device`
8. `A run reproduced by someone else`
9. `Every N/D explained`
10. `First tagged release`
