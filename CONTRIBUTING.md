# Contributing

This repository is an **experiment published to be argued with**, not a product with a roadmap
someone else has to follow. That changes what a useful contribution looks like.

## The most valuable thing you can send

Not a feature. A **reproducible disagreement**:

* a way to make the program do something it claims it cannot (place a real order, move the
  sealed limits, trade while stopped);
* a number in the README that does not hold up — where would you demand more evidence?
* a run you could not reproduce from its own record;
* a piece of documentation that promised something the code does not do.

Open an [issue](https://github.com/SalvoG1234/selftrade/issues/new/choose) for those. If the
subject is open-ended ("why is it built this way?"), a
[discussion](https://github.com/SalvoG1234/selftrade/discussions) is the better place.

Two commands print what the program actually sees and make a report much easier to act on:

```powershell
.\run.ps1 doctor
.\run.ps1 risk verify
```

## Setting up

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1     # .venv + requirements
.\run.ps1 doctor                                        # what the program can see
python -m pytest -q                                      # the suite (<!-- generated: suite-count -->856<!-- /generated: suite-count --> tests at this commit)
```

Python **3.11+** (64-bit). Windows 11 is the target; Ubuntu is what CI also covers. No API key
is needed for anything in the default configuration — the price feed is keyless public data.

## What this project will not accept

* **Live trading.** No change may add a path that sends a real order, or a default that arms a
  rail. The rails are: a simulated broker, a read-only Binance probe, and an MT5 bridge behind
  two keys plus `--live`, which this repository never invokes in CI.
* **A claim without a command.** If a change moves a number in the README, the pull request says
  which command produces the new one. Editing results to look better is the one thing this
  project has explicitly decided not to do — the shipped holdout result is a rejection.
* **Secrets in the tree.** No database, session file, API key, token, `state/` file or `.env`.
  `state/secrets.json` is gitignored for exactly this reason; an exported environment variable
  always overrides it.
* **A new dependency without a reason.** The list is deliberately short (numpy, pandas,
  requests, PyYAML). Say what the dependency buys and why the standard library cannot.

## Changing the code

* Match the surrounding style. The house habit is **comments that explain why**, not what —
  especially where the code could have done something simpler and chose not to.
* Tests live in `tests/` and are expected with behaviour changes, not after them. They must not
  touch the network: the project injects its transports and clients (see `tests/fake_mt5.py`,
  `tests/fake_loop.py`, and the `RequestsTransport` seam in `selftrade/binance.py`).
* Tests must not write into `state/`. Point the setting at `tmp_path` (most fixtures already
  do — `dashboard.password_file`, `binance.key_file`, `memory.db`).
* Anything in `selftrade/execution/` is the safety-critical half: the pre-trade gate, the kill
  switch and the sealed limits. Changes there need the test that would fail if the rule were
  loosened, and `python -m selftrade risk verify` must still exit 0.
* The dashboard is one inline HTML/CSS/JS file (`selftrade/dashboard_ui.py`) with no build step.
  Its page structure is a contract some tests read (`tests/test_dashboard.py`), so a new page
  means a nav item, a view, a renderer and the test that keeps them in step.

## Pull requests

Small, single-purpose, with the evidence in the description — the
[template](.github/PULL_REQUEST_TEMPLATE.md) asks for the command and its output, because
"tests pass" is not something a reviewer can check.

CI runs the suite on **Windows and Ubuntu** for **Python 3.11, 3.13 and 3.14**, plus the session
lock soak (`python -m selftrade lock-stress`). If your change is platform-specific, say which
platform you ran it on.

## Documentation

`docs/` is organised by what a reader is trying to do — see [`docs/README.md`](docs/README.md).
`README.md` is the front page and keeps the honest results; `docs/VERIFICATION.md` holds the full
ledger of what was actually run, and it is the file to update when you verify something new.

If your change adds or removes tests, the suite count in five documents is stale by exactly that
much. You do not have to find them by hand:

```powershell
python -m selftrade verify --ledger            # which counts are stale, and against what
python -m selftrade verify --ledger --write    # repair them, then record a real run
```

CI runs the check, so it is not optional — but the numbers it writes are measured, so nothing has to
be typed and nothing has to be remembered.

If you touch the dashboard's markup, the screenshots in `docs/img/` are real renders of a running
server. Regenerate them rather than letting them go stale, and never add a mock-up that looks
like a capture.
