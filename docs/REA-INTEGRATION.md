# REA integration

REA is an **information source, not an execution source**. It is used to learn
*how* a feed, an indicator or a contract actually computes something. The
evidence — including its limits — is stored in memory and fed into the proposal
prompt, so the search reasons from facts rather than guesses.

With REA disabled (`rea.enabled: false`, the default) the whole project works.

## Rules

1. A target is only analysed when it declares `authorized: true`. Targets
   without it are listed as **BLOCKED** and are never executed.
2. `dry_run: true` is the default. With dry run, the exact command that *would*
   run is printed and **nothing is executed**, even with `--live`.
3. Only analyse artifacts you are permitted to analyse.
4. Evidence is stored as a lesson tagged `rea`, carrying the question and the
   target. Failed runs store nothing.

## Config

```yaml
rea:
  enabled: true
  dry_run: true
  node_cmd: npx
  package: rea-agents@latest
  provider: ghidra
  ghidra_dir: C:\tools\ghidra_12.1.4_PUBLIC
  java_home: C:\tools\jdk-21
  targets:
    - name: eurusd-feed
      path: C:\artifacts\feed.exe
      question: how is the close price computed?
      authorized: true      # required
```

## Commands

```powershell
.\run.ps1 rea doctor
.\run.ps1 rea plan
.\run.ps1 rea run            # dry run (rea.dry_run=true)
.\run.ps1 rea run --live     # executes only if rea.dry_run=false
```

`rea plan` shows, per target, whether it would run or is blocked. `rea run`
reports the commands, the exit codes and, for successful runs, stores the
evidence.

The evolution loop reads stored REA evidence and includes it in the proposal
prompt (`propose.build_user_prompt(evidence=...)`). `evolve --no-rea` ignores
it for that run.

## Testing

The REA runner is injectable:

```python
rea.run(settings, memory, live=True, runner=lambda cmd: (0, "evidence text"))
```

so the authorization gate, the dry run and evidence storage are all covered
offline in `tests/test_rea.py`.
