# The Social Manager

An AI system that promotes **this** repository — `SalvoG1234/selftrade` — on GitHub, X and
Instagram, in Italian and English, without inventing a single number, and without ever being able
to publish something the deterministic gate has not passed.

It is a second, separate program inside this repository: `socialmanager/` (~11,200 lines, with a
suite of its own — 235 cases in 11 modules) alongside the trading runtime it talks about. It does not import `selftrade`, cannot
place an order, and cannot write to a repository, an issue or a discussion — see
[the connectors](SOCIAL-CONNECTORS.md) for why that is structural rather than a promise.

- **Deployment** — the container, the scheduler, the database and the credentials you must insert:
  [SOCIAL-DEPLOY.md](SOCIAL-DEPLOY.md).
- **Platform setup, scopes and limits** — [SOCIAL-CONNECTORS.md](SOCIAL-CONNECTORS.md).
- **What it produced, as drafts** — [social-samples/drafts.md](social-samples/drafts.md) and the
  calendar in [social-samples/calendar-initial.json](social-samples/calendar-initial.json).

## What it is not

| Not this | Because |
| --- | --- |
| An auto-poster | Draft mode is the default. Publishing needs the connector to be enabled, a real token, `dry_run` off, the policy engine to pass, and — for anything sensitive — a human approval. |
| A growth tool | No mass follower, no engagement for engagement's sake, no reply to a stranger to be noticed. `autonomy.allow_replies` is `false` and replies need a human in every mode. |
| A translator | Every post is written per platform and per language from the same facts. Nothing is machine-translated post-by-post. |
| A source of numbers | Every claim in a post traces to a stored fact with a URL. A number in the copy that no source contains is flagged, and the item goes to review. |
| Verified against live accounts | **No live X or Instagram call has ever been made from this repository.** The connectors report `ready: false` until you configure them, and say so in the dashboard. |

## The pieces, and what each one decides

| Component | File | The decision it owns |
| --- | --- | --- |
| Orchestrator | [`socialmanager/orchestrator.py`](../socialmanager/orchestrator.py) | sequences a run, owns the transaction boundaries, and is the only thing the CLI and the dashboard talk to |
| GitHub Connector | [`socialmanager/github.py`](../socialmanager/github.py) | reads releases, commits, issues, discussions and docs; **structurally** read-only (every request goes through a `GET`-only helper) |
| Content Agent | [`socialmanager/content.py`](../socialmanager/content.py) | turns stored events into briefs, then into one variant per platform and language; refuses to write without a fact |
| Visual Generator | [`socialmanager/visuals.py`](../socialmanager/visuals.py) | real PNG cards (5×7 bitmap font + zlib, so no image dependency) and Reel storyboards as SVG frames |
| Editorial Planner | [`socialmanager/planner.py`](../socialmanager/planner.py) | the calendar: slots, weekly targets, category share, min gap, quiet hours. It never invents an event to fill a slot |
| Publisher | [`socialmanager/publisher.py`](../socialmanager/publisher.py) | the **only** component that publishes; re-checks the policy result, the approval, the duplicate and the lease before each call |
| Community Agent | [`socialmanager/community.py`](../socialmanager/community.py) | classifies feedback, clusters recurring requests, drafts replies, proposes issues only when allowed |
| Analytics Agent | [`socialmanager/analytics.py`](../socialmanager/analytics.py) | reads back the metrics the APIs actually return, and states "unavailable" where they do not |
| Policy Engine | [`socialmanager/policy.py`](../socialmanager/policy.py) | 13 deterministic checks before any write (below) |
| Approval Manager | [`socialmanager/approvals.py`](../socialmanager/approvals.py) | the queue, and the seven cases that always need a human |
| Notification Service | [`socialmanager/notify.py`](../socialmanager/notify.py) | Telegram, off by default; errors and approval requests only |
| Dashboard | [`socialmanager/dashboard.py`](../socialmanager/dashboard.py) | the admin panel: 13 pages, password + CSRF + same-origin on every mutation |
| Persistence | [`socialmanager/db.py`](../socialmanager/db.py) | SQLite: 13 entities, stable ids, UTC timestamps, uniqueness constraints, append-only audit chain |

Two supporting modules are worth naming because they carry the security story: `secrets.py` (the
only reader of environment variables — a value never enters a log, a prompt or the database) and
`http.py` (retries with backoff, per-connector rate limiting, an `api_call` row for every request).

## The gate, in order

```
GitHub ──► events            ┌─ policy: 13 checks ─┐
           │                 │  kill switch        │
           ▼                 │  platform allowed   │
      content brief ──► draft│  account ready      │
                           │  autonomy mode      │
                           │  approval           ├─► BLOCK  (never published)
                           │  facts and sources  │
                           │  format and length  ├─► REVIEW (waits for a human)
                           │  no secrets or PII  │
                           │  financial language ├─► PASS ──► Publisher ──► platform
                           │  platform rules     │             │
                           │  duplicate          │             └─ duplicate + lease, again
                           │  frequency          │
                           └─ language declared  ┘
```

A **check that raises is treated as `review`, never as `pass`**: failing safely is the default, so a
bug in a rule can stall a post but cannot wave one through.

## The three autonomy levels

| Mode | What the AI does alone | What still stops |
| --- | --- | --- |
| `draft` (default) | writes items, variants, replies and the calendar | everything: nothing is published |
| `controlled` | publishes ordinary, policy-passing content within the calendar's limits | anything `high`/`sensitive` risk, approval-required categories, replies |
| `advanced` | additionally adapts the calendar and acts on feedback within the configured limits | the same seven cases, and any change to the limits themselves |

Set it in `social.yaml` (`autonomy.mode`) or per run with `SOCIAL_OVERRIDES=autonomy.mode=controlled`.

## What always needs a human

Seven cases, implemented as `FORCED_REASONS` in `approvals.py`, regardless of mode or risk:

1. **security** — a security question is involved;
2. **controversy** — the content answers criticism or takes a position;
3. **financial** — a performance or capital statement (the policy engine flags every performance term);
4. **commitment** — the reply could commit the project to something;
5. **permissions** — the change touches permissions or credentials;
6. **destructive** — the operation destroys data or history;
7. **autonomy** — the change modifies the autonomy limits. An agent can never widen its own freedom,
   and the audit event records the old *and* the new value.

Replies are held for a human in **every** mode until `autonomy.allow_replies` is turned on, and
issue proposals stay off until `autonomy.allow_issue_proposals` is.

## The command line

Every operation the dashboard performs is also a command, and every command goes through the same
gates: this is what cron, the container and the scheduled workflow call, so it must be impossible to
script around the safety layer.

| Command | What it does |
| --- | --- |
| `init`, `doctor`, `status` | create the database, check the environment, print one screen of reality |
| `sync` | read GitHub (read-only), index the docs, ingest feedback |
| `produce [--limit N]` | build briefs and write bilingual variants |
| `drafts`, `show <id>`, `policy` | inspect; `policy` re-runs the 13 checks over stored drafts |
| `plan [--schedule]`, `calendar [--initial]` | the calendar, and optionally the `scheduled_post` rows |
| `approvals`, `approve <id>`, `reject <id>` | the queue, and the two decisions |
| `publish [--live]`, `tick [--live]` | publish or **simulate** (the default), and the worker tick that leases due jobs |
| `feedback`, `analytics [--export json\|csv]` | classify feedback, read back metrics |
| `killswitch engage\|release\|status` | the stop button, independent of the model |
| `accounts`, `connectors`, `token-check` | what is configured, by variable **name** and fingerprint, never by value |
| `settings`, `audit [--verify]`, `backup`, `logs`, `export` | settings, the append-only log and its chain, backups, exports |
| `serve [--host H] [--port P]` | the dashboard (a non-loopback bind is refused without a password) |

## The dashboard

Thirteen pages on `http://127.0.0.1:8788`, sharing one design system: a dark theme by default and an
optional light one from the top bar, remembered per browser. The rail groups them by what the
operator is doing - **Panoramica** (Overview, **Guida operativa**), **Pianificazione** (Editorial
Calendar), **Studio contenuti** (Content Studio), **Approvazioni** (Approval Queue, Published Posts),
**Community e feedback** (Community Inbox, Feedback Intelligence), **Analytics e report** (Analytics),
**Agenti e automazioni** (Agent Activity, Social Accounts) and **Registro e impostazioni** (Logs &
Audit, Settings). Grouping is presentation only: the page ids and the labels are still what the
guide, the command palette and the suite key off, so a group never renames or hides a page.

It shows what exists and says "unavailable" where the platforms have not returned data — it does not
fill a chart with a plausible-looking number. Sessions are password-based (rate-limited, with a
stored hash, never the password itself), every mutation checks the CSRF token and the origin, and a
non-loopback bind is refused unless a password is configured.

### The operational guide, in the panel

The **Guida operativa** page is the answer to "what do I do now, and in what order". It is not a
manual kept next to the code: it is a thirteen-step daily cycle served by `GET /api/guide` that
covers **every screen** (each step links to the page it is talking about; a page added to the rail
without a step fails the suite), and **every line of it carries a measurement read at that moment** —
the mode and the kill switch, whether the sources were ever synced, how many drafts are unscheduled,
how many slots of the week are filled and on which platforms, how many approvals are waiting and how
many of them are high-risk or sensitive, what was really published versus simulated versus still
unverified, the unread feedback, the recorded API errors and failed runs, which connectors are not
ready and why, how many environment variables are present (by name), how much recurring feedback is
waiting to become an issue proposal, and whether the audit chain is intact.

Each step also carries a state derived from those same readings (`ok` / `todo` / `attention` /
`blocked`), so the page shows where the day is stuck instead of a wall of equal-looking cards. On an
empty store it says *mai eseguita* and *nessuna richiesta in attesa* rather than showing a
plausible-looking example it does not have — the test suite asserts exactly that, in both directions
(`test_the_guide_is_a_daily_cycle_whose_lines_are_measurements`,
`test_the_guide_counts_what_is_really_queued_and_follows_the_kill_switch`).

The coverage is guarded rather than promised: the test reads the page ids **out of the served
document** (not out of a list in the test file) and fails when the rail has a page no step points
at, and when a step points at a page that is not in the rail. A second test proves that guard is not
vacuous by injecting a new page into the real rail and watching it be reported as undocumented.

### Working in it every day

The pages are built for the operator who opens them twice an hour, not for the screenshot:

- **The automatic refresh never discards unsaved work.** The page polls every 30 seconds. While a
draft is dirty, or a textarea has focus, the poll updates the mode/kill-switch badges and the
freshness line and *leaves the page alone*; the top bar reads *modifiche non salvate*. Before this,
a 30-second tick in the Content Studio threw away whatever had just been typed.
- **The approval queue takes a batch.** Filter by state, by risk and by free text (title, reason or
`#id`), tick a selection or the whole filter, add one note, then approve or reject in a single
gesture. It loops the single-decision endpoint on purpose: every approval keeps its own audit row and
its own note, so a batch stays reviewable one item at a time.
- **Filters and selection survive a redraw.** They live in the page's state, not in the markup, and
the controls are re-synced from that state — so the dropdown always shows what the table is actually
filtered by.
- **Command palette and keyboard.** `Ctrl/Cmd+K` opens a palette of the thirteen pages plus the common
actions (sync, produce, plan, simulated tick, collect metrics, global pause). `/` focuses the search
field, `r` refreshes, `g` then a letter jumps to a page, `Esc` closes, `?` lists the shortcuts.
- **The risky buttons ask first.** `Pubblica (live)` and the global pause show a confirmation that
names the platform and the consequence; cancel really cancels (verified: nothing was published).
- **Freshness is on screen.** The top bar says how many seconds ago the state was read and turns
amber when it is stale or the server did not answer, and a chip shows the pending-approval count that
links straight to the queue.
- **The rail groups by task, and gets out of the way.** Below 1080px it becomes a drawer over the
page it is navigating to, dimmed by a scrim and closed by its `✕`, by `Esc` or by a tap outside,
with `aria-expanded` on the handle; below 900px the top bar wraps into two rows instead of pushing
its own controls off the viewport.
- **It is usable from the keyboard alone.** Every control has a visible focus ring rather than the
browser default, the content is one *Salta al contenuto* skip-link away, the current page carries
`aria-current="page"`, the freshness line is a `role="status"` live region, and
`prefers-reduced-motion` turns every transition off.

## Facts, sources and the honesty rules

- Every post carries the list of claims it makes; `show <id>` prints them with their URLs.
- A number in the body that no stored fact contains is detected (`unsupported_numbers`) and the item
  is escalated to review.
- Historical results, simulations, paper trading and live results are distinguished in the copy the
  templates write — and the templates never promise a profit.
- No engagement, follower, star or user is ever invented: a metric that the API did not return is
  stored and displayed as unavailable.
- Nothing is presented as shipped before it is: a test that has not run is not "passing", and a
  planned feature is not described as available.

## Tests: what was actually run

The social manager's own suite is **235 cases in 11 modules**, all passing, and it runs inside the
repository's suite (1133 tests in 47 modules) — the number the documentation ledger measures and CI
enforces:

| Module | Tests | What it pins down |
| --- | --- | --- |
| `test_social_dashboard.py` | 55 | the panel over real HTTP: login, CSRF, same-origin, the thirteen pages, the operational guide (including a guard that fails if a rail page has no step), the batch-approval audit trail, the kill switch button |
| `test_social_policy.py` | 29 | every one of the 13 checks, including the failing-safe path and the duplicate rule |
| `test_social_content.py` | 23 | facts, both languages, the fallback when no model is reachable, truncation, unsupported numbers |
| `test_social_db.py` | 26 | schema, constraints, retention, the audit chain, backup/restore, and the private-store permissions (the mask rule on every platform, the POSIX file modes) |
| `test_social_http.py` | 17 | the shared client's own security behaviour: no redirect chased, no header with CR/LF, no clear-text endpoint off the machine, retries, and the redacted `api_call` record — plus the one model call that carries an API key |
| `test_social_deploy.py` | 15 | Dockerfile, compose, `.env.example` and the workflow against the code they deploy |
| `test_social_community.py` | 15 | classification, clustering, reply gating, issue proposals |
| `test_social_github.py` | 14 | read-only enforcement, noise filtering, rate limits, missing token, discussions off |
| `test_social_planner.py` | 14 | slots, share, gaps, quiet hours, `--schedule` idempotence |
| `test_social_publisher.py` | 14 | double publish, retry, mode B approval, failed call, kill switch mid-flight |
| `test_social_config.py` | 13 | defaults, overrides, unknown keys, the refusals |

```text
python -m pytest tests/test_social_*.py -q      # the social manager's own 235 cases: green
python -m pytest -q                            # 1133 passed (the whole repository)
python -m selftrade verify --ledger            # every count in the docs equals the measurement
```

**Every API call in those tests is a mock.** `tests/fake_social.py` drives a `FakeTransport` that
routes by URL and records every request, so a test that accidentally tries the network fails instead
of posting; there is no path from the suite to a real account. Consequently the suite proves the
*safety* properties (nothing publishes without a pass, no duplicate, no unverified claim, no
credential in a log) and the *shape* of each call — it does not prove that X or Instagram accept
them. Nothing here should be read as a claim about a live account.

The one part that has run against the real world is the GitHub connector, read-only and anonymously:
`sync` fetched 44 sources and indexed the repository's own docs into `state/social.db`, which is
where the sample drafts came from.

## What you have to insert yourself

Nothing here creates an app, an account or a token on your behalf, and no credential belongs in this
repository. The full list, with what each one unlocks, is in
[SOCIAL-DEPLOY.md](SOCIAL-DEPLOY.md#credentials-you-must-provide). Short version: all of them are
*optional* except the dashboard password, which is required only if you bind the panel beyond
loopback. In draft mode an empty `.env` is a complete, working configuration.

## Known gaps, stated plainly

- **No live publishing has been verified.** The X and Instagram connectors are written against the
  current endpoints and fully covered by mocks; they have never been exercised with a real token
  from this repository.
- **SQLite only.** `database.url` accepts a PostgreSQL URL but no driver is bundled: configure one and
  `doctor` reports it as a problem rather than pretending. See
  [SOCIAL-DEPLOY.md](SOCIAL-DEPLOY.md#the-database).
- **Reels are storyboards, not video.** The generator writes the script and the SVG frames; it does
  not encode a video, and it does not pretend to.
- **X posts with a link cost roughly 13× a plain one** (about $0.20 vs $0.015 at the current
  published rates) — `estimate_cost` records it per post, and the policy engine allows one link.
- **The image font is a 5×7 bitmap.** Cards are real PNGs; a headline longer than the card gets
  wrapped by the generator's own logic, not by a typography engine.
- **Feedback is only what the APIs expose.** X mentions and Instagram comments on own media, no
  scraping, no crawling, no browser automation.
