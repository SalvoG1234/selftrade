# Connectors, permissions and limits

Three connectors, one rule: **the Social Manager reads with the API and publishes with the API, and
never simulates a human in a browser.** No scraping, no automation of the web UI, no session
cookies, no third-party wrapper service. Where an API does not offer something, the feature is
reported as unavailable rather than worked around.

The current state of all three at once:

```text
python -m socialmanager connectors      # readiness, scopes, limits, prices, what is missing
python -m socialmanager token-check     # which variables are set, by name and fingerprint
```

| Platform | Purpose | Default | What it needs from you before it can publish |
| --- | --- | --- | --- |
| GitHub | source of truth: releases, commits, issues, discussions, docs | reads anonymously | nothing (a token only raises the rate limit and unlocks Discussions) |
| X | posts, media, metrics, mentions | disabled, `dry_run: true` | app + user-context token, `connectors.x.enabled: true`, `dry_run: false` |
| Instagram | images, carousels, Reels, comments, insights | disabled, `dry_run: true` | professional account, App Review, long-lived token, a public HTTPS media base, `dry_run: false` |

The character ceilings below are the platforms', not this project's opinion of them, and there is one
copy of each: `models.PLATFORM_LIMITS` declares them, `policy.py` refuses a draft that breaks one, and
the Content Studio measures the draft against the same numbers before it draws the preview and before
an approval is accepted ([`preview.py`](../socialmanager/preview.py)).

## GitHub — read-only, by construction

The mission forbids the Social Manager from touching code, merges or issue administration. That is
enforced in the connector rather than promised in this document: every request goes through an
internal `_read` helper that **refuses any method other than `GET`**, and the single `POST` in the
file is GitHub's own GraphQL endpoint, which only ever sends a `query` document — a `mutation` is not
accepted. There is no code path here that can commit, merge, comment, label, close or delete.

What it collects, and why each kind exists:

| Kind | Why the content engine cares |
| --- | --- |
| `release` | the only kind that reliably means "something shipped" |
| `commit` | development activity, filtered to messages that carry news (`bump`, `merge`, `wip`… are dropped) |
| `issue` | questions and bug reports — also where feedback starts |
| `discussion` | opt-in, needs a token; usually the richest technical conversation |
| `readme` / docs | what the project currently *claims*, so the copy cannot contradict it |

**Permissions.** Nothing is required. Anonymous reads are limited to 60 requests/hour per IP. If you
want Discussions, or a comfortable limit, create a classic token with exactly `public_repo` — or a
fine-grained token with at most *Contents: read*, *Issues: read*, *Discussions: read* (which is
`read:discussion` in the classic scope) — and set `SOCIAL_GITHUB_TOKEN`.

**Discussions stay off** unless `github.discussions: true` *and* a token is present; a missing read
permission is reported as unavailable, not as an empty list.

**What it will never do**, even when feedback clearly deserves it: open an issue. Recurring feedback
can be *proposed* as a draft issue body (`autonomy.allow_issue_proposals`, off by default, and never
auto-posted), and duplicates are checked first.

## X

Written against the endpoints that are current in late 2026 — and deliberately *not* against
`v1.1 statuses/update`, the command-style `POST /2/media/upload` flow with `command=INIT/APPEND/FINALIZE`,
the deprecated filter stream, or OAuth 1.0a. All four are still mentioned in the wild and all four
are wrong for a new integration; every request is asserted against an allow-list
(`CURRENT_ENDPOINTS`) so a future edit cannot quietly reintroduce one.

### Authentication

**OAuth 2.0 Authorization Code with PKCE, user context.** The app-only bearer token is read-only and
cannot post, so the connector never pretends it can.

| Variable | What it is |
| --- | --- |
| `SOCIAL_X_CLIENT_ID` | the app's client id |
| `SOCIAL_X_CLIENT_SECRET` | the app's client secret (used by the token exchange) |
| `SOCIAL_X_ACCESS_TOKEN` | the **user-context** access token — the one that can post |
| `SOCIAL_X_REFRESH_TOKEN` | with `offline.access`, what keeps a long-running worker alive |

Scopes: `tweet.read`, `tweet.write`, `users.read`, `media.write`, `offline.access`.

**The authorisation exchange is a manual step.** This CLI does not perform it: you authorise the app
in the developer portal, complete the PKCE flow, and export the resulting token yourself. The
readiness report says exactly that — it used to point at a helper command that does not exist, which
is worse than a manual step, and the test suite now fails if a message ever names a subcommand the
CLI does not have.

### Publishing and reading

| Surface | Use |
| --- | --- |
| `POST /2/tweets` | a post (≤ 280 characters; hashtags are appended only if they still fit) |
| `POST /2/media/upload` (+ `initialize`/`append`/`finalize`) | images: one call under 5 MB, chunked above it |
| `GET /2/users/me`, `GET /2/tweets/{id}` | who the token belongs to, and one own post |
| `GET /2/users/{id}/mentions`, recent search by `conversation_id` | mentions and replies **that the API returns** |
| `POST /2/tweets` with `in_reply_to` | a reply — held for a human in every mode |

**Metrics are available for 30 days** after a post is created and only for own posts; outside that
window the analytics page states "unavailable" instead of showing a zero. Documented limits are
recorded in the readiness report (`app_24h` / `user_15min` per endpoint).

### What it costs

X's self-serve pricing is pay-per-use, and a link changes the price by an order of magnitude:

| Action | Published rate used by this project |
| --- | --- |
| one post | ~**$0.015** |
| one post **containing a URL** | ~**$0.20** |
| reading one own post | ~$0.001 |

`estimate_cost()` records the expected cost per post in the API log, and the policy engine's
`max_links_per_post: 1` is what keeps a careless calendar affordable. Prices are the project's
reading of the current published list — check the Developer Console for your own app.

**Status: never exercised against a live account from this repository.** Fully covered by mocks
(`tests/test_social_publisher.py`, and the connector's own paths through `FakeTransport`).

## Instagram

Uses the official Instagram Graph API. Two login flows are supported, and the connector reports
which one is active:

| `connectors.instagram.login` | Scopes for publishing |
| --- | --- |
| `instagram_login` (no Facebook Page) | `instagram_business_basic`, `instagram_business_content_publish` |
| `facebook_login` (Page-linked) | `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` |

Comments and insights use their own permissions (`instagram_business_manage_comments` /
`instagram_business_manage_insights`, or `instagram_manage_comments` / `instagram_manage_insights`),
and the stored scope list is checked before a read: a token that can publish but cannot read comments
is reported as such rather than silently returning nothing.

### Requirements that are not negotiable

1. The account must be **professional** (Business or Creator).
2. Leaving development mode needs **App Review and business verification**.
3. `connectors.instagram.ig_user_id` must be the IG professional account id.
4. **Media must be reachable over public HTTPS.** Meta fetches the file itself, so
   `connectors.instagram.public_media_base_url` must be an `https://` URL that serves the generated
   files; `doctor` flags a non-HTTPS value as a configuration problem. A local directory alone cannot
   publish.
5. `SOCIAL_IG_APP_SECRET` is used for the `appsecret_proof` on every call.

### The publish flow, as implemented

```
create container (image / video / carousel)  ──►  poll GET ?fields=status_code (3 s, up to 30 tries)
    │                                              │
    │ 400 containers per rolling 24 h              ├─ IN_PROGRESS … keep polling
    │ TTL 24 h after creation                      ├─ FINISHED ──► POST /media_publish
    │                                              └─ ERROR / EXPIRED ──► recorded, not retried blind
    ▼
store the returned media id + permalink in published_post
```

The publishing quota (`/{ig-user-id}/content_publishing_limit`) is read **at runtime** rather than
trusted from a documentation page, and the container create ceiling (400/24 h) and TTL are stated in
the readiness report for the same reason.

Insights are requested per media type, and `impressions` is deliberately absent (deprecated for media
created after 2 July 2024). Carousel insights for media *inside* an album do not exist, which the
report says instead of returning zeroes.

**Status: never exercised against a live account from this repository.** The flow — including the
container polling, the error paths and the quota read — is covered by mocks.

## Cross-cutting rules

- **No credential ever reaches a log, a prompt, the database or the dashboard.** Readiness is
  reported as a variable *name*, a boolean and a one-way `sha256` fingerprint.
- **No credential is ever written by this project.** It reads environment variables; you set them.
- **Retries** are bounded with backoff and recorded per attempt in `api_call`; rate limiting is per
  connector, and a 429 is retried, not hammered.
- **Isolation is tested**: a failure or a rate limit on one platform cannot publish or block on
  another (see `test_social_publisher.py`).
- **API versions are pinned where the platform pins them** (`connectors.instagram.graph_version`,
  currently `v25.0`) so an upgrade is a decision with a diff rather than a surprise.

## See also

- [SOCIAL-MANAGER.md](SOCIAL-MANAGER.md) — components, the 13 policy checks, autonomy modes, approvals.
- [SOCIAL-DEPLOY.md](SOCIAL-DEPLOY.md) — where these variables are set, locally and in the cloud.
