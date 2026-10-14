"""The policy engine: the deterministic gate in front of every action.

No model runs here.  Every check is a plain function over stored data, and the
result is one of three outcomes:

* ``pass`` - the action may proceed (subject to the autonomy mode);
* ``needs_review`` - a human decides; the draft is held, not published;
* ``block`` - refused, with the reason recorded.

Three design rules the brief demands, made structural:

1. **fail safe.**  A check that raises becomes ``needs_review`` - never ``pass``.
   An engine that cannot verify a requirement must not let the action through.
   :func:`PolicyEngine.check` wraps every check, so this cannot be forgotten.
2. **Mode A cannot publish.**  ``publish`` in draft mode is a *block*, not a
   confirmation prompt.  The check is in the engine rather than in the caller.
3. **limits are ceilings, duplicates are terminal.**  Frequency caps and the
   "this exact text already went out" test both block; neither can be approved
   away, because approving the same text twice is exactly the mistake the
   duplicate rule exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import obs, secrets
from .db import SocialDB, day_start_utc, parse_ts, to_ts, utcnow
from .models import (
    AccountStatus,
    AutonomyMode,
    PLATFORM_LIMITS,
    Platform,
    PolicyOutcome,
    Risk,
    normalize_body,
)

PASS = PolicyOutcome.PASS
BLOCK = PolicyOutcome.BLOCK
REVIEW = PolicyOutcome.NEEDS_REVIEW
SEVERITY = {str(PASS): 0, str(REVIEW): 1, str(BLOCK): 2}

#: Actions the engine knows how to judge.  Drafting is always allowed; the other
#: two are what an unattended worker would do, so they carry the strict checks.
ACTION_DRAFT = "draft"
ACTION_SCHEDULE = "schedule"
ACTION_PUBLISH = "publish"
WRITE_ACTIONS = frozenset({ACTION_SCHEDULE, ACTION_PUBLISH})

KILL_SWITCH_KEY = "kill_switch"

#: Write scopes each connector needs before it may publish at all.
REQUIRED_WRITE_SCOPES: dict[str, tuple[str, ...]] = {
    str(Platform.X): ("tweet.write",),
    str(Platform.INSTAGRAM): ("instagram_business_content_publish", "instagram_content_publish"),
    str(Platform.GITHUB): (),
}

_MENTION = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


@dataclass
class Check:
    name: str
    outcome: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.name, "outcome": self.outcome, "detail": self.detail}


@dataclass
class PolicyReport:
    """The verdict, plus every check that produced it - the audit trail."""

    outcome: str = str(PASS)
    checks: list[Check] = field(default_factory=list)
    action: str = ACTION_DRAFT
    platform: str = ""
    variant_id: int | None = None

    def add(self, name: str, outcome: str, detail: str = "") -> None:
        if SEVERITY.get(outcome, 1) > SEVERITY.get(self.outcome, 0):
            self.outcome = outcome
        self.checks.append(Check(name, outcome, detail))

    @property
    def blocked(self) -> bool:
        return self.outcome == str(BLOCK)

    @property
    def needs_review(self) -> bool:
        return self.outcome == str(REVIEW)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.outcome != str(PASS)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "action": self.action,
            "platform": self.platform,
            "variant_id": self.variant_id,
            "checks": [c.to_dict() for c in self.checks],
            "reasons": self.reasons,
            "evaluated_at": to_ts(),
        }


@dataclass
class PolicyContext:
    settings: Any
    db: SocialDB
    variant: dict[str, Any]
    item: dict[str, Any] | None
    account: dict[str, Any] | None
    action: str
    now: datetime
    mode: AutonomyMode
    live_enabled: bool = False


def kill_switch_engaged(db: SocialDB, settings: Any) -> bool:
    """The stop button: a database setting, else the configuration file.

    Deliberately independent of any model and of the autonomy mode - the button
    stops the publisher even when everything else is configured to run.
    """
    stored = db.setting(KILL_SWITCH_KEY, None)
    if isinstance(stored, bool):
        return stored
    return bool(settings.autonomy.kill_switch)


def set_kill_switch(db: SocialDB, engaged: bool, actor: str = "operator") -> bool:
    db.set_setting(KILL_SWITCH_KEY, bool(engaged), actor=actor)
    db.audit(
        "kill_switch.engaged" if engaged else "kill_switch.released",
        actor=actor,
        subject="system",
        detail={"engaged": bool(engaged)},
    )
    obs.warning("kill_switch.changed", detail={"engaged": bool(engaged), "actor": actor})
    return bool(engaged)


class PolicyEngine:
    """Runs the checks.  Deterministic, offline, and safe by default."""

    def __init__(self, settings: Any, db: SocialDB, *, now: Callable[[], datetime] | None = None):
        self.settings = settings
        self.db = db
        self._now = now or utcnow

    # ------------------------------------------------------------------ checks
    def check(
        self,
        variant: dict[str, Any],
        *,
        action: str = ACTION_PUBLISH,
        account: dict[str, Any] | None = None,
        item: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> PolicyReport:
        now = now or self._now()
        platform = str(variant.get("platform", ""))
        item = item if item is not None else self._item_for(variant)
        if account is None:
            account = self.db.account_for(platform)
        context = PolicyContext(
            settings=self.settings,
            db=self.db,
            variant=variant,
            item=item,
            account=account,
            action=action,
            now=now,
            mode=self.settings.mode(),
        )
        report = PolicyReport(
            action=action, platform=platform, variant_id=variant.get("id")
        )
        for name, check in self._checks():
            try:
                outcome, detail = check(context)
            except Exception as exc:  # noqa: BLE001 - an unverifiable rule must not pass
                obs.error("policy.check_error",
                          detail={"check": name, "error": f"{type(exc).__name__}: {exc}"})
                outcome, detail = str(REVIEW), f"check failed to run: {type(exc).__name__}"
            report.add(name, outcome, detail)
        return report

    def evaluate_and_store(self, variant_id: int, *, action: str = ACTION_DRAFT) -> PolicyReport:
        variant = self.db.variant(variant_id)
        if variant is None:
            raise KeyError(f"variant {variant_id} not found")
        report = self.check(variant, action=action, item=variant.get("item"))
        self.db.set_variant_policy(variant_id, report.outcome, report.to_dict())
        return report

    # ------------------------------------------------------------ the catalogue
    def _checks(self) -> list[tuple[str, Callable[[PolicyContext], tuple[str, str]]]]:
        return [
            ("kill_switch", _check_kill_switch),
            ("platform_authorised", _check_platform),
            ("account_ready", _check_account),
            ("autonomy_mode", _check_mode),
            ("approval", _check_approval),
            ("facts_and_sources", _check_facts),
            ("format_and_length", _check_format),
            ("no_secrets_or_pii", _check_secrets),
            ("financial_language", _check_financial),
            ("platform_rules", _check_platform_rules),
            ("duplicate", _check_duplicate),
            ("frequency", _check_frequency),
            ("language_declared", _check_language),
        ]

    # --------------------------------------------------------------- utilities
    def _item_for(self, variant: dict[str, Any]) -> dict[str, Any] | None:
        item = variant.get("item")
        if item:
            return item
        item_id = variant.get("item_id")
        return self.db.item(int(item_id)) if item_id else None

    def next_allowed_time(self, platform: str, now: datetime | None = None) -> datetime:
        """When the frequency rules would allow the next post on this platform."""
        now = now or self._now()
        limit = int(self.settings.editorial.max_posts_per_day.get(platform, 0) or 0)
        last = parse_ts(self.db.last_published_at(platform))
        candidate = now
        if limit > 0:
            day_start = day_start_utc()
            while self.db.published_today(platform, day_start) >= limit:
                candidate = max(candidate, parse_ts(day_start) or now) + timedelta(days=1)
                day_start = to_ts((parse_ts(day_start) or now) + timedelta(days=1))
        gap = int(self.settings.editorial.min_gap_minutes or 0)
        if last and gap > 0:
            earliest = last + timedelta(minutes=gap)
            candidate = max(candidate, earliest)
        return candidate


# ------------------------------------------------------------------ the checks
def _check_kill_switch(ctx: PolicyContext) -> tuple[str, str]:
    if kill_switch_engaged(ctx.db, ctx.settings):
        return str(BLOCK), "global kill switch is engaged: no action may be executed"
    return str(PASS), ""


def _check_platform(ctx: PolicyContext) -> tuple[str, str]:
    allowed = {str(p) for p in ctx.settings.autonomy.allowed_platforms}
    platform = str(ctx.variant.get("platform", ""))
    if platform not in allowed:
        return str(BLOCK), f"platform {platform!r} is not in autonomy.allowed_platforms"
    return str(PASS), ""


def _check_account(ctx: PolicyContext) -> tuple[str, str]:
    if ctx.action not in WRITE_ACTIONS:
        return str(PASS), "draft only: no credential is needed"
    platform = str(ctx.variant.get("platform", ""))
    account = ctx.account
    if account is None:
        return str(BLOCK), (
            f"no social_account row for {platform}: connect the account before publishing"
        )
    status = str(account.get("status", ""))
    if status != str(AccountStatus.READY):
        return str(BLOCK), f"{platform} account status is {status!r}, not 'ready'"
    needed = REQUIRED_WRITE_SCOPES.get(platform, ())
    if needed:
        have = {str(s) for s in (account.get("scopes") or [])}
        if not (have & set(needed)):
            return str(BLOCK), (
                f"account lacks a writing scope (needs one of {', '.join(needed)})"
            )
    return str(PASS), ""


def _check_mode(ctx: PolicyContext) -> tuple[str, str]:
    if ctx.action not in WRITE_ACTIONS:
        return str(PASS), f"mode {ctx.mode} allows drafting"
    if ctx.mode == AutonomyMode.DRAFT:
        return str(BLOCK), (
            "autonomy mode A (draft): publishing is disabled for every platform; "
            "approve manually or switch to mode B in Settings"
        )
    risk = str(ctx.item.get("risk") if ctx.item else ctx.variant.get("risk", Risk.LOW))
    if ctx.mode == AutonomyMode.CONTROLLED and risk in {"high", "sensitive"}:
        # Mode B may publish sensitive content *once a human has said yes*: that is
        # what "the sensitive cases require approval" means.  Without the approval
        # the item waits, and the approval check below says so.
        item_id = int((ctx.item or {}).get("id") or 0)
        if item_id and ctx.db.approval_state("content_item", item_id) == "approved":
            return str(PASS), f"mode B: a human approved this {risk}-risk item"
        return str(REVIEW), (
            f"mode B holds {risk}-risk content for a human decision (no approval yet)"
        )
    return str(PASS), f"mode {ctx.mode} allows this action"


def _check_approval(ctx: PolicyContext) -> tuple[str, str]:
    item = ctx.item
    if not item:
        return str(REVIEW), "variant has no content_item: nothing is traceable, holding"
    subject_type, subject_id = "content_item", int(item["id"])
    risk = str(item.get("risk", Risk.LOW))
    requires = (
        ctx.action in WRITE_ACTIONS
        and (
            ctx.mode == AutonomyMode.DRAFT
            or risk in {str(r) for r in ctx.settings.autonomy.require_approval_risks}
            or str(item.get("state", "")) == "needs_review"
        )
    )
    state = ctx.db.approval_state(subject_type, subject_id)
    if not requires:
        return str(PASS), f"approval not mandatory ({state})"
    if ctx.action == ACTION_DRAFT:
        return str(PASS), "drafting never needs an approval"
    if state == "approved":
        return str(PASS), "approved by a human"
    if state == "rejected":
        return str(BLOCK), "a human rejected this item"
    if state == "pending":
        return str(REVIEW), f"the {risk}-risk approval is pending"
    return str(REVIEW), (
        f"{risk}-risk content needs an approval and none has been requested yet"
    )


def _check_facts(ctx: PolicyContext) -> tuple[str, str]:
    item = ctx.item or {}
    facts = item.get("facts") or []
    if not facts:
        outcome = str(REVIEW) if ctx.settings.policy.review_without_sources else str(BLOCK)
        return outcome, "no verified facts attached to this item"
    unverified = [f for f in facts if not f.get("verified", True)]
    if unverified:
        return str(REVIEW), f"{len(unverified)} fact(s) are marked unverified"
    urls = [str(f.get("source_url", "")) for f in facts]
    missing = [f for f in facts if not str(f.get("source_url", "")).strip()]
    if missing and ctx.settings.policy.require_sources:
        return str(BLOCK), f"{len(missing)} fact(s) have no source URL"
    allowed = tuple(ctx.settings.policy.source_hosts_allowed or ())
    if allowed:
        for url in urls:
            if not url:
                continue
            if not (url.startswith(("http://", "https://")) or url.startswith("docs/")):
                continue
            host = url.split("//", 1)[-1].split("/", 1)[0] if "//" in url else ""
            if host and not any(host == a or host.endswith("." + a) for a in allowed):
                return str(REVIEW), f"source host {host!r} is not in policy.source_hosts_allowed"
    return str(PASS), f"{len(facts)} fact(s) with sources"


def _check_format(ctx: PolicyContext) -> tuple[str, str]:
    body = str(ctx.variant.get("body") or "")
    platform = str(ctx.variant.get("platform", ""))
    if not body.strip():
        return str(BLOCK), "empty body"
    if _ZERO_WIDTH.search(body):
        return str(BLOCK), "body contains zero-width characters"
    limits = PLATFORM_LIMITS.get(platform, {})
    hard = limits.get("text") or limits.get("caption") or 100000
    if len(body) > hard:
        return str(BLOCK), f"body is {len(body)} characters, the platform limit is {hard}"
    minimum = int(ctx.settings.policy.min_chars.get(platform, 0) or 0)
    if len(body.strip()) < minimum:
        return str(REVIEW), f"body is shorter than the {minimum}-character floor"
    links = re.findall(r"https?://\S+", body)
    if len(links) > int(ctx.settings.policy.max_links_per_post):
        return str(BLOCK), f"{len(links)} links, the limit is {ctx.settings.policy.max_links_per_post}"
    # Only tokens with a letter count: "#21" is an issue reference, not a hashtag.
    tags = re.findall(r"#\w*[A-Za-z]\w*", body) + \
        re.findall(r"#\w*[A-Za-z]\w*", str(ctx.variant.get("hashtags") or ""))
    max_tags = int(ctx.settings.policy.max_hashtags.get(platform, 30) or 30)
    if len(tags) > max_tags:
        return str(BLOCK), f"{len(tags)} hashtags, the limit is {max_tags}"
    emojis = len(re.findall(
        "[\U0001f300-\U0001faff\U00002600-\U000027bf]", body))
    if emojis > int(ctx.settings.policy.max_emojis or 0):
        return str(REVIEW), f"{emojis} emoji, above the configured maximum"
    return str(PASS), f"{len(body)} characters"


def _check_secrets(ctx: PolicyContext) -> tuple[str, str]:
    body = str(ctx.variant.get("body") or "") + " " + str(ctx.variant.get("hashtags") or "")
    masked = secrets.redact(body, ctx.settings.secret_names())
    if masked != body:
        return str(BLOCK), "the body contains a value that matches a credential pattern"
    for pattern in ctx.settings.policy.pii_patterns:
        try:
            found = re.search(pattern, body)
        except re.error:
            continue
        if found:
            return str(BLOCK), "the body contains personal data (email, phone or card-like number)"
    for name in ctx.settings.secret_names():
        value = secrets.read(name)
        if value and value in body:
            return str(BLOCK), f"the body contains the value of {name}"
    return str(PASS), ""


def _check_financial(ctx: PolicyContext) -> tuple[str, str]:
    body = normalize_body(str(ctx.variant.get("body") or ""))
    for phrase in ctx.settings.policy.banned_phrases:
        if phrase and str(phrase).lower() in body:
            return str(BLOCK), f"banned performance claim: {phrase!r}"
    hits = [t for t in ctx.settings.policy.review_terms if str(t).lower() in body]
    if hits:
        approved = ctx.db.approval_state("content_item", int((ctx.item or {}).get("id") or 0))
        if ctx.action in WRITE_ACTIONS and approved != "approved":
            return str(REVIEW), (
                "mentions performance terms (" + ", ".join(hits[:4]) + "): a human must read it"
            )
        return str(PASS), "performance terms present but not being published here"
    return str(PASS), ""


def _check_platform_rules(ctx: PolicyContext) -> tuple[str, str]:
    body = str(ctx.variant.get("body") or "")
    platform = str(ctx.variant.get("platform", ""))
    mentions = _MENTION.findall(body)
    if len(mentions) > 3:
        return str(BLOCK), f"{len(mentions)} @mentions looks like mass engagement, which is banned"
    if re.search(r"\b(follow\s*(for|back|me)|seguimi|follow4follow)\b", body, re.IGNORECASE):
        return str(BLOCK), "the text asks for follows: engagement farming is banned"
    if platform == str(Platform.INSTAGRAM):
        if not str(ctx.variant.get("body") or "").strip():
            return str(BLOCK), "empty caption"
        media = ctx.variant.get("media") or []
        if ctx.action in WRITE_ACTIONS and not media:
            return str(REVIEW), "Instagram publishing needs media (image or carousel)"
    if platform == str(Platform.X) and len(body) > 280:
        return str(BLOCK), "over the 280-character post limit"
    return str(PASS), ""


def _check_duplicate(ctx: PolicyContext) -> tuple[str, str]:
    platform = str(ctx.variant.get("platform", ""))
    digest = str(ctx.variant.get("content_hash") or "")
    variant_id = int(ctx.variant.get("id") or 0)
    if not digest:
        return str(REVIEW), "variant has no content hash: cannot prove it is new"
    published = ctx.db.one(
        """SELECT id, published_at, url FROM published_post
           WHERE platform = ? AND content_hash = ? AND state <> 'dry_run'
           ORDER BY id DESC LIMIT 1""",
        [platform, digest],
    )
    if published:
        return str(BLOCK), (
            f"identical text was already published on {platform} at "
            f"{published['published_at']} (post {published['id']})"
        )
    # The variant itself is always "stored": exclude it, or every fresh draft
    # would be reported as its own duplicate.
    other = ctx.db.one(
        """SELECT id, item_id FROM content_variant
           WHERE platform = ? AND content_hash = ? AND id <> ? LIMIT 1""",
        [platform, digest, variant_id],
    )
    if other:
        return str(REVIEW), (
            f"the same text already exists as variant {other['id']} "
            f"(item {other['item_id']}): publish one of the two"
        )
    return str(PASS), ""


def _check_frequency(ctx: PolicyContext) -> tuple[str, str]:
    if ctx.action == ACTION_DRAFT:
        return str(PASS), ""
    platform = str(ctx.variant.get("platform", ""))
    limit = int(ctx.settings.editorial.max_posts_per_day.get(platform, 0) or 0)
    if limit <= 0:
        return str(BLOCK), f"max_posts_per_day for {platform} is 0: publishing is disabled"
    today = ctx.db.published_today(platform, day_start_utc())
    if today >= limit:
        return str(BLOCK), f"{today} posts already published today on {platform} (cap {limit})"
    gap = int(ctx.settings.editorial.min_gap_minutes or 0)
    last = parse_ts(ctx.db.last_published_at(platform))
    if last and gap > 0:
        earliest = last + timedelta(minutes=gap)
        if ctx.now < earliest:
            return str(BLOCK), (
                f"only {int((ctx.now - last).total_seconds() // 60)} minutes since the last "
                f"{platform} post; the configured gap is {gap}"
            )
    return str(PASS), f"{today}/{limit} posts today on {platform}"


def _check_language(ctx: PolicyContext) -> tuple[str, str]:
    lang = str(ctx.variant.get("lang") or "")
    if lang not in {str(x) for x in ctx.settings.editorial.languages}:
        return str(REVIEW), f"language {lang!r} is not in editorial.languages"
    return str(PASS), ""


def summarize(report: PolicyReport) -> str:
    if report.outcome == str(PASS):
        return f"policy pass ({report.platform})"
    if report.outcome == str(REVIEW):
        return "policy: needs review - " + "; ".join(report.reasons[:2])
    return "policy: blocked - " + "; ".join(report.reasons[:2])
