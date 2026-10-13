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
