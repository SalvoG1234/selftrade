# Maintaining this repository

The things below cannot live in a file: they are settings on GitHub, and only the owner can set
them. This page is the checklist, with the exact text where text is needed, so the repository
does not depend on someone remembering what was decided.

Everything else — CI, templates, docs, screenshots — **is** in the tree, and the tests
(`tests/test_docs.py`) keep the links and the index honest.

## About

**Description** (the one-line text under the repository name, ~120 characters):

```
Self-improving trading-strategy search for Windows 11: walk-forward + holdout, a sealed risk layer, paper rail, honest losses.
```

**Website**: leave empty unless there is a stable URL. Pointing at a raw file in the repository is
worse than nothing.

**Topics** (paste as-is; they are the ones this project can actually justify):

```
algorithmic-trading
quantitative-finance
backtesting
walk-forward
python
windows
dashboard
risk-management
paper-trading
research
experimental
```

Deliberately **not** in the list: `profitable`, `passive-income`, `signal`, `crypto-bot`,
`ai-trading` as a promise. A topic is a claim about what the repository is, and this one is a
research experiment whose shipped result is a loss.

## Social preview

`docs/img/social-preview.png` (1280×640) is rendered from `docs/img/social-preview.html`, which is
in the tree so the card can be re-rendered instead of re-invented:

```powershell
python -m selftrade verify --card --write    # draw it, and record what it was drawn from
python -m selftrade verify --card            # the check CI runs, and it needs no browser
```

`--write` draws the source with the first Chromium it can find (name one with `--chrome` or
`$SELFTRADE_CHROME`) and writes a record *into the file*: the count, the SHA-256 of the HTML it was drawn
from, the browser and the date. `verify --card` then compares that record with the tree - the count
against the measured suite, the digest against the source as it is now - so repairing the documents
and leaving the picture alone fails the build instead of shipping last week's number on the front
page. `verify --card --render` draws the source again in CI and requires 1280x640 and today's count
in the rendered text; it is not a byte comparison, because rasterisation is not portable between
machines or browser releases - and, measured rather than assumed, not reproducible even on one
machine with one browser (two drawings of this source differed in 173 060 bytes here), so the byte
difference is reported and never fails a build.

Upload it under **Settings → General → Social preview**. It carries the test count and the
platform names — both facts this repository proves — and no performance claim.

## One-time settings

| Setting | Where | Why |
| --- | --- | --- |
| **Discussions** enabled | Settings → Features | the issue forms point questions there (`design_feedback.yml`, `SUPPORT.md`) |
| **Private vulnerability reporting** enabled | Settings → Security | `SECURITY.md` tells reporters to use it; without it they have no private channel |
| **Dependabot alerts + security updates** | Settings → Security | `.github/dependabot.yml` only covers version updates |
| **Secret scanning + push protection** | Settings → Security | the strongest available guard for the one thing this project promises never to publish |
| **Branch protection on `master`** | Settings → Branches | require the `tests` workflow before merge, once there is more than one contributor |
| **Milestones** | Issues → Milestones | the ten titles in [`ROADMAP.md`](ROADMAP.md) are written to be pasted |

## Things that must be true before a release is tagged

Nothing has been released yet, and the changelog says so. When the first tag happens, it should be
true that:

1. CI is green on the exact commit being tagged, on Windows and Ubuntu (all three Python versions).
2. `python -m pytest` passes on a **clean** worktree, so the recorded numbers carry
   `reproducible: 1` rather than `0`.
3. `docs/VERIFICATION.md` has a dated row for the suite and for the numbers the README quotes.
4. `CHANGELOG.md` has a dated section instead of `Unreleased`, and `selftrade.__version__` matches
   it.
5. The screenshots in `docs/img/` were regenerated from a build of that commit.

## Housekeeping that is deliberate

* **Nothing is hiding in the index.** The agent-workspace directories `.claude/` and `.freebuff/`
  are gitignored, and they were never committed: `git ls-files .claude .freebuff` returns nothing
  on `master`, so there is no stale entry to remove. The rule that keeps it that way is the first
  block of `.gitignore`, and `git status --short` after a session is the check.
* **`state/` and `reports/` never appear in a commit.** They hold the database, the credentials
  file, the kill switch and the dashboard password hash. `tests/test_secrets.py` and
  `tests/test_docs.py` are what keep that honest, together with the `git status --short` line in
  the pull-request checklist.
* **The badge on the front page is live.** It reflects the last run of the `tests` workflow, so it
  is not a claim that has to be maintained by hand — but a red badge is information, not a bug to
  hide: the exception is a flaky *host*, never a muted test.
