# The session file, its lock, and the soak that runs the two of them together

`session.py` holds one small JSON file that the evolution loop writes at every
generation boundary and the operator writes from somewhere else — the
dashboard's buttons, `session stop` on the command line. Both sides do a
**read-modify-write**, so without coordination the loop can write back the
snapshot it read *before* a `stop` arrived. The click then does nothing, which
is the one failure a control panel must not have.

Every write therefore happens under a cooperative lock: the *existence* of
`<session file>.lock`, created with `O_EXCL`. No dependency, no server. Nothing
here may wedge a run — a lock left behind by a dead process is broken after 30 s,
and a holder that outlives 2 s is written past rather than waited on.

That is simple enough to state and easy enough to get wrong. It was wrong twice,
both times on Windows, and the module below is how the second one stays fixed.

## The two ways a write happened beside the lock

| what happened | why it lost work |
| --- | --- |
| `os.open(O_CREAT\|O_EXCL)` on the lock file answers a **lost create race** with `EACCES`, not `EEXIST`. The code read `EACCES` as "this directory is not writable", carried on **without** the lock, and two unlocked read-modify-writes is exactly how a `stop` disappears. | the operator's request is overwritten by a writer that never saw it |
| `os.replace(tmp, dest)` fails while another handle holds `dest` open without share-delete — which is the handle `Path.read_text` takes, and the dashboard reads this file on a timer **from another process**. | a prepared state write was thrown away whole |

The second fix is a bounded retry of the rename, and only for that error. The
first fix is a one-line change of meaning with a long comment, because it is the
kind of line that gets "simplified" back: the error from a lock create must be
read as *held*, never as *unavailable*.

## The soak

`lockstress.py` runs the real `Session` the way the defect happened, counts what
it saw, and can prove its own teeth.

```console
$ python -m selftrade lock-stress                 # the lock as shipped
$ python -m selftrade lock-stress --legacy        # the pre-fix reading
$ python -m selftrade lock-stress --compare       # both, and whether it means anything
```

Each **round** starts from a fresh state file. Several writers heartbeat it at
the loop's cadence while an operator writes `stop`, waits for the next writer to
land, and reads the file back — as many times per round as `--cycles`. A `stop`
that is not there any more is a lost request. Everything happens in a private
temporary directory; the soak never touches `state/session.json`.

Counters reported: requests written and overwritten, writes performed **without
the lock**, writes that **overlapped another writer on the same temporary file**,
creates that found the file already there, creates the *operating system itself*
denied, races injected, renames retried, reads that could not be taken at all.

The same command also runs in CI on both platforms, after the suite, so the lock is
exercised the way it failed and not only one rule at a time.

## The injection, exactly

A lost create race is a few microseconds wide. Left alone it fires now and then
— the soak measures it, see below — but "now and then" is not something a tool
can wait for, so it is injected at `--race-share` (default 15% of lock creates).
The injection is faithful rather than arbitrary:

* it fires **only while another writer really holds the lock**, which is the one
  moment a create can be lost — and therefore the moment the holder is about to
  release it, so a writer that *waits* is never left waiting for a lock nobody
  owns;
* it raises what Windows raises: `PermissionError` with `errno` 13 and
  `winerror` 5 (`ERROR_ACCESS_DENIED`), so the code under test cannot tell the
  injected event from the real one;
* the rename is injected the same way with `winerror` 32
  (`ERROR_SHARING_VIOLATION`) — what a reader holding the destination open
  causes on Windows.

## The pre-fix reading is kept as a control

`LegacyLockSession` is the `_locked` of the release before the fix: a create that
failed with `EACCES` meant "carry on without the lock". Nothing in the
application uses it. It exists so that a clean report can be printed beside a
counter that *moved* without the fix:

```
Lock soak comparison: validated
  Lock soak (the lock as shipped): 15 rounds, 3 writers x 3 stop-cycles
    (45 checks, 761 writes, 282 create races injected, 49 renames violated) -> clean
  Lock soak (before the fix  ):    15 rounds, 3 writers x 3 stop-cycles
    (33 checks, 273 writes, 50 create races injected, 17 renames violated) -> defect
  the same injection, two readings -> stops lost 0 vs 2, writes without the lock 0 vs 53
```

`--compare` exits **0** only when the shipped lock is clean *and* the pre-fix
reading is not. Two clean runs are reported as **inconclusive**, not as a pass: a
soak whose injection never bit proved nothing, and saying otherwise is the kind
of green light this tool exists to refuse. `--legacy` alone exits **0** when it
reproduces the defect; the plain soak exits **0** clean, **2** on a regression.

## What it measured

| run | result |
| --- | --- |
| `lock-stress` (default) | 15 rounds, 968 writes, **365 create races injected**, **3 creates denied by the operating system itself**, 0 stops overwritten, 0 writes beside the lock, 0 writes overlapping on the temporary file, 9.7 s |
| `lock-stress --compare` | shipped **clean** (45 checks) vs pre-fix **defect** (33 checks, 2 stops lost, 53 writes without the lock, plus writers colliding on one temporary file); 28 s total |
| the injection off (`--race-share 0`) | still **clean** — the natural race is met and survived, and the count of creates the OS itself denied is reported rather than confused with an injection |
| the CI step, both platforms | Windows: 505 writes, 45 races injected, **1 create denied by the operating system itself**, clean in 1.7 s. Ubuntu: 783 writes, 53 races injected, **0 denied by the operating system itself**, clean in 1.1 s — the natural race is a Windows one, and the injection is what exercises that path on Linux at all |

The natural figure is worth reading twice. On Windows, with three writers
hammering one lock, the operating system denies a create by itself a few times in
a few thousand — because the ~0.02 % figure quoted when the fix landed was
measured on a much lighter loop. The defect was not a theoretical race with a
photographic finish; it was something a busy session could hit in a day. On Linux
it is not a few in a few thousand, it is **none**: the same soak, the same load,
zero creates denied by the kernel, which is why the injection is not a
convenience but the only thing that takes that path on a Linux runner.

## What this deliberately does not do

* **not a substitute for the unit tests.** `tests/test_session.py` states each
  rule once, deterministically, and fails with a named assertion. The soak is the
  instrument beside them: it is *probabilistic* by nature (the counters move
  between runs; `--seed` seeds the injection, not the schedule), so it is used to
  look for what nobody thought to assert — not to be the assertion.
* **threads, not processes.** The lock is the existence of a file created with
  `O_EXCL` against a shared path, which the operating system serializes whatever
  the callers are; the errno semantics this tool exists to check do not change at
  a process boundary. A cross-process variant would be a different harness with
  different counters, not a stricter one.
* **not a benchmark.** The timings above are one machine's, and the pre-fix run is
  slower because it retries and fails, not because the fix made the loop faster.
