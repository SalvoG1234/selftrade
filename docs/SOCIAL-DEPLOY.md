# Deploying the Social Manager

The brief asks for a system that keeps working **when the operator's PC is off**. That is two halves:

- **Local (Windows 11)** — development, configuration, the dashboard, the optional local model, the
  tests. Nothing here is required at run time.
- **Cloud** — a worker that ticks on a schedule, the connectors, the persistent store, the reports.
  This half has no dependency on the machine on the desk.

Both halves run the *same* code through the *same* gates; the only difference is which machine calls
`python -m socialmanager tick`. That is deliberate: a safety layer that only applies in one
deployment is not a safety layer.

Files that make up the deployment kit:

| File | What it is |
| --- | --- |
| [`Dockerfile`](../Dockerfile) | the image: the social manager only, non-root, healthy, simulating by default |
| [`docker-compose.yml`](../docker-compose.yml) | the always-on worker, the dashboard, and `init`/`backup` one-shots |
| [`.env.example`](../.env.example) | every variable the system reads, with no values |
| [`.dockerignore`](../.dockerignore) | keeps `state/`, `.env` and the trading runtime out of the build context |
| [`.github/workflows/social-worker.yml`](../.github/workflows/social-worker.yml) | the serverless scheduler (inert until you switch it on) |

## Local

```powershell
python -m socialmanager init        # social.yaml + the database + the recorded accounts
python -m socialmanager doctor      # what works, what is missing, and why
python -m socialmanager sync        # read GitHub (read-only), index the docs
python -m socialmanager produce     # write bilingual drafts from real events
python -m socialmanager plan        # the editorial calendar
python -m socialmanager serve       # the dashboard on http://127.0.0.1:8788
```

`doctor` exits `1` while something genuinely needs configuring, which makes it usable as a check
rather than as decoration. `init` never overwrites an existing `social.yaml`.

## The container

```bash
docker build -t selftrade-social .
cp .env.example .env          # fill in what you have; an empty file still works, in draft mode
docker compose up -d --build
docker compose logs -f worker
```

What the image is, precisely: `python:3.13-slim`, a non-root user (uid 10001), the `socialmanager`
package, and exactly the two libraries it imports (`requests`, `PyYAML`). It does **not** contain
`selftrade/`, `requirements.txt`'s numpy/pandas, the tests or the docs, so a container physically
cannot place an order. `state/` is the only writable path and it is a volume.

Two services, plus two one-shots:

| Service | What it does | Default safety |
| --- | --- | --- |
| `worker` | `tick` every `SOCIAL_TICK_SECONDS` (300) while the machine is on | **simulates** unless `SOCIAL_TICK_LIVE=1` |
| `dashboard` | the admin panel, published on `127.0.0.1:8788` only | refuses to bind `0.0.0.0` without a password |
| `init` (`--profile tools`) | `docker compose run --rm init` | creates the database, records the accounts |
| `backup` (`--profile tools`) | `docker compose run --rm backup` | a consistent copy in the volume |

A tick is safe to interrupt and safe to repeat: jobs are leased in the database, the duplicate check
runs inside the publisher, and a container restarted mid-tick simply re-leases what is still due.

On a Linux host that bind-mounts `state/` instead of using the named volume, hand the directory to
the container's user once — `sudo chown -R 10001:10001 state` — because the process is not root by
design.

## The scheduler

**Recommended: the always-on worker above.** It is the only component that can publish unattended,
and it holds the whole state.

**Alternative: the scheduled workflow.** It is *inert until you enable it*: every scheduled run
exits immediately unless the repository variable `SOCIAL_CLOUD_ENABLED` is `true`
(Settings → Secrets and variables → Actions → Variables). A manual run from the Actions tab always
works, so you can try it first.

Two properties are deliberate and enforced by tests:

- **A scheduled run never publishes.** It ticks in simulation. Real publishing needs a manual
  dispatch with `live=true`, i.e. a human pressing a button.
- **The autonomy level starts at `draft`.** It is read from the `SOCIAL_MODE` variable.

The runner is ephemeral, so the database, the audit log and the generated media travel through the
Actions cache, and the drafts/reports are uploaded as artifacts on every run. A cache is not a
database: it can be evicted, and if it is, the audit chain restarts from the next run. Use the
always-on worker for a deployment whose audit trail must never break.

On a VPS, the same thing without Actions:

```cron
*/30 * * * * cd /srv/selftrade && docker compose run --rm worker python -m socialmanager tick >> state/worker.log 2>&1
```

## The database

SQLite, one file (`database.url`, default `state/social.db`), 13 tables, schema v1 applied on open
(`PRAGMA user_version`), UTC timestamps, uniqueness constraints on the platform-side ids.

**PostgreSQL is not implemented.** `database.url` accepts a `postgresql://` URL and `doctor` then
reports it as a *configuration problem* ("PostgreSQL needs an installed driver (psycopg); this build
ships SQLite only") rather than failing somewhere less obvious. The cloud half is designed around
that: a **single worker** writes, so SQLite is sufficient, and every state change goes through one
connection with a timeout. Moving to PostgreSQL is a real piece of work (a driver, a schema
translation and a transaction audit), not a URL change.

- **Backups**: `python -m socialmanager backup` (or the compose one-shot) writes a consistent copy of
  the database to `database.backup_dir` (default `state/backups`). SQLite's own backup API is used,
  so the copy is valid even while the worker is running.
- **Migrations**: forward-only, applied when the database is opened; `doctor` and `status` report the
  schema version. There is no downgrade path, which is why the version is printed.
- **Retention**: `database.retention_days` trims `api_call` (30 days) and `agent_run` (90 days).
  `audit_event` is `0` = **kept forever**: the audit chain is the record of what the system did, and
  pruning it would defeat the point.
- **Integrity**: `python -m socialmanager audit --verify` walks the hash chain and exits non-zero if
  any event was altered or removed. The scheduled workflow runs it every time.

## Health, logs and the stop button

| Check | Command | Meaning |
| --- | --- | --- |
| readiness | `python -m socialmanager doctor` | exits 1 while something needs configuring |
| liveness (container) | `status --json` (in the image's `HEALTHCHECK`) | the database opens and the settings load |
| the panel is listening | a TCP connect to `127.0.0.1:8788` (compose healthcheck) | the listener is bound and accepting |
| one screen of reality | `python -m socialmanager status` | modes, counts, kill switch, chain state, per-connector readiness |

The dashboard's `/api/health` sits **behind** the session on purpose: an authenticated operator
endpoint should not double as an unauthenticated probe from outside.

Logs are structured JSON lines (`obs.py`), written to stderr *and* to `state/social.log`, with a
secret-redaction pass over every line: a variable name can appear, a value cannot.

The **kill switch** is a stored setting (falling back to `autonomy.kill_switch` in the config), so it
survives a restart and is independent of the model, the autonomy mode and the dashboard being up:

```bash
python -m socialmanager killswitch engage     # nothing publishes, anywhere, until released
python -m socialmanager killswitch status
python -m socialmanager killswitch release
```

Choose your channels where the notifications are defined: `notify.enabled` plus
`SOCIAL_TELEGRAM_TOKEN` and `SOCIAL_TELEGRAM_CHAT_ID`. Notifications are **off by default**, and both
variables must be present for one to be sent; the configured events are errors, approval requests and
(optionally) policy blocks.

## Credentials you must provide

Nothing here creates an app, an account, a token or a page on your behalf. Every value below goes in
`.env` locally (gitignored) or in the platform's secret store in the cloud — never in this
repository, and never into a chat window.

| Variable | Required for | What happens without it |
| --- | --- | --- |
| `SOCIAL_DASHBOARD_PASSWORD` | binding the panel beyond loopback | loopback-only panel; a non-loopback bind is refused |
| `SOCIAL_GITHUB_TOKEN` | 5,000 req/h and Discussions | anonymous reads at 60 req/h, discussions skipped |
| `SOCIAL_X_CLIENT_ID` / `_SECRET` / `_ACCESS_TOKEN` / `_REFRESH_TOKEN` | X publishing and metrics | X stays in draft/simulation |
| `SOCIAL_IG_ACCESS_TOKEN` / `SOCIAL_IG_APP_SECRET` | Instagram publishing, comments, insights | Instagram stays in draft/simulation |
| `SOCIAL_LLM_API_KEY` | a hosted LLM behind an Ollama-compatible endpoint | — |
| `SOCIAL_LLM_REMOTE_API_KEY` | the optional remote provider | the deterministic templates write the drafts |
| `SOCIAL_TELEGRAM_TOKEN` / `_CHAT_ID` | notifications | no notification is sent (fine) |

In Settings → Secrets and variables → Actions, add the same names as **secrets** for the workflow,
plus the **variable** `SOCIAL_CLOUD_ENABLED=true` when you want the schedule to do something.

## Cost and quotas, before you turn anything on

- **X is pay-per-use**: about **$0.015** per post, about **$0.20** for a post containing a link, and
  about $0.001 per own-post read. The policy engine allows one link per post, and each publish
  records its expected cost in `api_call`.
- **Instagram** is free but capped: 400 containers per rolling 24 hours, a 24-hour container TTL, and
  a publishing quota that is read at runtime from Meta.
- **GitHub** is free within 60 req/h anonymous or 5,000 req/h with a token.
- **A local Ollama** is free and private. The remote provider is used only when it is both explicitly
  allowed (`llm.allow_remote_fallback`) and configured: no silent fallback, so a post is never
  written by a model the operator did not choose.

## Troubleshooting the usual first failures

| Symptom | Cause and fix |
| --- | --- |
| `refusing to start: a non-loopback bind needs a password` | `SOCIAL_DASHBOARD_PASSWORD` unset — that refusal is the feature, not a bug |
| `doctor` says X/Instagram `connector disabled in social.yaml` | `connectors.<name>.enabled: true` and a token; both are needed |
| Instagram: `public_media_base_url is empty` | Meta fetches the image over HTTPS itself; a local path cannot publish |
| `llm.unreachable` in the log | Ollama is not running. Drafts are still written by the templates, and the item says which generator wrote it |
| the worker container logs "0 job(s) handled" | nothing is due yet: `produce` creates drafts, `plan --schedule` queues them |
| `publish` reports `blocked` | read the reason: the policy report is stored per variant (`show <id>` prints it) |
| a metric shows as unavailable | the API did not return it (X metrics expire after 30 days; album members have no carousel insights) |

## See also

- [SOCIAL-MANAGER.md](SOCIAL-MANAGER.md) — the components, the 13 policy checks, the autonomy modes.
- [SOCIAL-CONNECTORS.md](SOCIAL-CONNECTORS.md) — per-platform setup, scopes, limits and prices.
