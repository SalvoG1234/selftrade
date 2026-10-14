# Security policy

This is a **local research tool**, not a service. Nothing here is exposed to the internet by
default: the dashboard binds `127.0.0.1`, it serves no remote assets, its session cookie is
`HttpOnly` + `SameSite=Strict`, and a non-loopback bind is refused while the bootstrap password
is still in force. What a security report is worth here is therefore narrow and specific.

## What counts as a vulnerability

1. **A way to send a real order.** Any path that reaches a real exchange or broker without
   `enabled`, `live` **and** `--live` (or without the MT5 two-key gate), or that places an order
   while the kill switch is engaged. The README claims this cannot happen; a counterexample is
   the most valuable report this project can receive.
2. **A way to move the sealed limits.** The risk caps are a frozen dataclass pinned by SHA-256;
   no config key, genome, environment variable or API call is supposed to reach them.
   `python -m selftrade risk verify` is the check (exit 0 = the seal holds).
3. **Credential exposure.** A value leaking out of the process: into a log, a report, an API
   response, the database, a recorded call, or the repository. This includes the dashboard's
   `GET /api/keys`, which is specified to return a **source and a fingerprint, never a value**.
4. **Authentication bypass.** Reaching `/api/*` (other than `/api/whoami`) without the session
   cookie when `dashboard.auth` is on, a cross-origin `POST` being accepted, a replayed cookie
   outliving its TTL, or a password change that does not sign other sessions out.
5. **A secret committed to the tree.** A database, key, token, session file or `state/` file
   that would be published. Report it even if it is only in history.

## What is not a security issue here

* The strategy losing money, or the holdout rejecting the champion. That is the recorded,
  intended result — see the front page of the README.
* The paper rail being simulated, the Binance rail being a read-only probe, or the absence of
  any order-sending code path in CI.
* A missing feature, a slow run, or a `N/D` where a number was expected.

## The Social Manager, which is a second surface

`socialmanager/` promotes this repository on GitHub, X and Instagram. It sends nothing on its own,
but it holds social tokens, exposes a second admin panel and talks to three APIs, so it has its own
version of the list above. Its detail is in [`docs/SOCIAL-MANAGER.md`](docs/SOCIAL-MANAGER.md) and
[`docs/SOCIAL-DEPLOY.md`](docs/SOCIAL-DEPLOY.md).

**What counts as a vulnerability there**

1. **A way to publish without the gate.** Anything that reaches a social API without passing all
   thirteen policy checks, or with the approval queue bypassed, or while the kill switch is engaged,
   or in `draft` mode. The policy result is stored per variant; a counterexample is a real report.
2. **A duplicate published.** The lease and the duplicate check are supposed to make a retry, a
   restart or two overlapping workers safe; the same post appearing twice is a bug worth reporting.
3. **A credential leaving the process** — into a log line, a prompt, an API response, the database,
   a recorded `api_call`, the dashboard or the repository. The panel and `token-check` are specified
   to return a **variable name and a one-way fingerprint, never a value**. A token sent to a host the
   response chose by redirecting, or to a plain-`http` endpoint, is the same finding — the three
   rules below exist so it cannot happen, and a counterexample is worth reporting.
4. **An LLM reaching a token.** The agents propose structured actions; only the publisher executes
   them, and no prompt may contain a credential.
5. **Panel authentication bypass**: reaching `/api/*` without a session, a cross-origin `POST` being
   accepted, a missing CSRF token being tolerated, the login rate limiter not limiting, or a
   non-loopback bind succeeding without a password configured.
6. **Writing to GitHub.** The connector is read-only by construction (a `GET`-only request helper,
   and GraphQL confined to `query`). Any path that could commit, comment, label, close or merge is
   the most valuable report this half can receive.

**What is not a social issue here**

* A draft that is merely poor in tone, or a connector that is off: everything ships disabled and in
  simulation, and `doctor` says which variables are missing.
* A metric shown as *unavailable*, or a platform limit quoted in the readiness report: those are the
  honest answer, not a defect.
* The absence of browser automation. Simulating a human in a browser to work around a missing API
  permission is deliberately out of scope, not a missing feature.
* No live X or Instagram call has been made from this repository: the connectors are covered by mocks
  and report `ready: false` until you configure them. That is a stated limitation, not a finding.

### The rules every outbound request has to pass

These are enforced in one place (`socialmanager/http.py`) rather than per connector, and each one
is the answer to a way a token leaves the machine without anyone deciding it should. They are
asserted directly in `tests/test_social_http.py`.

1. **No redirect is followed.** A `3xx` is returned to the caller as an answer. Chasing it would
   re-send the `Authorization` header to whatever host the response names, so a compromised or
   misconfigured endpoint could not harvest the token by answering `302`. The transport states the
   same default, so a caller that reaches it directly cannot opt out.
2. **No header carrying CR, LF or NUL.** A value with a line break — a token pasted with a trailing
   newline, a value read from a file — would inject a second header on the wire. It is refused with
   a message naming the header, before any connection is attempted.
3. **No clear-text request off the machine.** `https` is required for anything that is not loopback,
   so an `http://` `api_base` in `social.yaml` fails loudly instead of putting a bearer token on the
   network in the clear. Loopback stays allowed: a local proxy or a development server is a
   legitimate target and nothing crosses the network to reach it.
4. **The model calls enforce the same two rules, at their own call site.** The providers talk to an
   endpoint from configuration instead of through `HttpClient`, and the remote one sends an API key,
   so it refuses a non-loopback `http://` endpoint and never follows a redirect either. A local
   Ollama sends no credential and may therefore stay on plain `http` over the LAN.
5. **A failed request stays redacted.** The message recorded in `api_call` and shown on the Logs page
   passes through `secrets.redact`, so an error body that echoes a header cannot become a copy of the
   token, and the recorded `endpoint` is the path — never the query string, which could carry one.

### The store on disk is private data

`state/social.db` holds the audit log, the ingested feedback and the settings, so it is treated as
private rather than as a scratch file, and the claim is a reading instead of a promise:

* the file is **created `0600`** before SQLite opens it (creating it afterwards would mean the first
  moments already happened with the process umask), and the `-wal`/`-shm` siblings are narrowed with
  it because they hold the same rows;
* on open, group and other bits are **removed** — never added, so a store somebody made stricter
  keeps its mode — and `GET /api/health` reports `permissions` (`enforced`, `ok`, the per-file modes)
  so an operator can see the state rather than assume it;
* **`backup()` produces a copy with the same owner-only mode**, since the copy holds exactly what the
  store holds;
* narrowing happens **without writing to the audit log**: opening a store must not append to the
  chain, which is an invariant the tests and anyone reading the log both rely on;
* on Windows there are no POSIX mode bits to read; the file inherits the ACL of the profile it is
  created in and `permissions.enforced` reports `false` rather than pretending otherwise.

## How to report

Use GitHub's **private vulnerability reporting**: the repository's **Security** tab →
*Report a vulnerability*. That keeps the details out of public issues until there is something
to say. For a non-sensitive bug, a normal issue is fine.

Please include: the exact command or request, what you expected, what happened, and the output.
**Never paste a real API key, token or password.** If one has already been committed or shared,
revoke it at the exchange first and rotate it here (`state/secrets.json` is gitignored; an
exported environment variable overrides it) — then report.

## What this project does with the credentials you give it

The dashboard's **API keys** page can store the rail's key pair in `binance.key_file`
(`state/secrets.json` by default): written atomically, owner-only (`0600` where the platform
honours it), gitignored, and read **only after** an environment variable of the same name comes
up empty. It is **clear text** — an API secret has to be sent to the exchange, so it cannot be
hashed the way the dashboard login password is — and the page says so in one sentence, because
a security policy that buries that fact is the actual vulnerability.

A key sitting in that file arms nothing: `binance.enabled`, `binance.live` and `--live` still
gate every order, the sealed risk gate checks each one, and the kill switch can still refuse it.

## No response-time promise

This is a research project maintained in the open, without an on-call rotation. There is no
guaranteed acknowledgement window, and inventing one here would be the same kind of over-claim
the rest of this repository tries to avoid. Reports are read, and fixes to 1–3 above take
precedence over everything else.
