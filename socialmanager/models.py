"""The vocabulary every other module speaks: platforms, categories, states, ideas.

These are deliberately plain value objects.  Agents return
:class:`ContentIdea`/:class:`VariantDraft` instances; the database layer stores
them; the policy engine judges them; the dashboard renders them.  Nothing here
performs I/O, which is what makes the policy engine testable without a network.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Platform(str, Enum):
    """Where a variant can be published.  ``GH`` is the repo itself."""

    GITHUB = "github"
    X = "x"
    INSTAGRAM = "instagram"

    def __str__(self) -> str:  # nicer SQL bindings and log lines
        return self.value


class Category(str, Enum):
    """The ten editorial categories from the brief."""

    BUILD_IN_PUBLIC = "build_in_public"
    DEV_UPDATE = "dev_update"
    RELEASE = "release"
    TECH_EXPLAINER = "tech_explainer"
    EXPERIMENT = "experiment"
    LIMITS = "limits"
    DASHBOARD_DEMO = "dashboard_demo"
    FEEDBACK_REQUEST = "feedback_request"
    TUTORIAL = "tutorial"
    CONTRIBUTE = "contribute"

    def __str__(self) -> str:
        return self.value


#: Keyed by the *string* value, not by the enum member: ``Category`` is a ``str``
#: subclass, so ``Category.RELEASE == "release"`` is true while
#: ``hash(Category.RELEASE) != hash("release")`` - a dictionary keyed by members
#: silently misses every string lookup.  Everything user-facing is keyed by value.
CATEGORY_LABELS: dict[str, str] = {
    "build_in_public": "Build in public",
    "dev_update": "Aggiornamento di sviluppo",
    "release": "Release e changelog",
    "tech_explainer": "Spiegazione tecnica",
    "experiment": "Esperimento e risultato reale",
    "limits": "Limiti e problemi scoperti",
    "dashboard_demo": "Demo della dashboard",
    "feedback_request": "Richiesta di feedback",
    "tutorial": "Tutorial e guida",
    "contribute": "Invito a contribuire",
}


class Risk(str, Enum):
    """How much human attention a piece of content needs before it goes out."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SENSITIVE = "sensitive"

    def __str__(self) -> str:
        return self.value


#: Risk levels that always require a human decision, in every autonomy mode.
ALWAYS_APPROVAL: frozenset[str] = frozenset({Risk.HIGH, Risk.SENSITIVE})


class EditorialState(str, Enum):
    """The life of a content item.  ``scheduled`` is not ``published``."""

    IDEA = "idea"
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        return self.value


class PolicyOutcome(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    NEEDS_REVIEW = "needs_review"

    def __str__(self) -> str:
        return self.value


class AutonomyMode(str, Enum):
    """Mode A/B/C from the brief."""

    DRAFT = "draft"            # A - the AI writes, a human publishes
    CONTROLLED = "controlled"  # B - ordinary content may publish itself
    ADVANCED = "advanced"      # C - plans, publishes, measures, adapts

    def __str__(self) -> str:
        return self.value


class AccountStatus(str, Enum):
    UNCONFIGURED = "unconfigured"   # no credentials: draft/simulation only
    DRAFT_ONLY = "draft_only"       # credentials present, write permission missing
    READY = "ready"                 # verified: can publish
    ERROR = "error"
    REVOKED = "revoked"

    def __str__(self) -> str:
        return self.value


#: Platform hard limits, used by the policy engine and the composer.  Keyed by
#: string value for the reason given above: every lookup in this codebase has a
#: plain ``str`` in hand.
PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "x": {"text": 280, "media": 4, "alt_text": 1000},
    "instagram": {"caption": 2200, "carousel_items": 10, "hashtags": 30},
    "github": {"title": 256, "body": 65536},
}

#: Conservative starting ceilings (the operator can lower them in social.yaml).
DEFAULT_FREQ_LIMITS: dict[str, int] = {
    "x": 2,
    "instagram": 1,
    "github": 1,
}

LANGS = ("it", "en")


@dataclass(frozen=True)
class Fact:
    """One checkable statement, bound to the artifact that proves it.

    The content engine may only build a claim out of facts like these, and the
    policy engine refuses a draft whose facts are unverified and unlinked.
    """

    claim: str
    source_url: str
    kind: str = "repo"          # repo|commit|release|issue|docs|run|ci|test
    verified: bool = True
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Fact:
        return Fact(
            claim=str(data.get("claim", "")),
            source_url=str(data.get("source_url", "")),
            kind=str(data.get("kind", "repo")),
            verified=bool(data.get("verified", True)),
            excerpt=str(data.get("excerpt", "")),
        )


@dataclass
class VariantDraft:
    """One platform+language rendering of an idea."""

    platform: Platform
    lang: str
    body: str
    hashtags: str = ""
    media: list[str] = field(default_factory=list)
    generator: str = "template"
    risk: Risk = Risk.LOW
    alt_text: str = ""

    def content_hash(self) -> str:
        """Stable hash of what would actually go out (used for dedupe)."""
        raw = "\n".join(
            [
                str(self.platform),
                self.lang,
                normalize_body(self.body),
                (self.hashtags or "").strip(),
                ",".join(sorted(self.media)),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def char_count(self) -> int:
        return len(self.body)


@dataclass
class ContentIdea:
    """The unit an agent proposes and a human (or policy) judges."""

    title: str
    category: Category
    summary: str = ""
    risk: Risk = Risk.LOW
    facts: list[Fact] = field(default_factory=list)
    variants: list[VariantDraft] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # content_source.external_ref
    planned_for: str | None = None                     # YYYY-MM-DD, project timezone
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "category": str(self.category),
            "summary": self.summary,
            "risk": str(self.risk),
            "facts": [f.to_dict() for f in self.facts],
            "variants": [
                {
                    "platform": str(v.platform),
                    "lang": v.lang,
                    "body": v.body,
                    "hashtags": v.hashtags,
                    "media": list(v.media),
                    "generator": v.generator,
                    "risk": str(v.risk),
                    "alt_text": v.alt_text,
                    "content_hash": v.content_hash(),
                }
                for v in self.variants
            ],
            "sources": list(self.sources),
            "planned_for": self.planned_for,
            "notes": self.notes,
        }


_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def normalize_body(text: str) -> str:
    """Whitespace-insensitive form used for hashing and duplicate detection."""
    return _WS.sub(" ", _ZERO_WIDTH.sub("", text or "")).strip().lower()


def hash_text(text: str) -> str:
    return hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()


def platform_of(value: Any) -> Platform:
    return value if isinstance(value, Platform) else Platform(str(value))


def category_of(value: Any) -> Category:
    return value if isinstance(value, Category) else Category(str(value))
