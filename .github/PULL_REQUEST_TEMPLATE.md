## What this changes

<!-- One or two sentences. Which part of the program or which document, and why. -->

## Does it move a number or a claim?

<!-- This project's README contains real, unflattering results (the holdout rejected the
     champion). If this PR changes any figure, table row, threshold or sentence that makes a
     claim about behaviour, say which one and paste the command that produces the new value.
     If it changes nothing a reader could check, write "no claim moves". -->

## Evidence

<!-- The exact commands you ran and what they printed. A passing `python -m pytest` step is
     itself evidence only if you paste the last line; "tests pass" is not. -->

```
python -m pytest -q
```

## Trading path and risk layer

<!-- Tick only what is true. -->

- [ ] This PR does **not** touch the order path (`selftrade/execution/`), the sealed limits
      (`selftrade/risk_rules.py`) or the kill switch.
- [ ] It touches them, and the sealed limits are **unchanged** (`python -m selftrade risk verify`
      still exits 0, digest unchanged).
- [ ] It cannot enable live trading: no new way to send a real order, no new default that arms
      a rail.

## Checklist

- [ ] `python -m pytest -q` passes locally on **Windows** (the target platform).
- [ ] No database, key, token, session file or `state/` file is included in the diff
      (`git status --short`).
- [ ] No new dependency, or the PR says why one is needed and it is in `requirements*.txt`.
- [ ] Documentation is updated where the behaviour changed, and no new **badge, screenshot or
      metric** claims something that was not actually run.
- [ ] If the dashboard markup changed: the screenshot in `docs/img/` was regenerated from a
      running server, or the PR says it needs regeneration.
