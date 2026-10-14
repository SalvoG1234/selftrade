"""The platform format: how a draft would look, and what would refuse it.

Phase 4 of the dashboard brief asks the Content Studio for *an anteprima fedele
al formato di ciascuna piattaforma* and for *limiti di lunghezza, link e campi
obbligatori validati prima dell'approvazione*.  This module is both halves, and
nothing else:

* :func:`preview_variant` measures one stored variant against the platform's own
  format - the character ceilings, the effective X length (a link counts as 23
  characters, the weight ``t.co`` gives it), how an over-long post would be split
  into a thread, where Instagram cuts the caption with *… more*, and the blocks
  GitHub would render from the markdown;
* :func:`verify_variant` reports every missing or over-limit field as a
  structured finding, and :func:`preview_item` aggregates them, so an approval
  can be refused with the field-level reason instead of a shrug.

Four rules the rest of this codebase already holds, kept here:

1. **Ceilings are read, never retyped.**  Every limit comes from
   :data:`~socialmanager.models.PLATFORM_LIMITS` or from ``policy`` in the
   settings, so the preview cannot drift away from the gate that judges it.
2. **The preview never loosens the gate.**  Where X counts a link as 23
   characters that number is *shown* (``measured.effective_length``), but the
   limit check uses the raw length - exactly the conservative rule
   ``policy._check_format`` applies.  The panel can therefore never say "fits"
   about a body the policy is about to block.  Policy keeps its own verdict, and
   the stored ``policy_report`` travels in the same payload, so the operator sees
   both without either one being a second implementation of the other.
3. **Nothing here renders HTML.**  GitHub markdown becomes blocks and inline
   spans drawn from a small whitelist, and the browser builds DOM nodes with
   ``textContent`` out of that structure.  Model-written text never reaches an
   ``innerHTML`` sink, which is what makes showing a preview safe under the
   dashboard's ``default-src 'none'``.
4. **No I/O.**  No network, no database, no ``stat`` on the media files: a
   preview must not depend on the working directory.  Media is counted and its
   requirements are stated; whether the file is still on disk is the publisher's
   problem, and it already fails loudly there.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .models import LANGS, PLATFORM_LIMITS

#: X wraps every link with ``t.co`` and counts it as a fixed 23 characters, no
#: matter how long the URL is.  Shown next to the raw count; never used to pass a
#: check.
X_URL_WEIGHT = 23

#: Where the Instagram feed cuts a caption and offers "… more".
INSTAGRAM_FEED_CUT = 125

ERROR = "error"          # refuses: the platform or the gate would reject this
WARNING = "warning"      # informs: worth a human's attention, does not refuse
OK = "ok"

#: The only span kinds :func:`inline_spans` may emit.  A test pins this list, so
#: a later change cannot smuggle a new node type past the renderer's whitelist.
SPAN_KINDS = frozenset({"text", "code", "strong", "link"})

#: Schemes a markdown link may use.  A ``javascript:`` or ``data:`` target is
#: refused rather than rendered as a link.
SAFE_SCHEMES = ("http://", "https://", "mailto:")


@dataclass(frozen=True)
class PlatformSpec:
    """What the platform's own composer would let you save."""

    platform: str
    label: str
    body_field: str            # what the platform calls the body it stores
    body_limit: int
    markdown: bool             # the body is rendered as markdown
    clickable_links: bool      # a link inside the body is tappable
    hashtags_separate: bool    # hashtags live in their own field
    media_limit: int
    media_required: bool
    threads: bool              # a single post can be continued
    title_limit: int = 0       # GitHub: the content title
    alt_text_limit: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Built from ``PLATFORM_LIMITS``: only the *shape* of each platform is written
#: here, every number is read from the single place that declares it.
SPECS: dict[str, PlatformSpec] = {
    "x": PlatformSpec(
        platform="x", label="X", body_field="text",
        body_limit=int(PLATFORM_LIMITS["x"]["text"]),
        markdown=False, clickable_links=True, hashtags_separate=False,
        media_limit=int(PLATFORM_LIMITS["x"]["media"]),
        alt_text_limit=int(PLATFORM_LIMITS["x"]["alt_text"]),
        media_required=False, threads=True,
    ),
    "instagram": PlatformSpec(
        platform="instagram", label="Instagram", body_field="caption",
        body_limit=int(PLATFORM_LIMITS["instagram"]["caption"]),
        markdown=False, clickable_links=False, hashtags_separate=True,
        media_limit=int(PLATFORM_LIMITS["instagram"]["carousel_items"]),
        media_required=True, threads=False,
    ),
    "github": PlatformSpec(
        platform="github", label="GitHub", body_field="body",
        body_limit=int(PLATFORM_LIMITS["github"]["body"]),
        title_limit=int(PLATFORM_LIMITS["github"]["title"]),
        markdown=True, clickable_links=True, hashtags_separate=False,
        media_limit=0, media_required=False, threads=False,
    ),
}


@dataclass(frozen=True)
class Finding:
    """One field of one variant, judged: what it measures and where the ceiling is."""

    field: str
    severity: str
    message: str
    measured: Any = None
    limit: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------- measurements
_LINK = re.compile(r"https?://\S+")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
#: A token with a letter is a hashtag; "#21" is an issue reference, not one.
_HASHTAG = re.compile(r"#\w*[A-Za-z]\w*")


def links_in(text: str) -> list[str]:
    return _LINK.findall(text or "")


def markdown_link_targets(text: str) -> list[str]:
    return [t.strip() for t in _MD_LINK.findall(text or "")]


def link_is_safe(target: str) -> bool:
    """A target the preview may render as a link.

    A relative target (``docs/ARCHITECTURE.md``) is fine: GitHub resolves it
    inside the repository, and the project's own documentation links that way.
    """
    target = (target or "").strip()
    if not target:
        return False
    if target.startswith(SAFE_SCHEMES) or target.startswith("#") or target.startswith("/"):
        return True
    return not _SCHEME.match(target)


def link_is_wellformed(url: str) -> bool:
    """``https://`` with a host that could resolve.

    Catches the failure the truncation in ``content._variant`` can produce: a URL
    cut in the middle when a body is trimmed to a platform limit.
    """
    url = url or ""
    if "…" in url or " " in url or "\t" in url:
        return False
    rest = url.split("//", 1)[-1] if "//" in url else url
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return bool(host) and "." in host and not host.startswith(".") and not host.endswith(".")


def x_effective_length(text: str) -> int:
    """What X would count: every link weighs 23 characters."""
    return len(_LINK.sub(lambda m: "x" * X_URL_WEIGHT, text or ""))


def split_thread(text: str, limit: int) -> list[str]:
    """Split a body into segments of at most ``limit`` characters.

    Words are never cut in half, and nothing is dropped: a body that fits is one
    segment, and a single word longer than the limit (a long URL) is the only
    thing that gets hard-split, because there is no other way to send it.
    """
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    segments: list[str] = []
    current = ""
    for word in body.split():
        while len(word) > limit:                    # a URL, a hash, a long token
            if current:
                segments.append(current)
                current = ""
            segments.append(word[:limit])
            word = word[limit:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                segments.append(current)
            current = word
    if current:
        segments.append(current)
    return segments


def instagram_caption_cut(text: str, limit: int = INSTAGRAM_FEED_CUT) -> dict[str, Any]:
    """Where the feed stops the caption in the preview."""
    body = (text or "").strip()
    truncated = len(body) > limit
    return {
        "head": body[:limit].rstrip() if truncated else body,
        "truncated": truncated,
        "cut_at": limit,
        "total": len(text or ""),
    }


# --------------------------------------------------------------------- markdown
_INLINE = re.compile(r"(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)\s]+\))")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")


def inline_spans(text: str) -> list[dict[str, Any]]:
    """Inline runs from a whitelist: plain text, ``code``, **strong**, a link.

    The values are raw text.  The renderer must place them with ``textContent``:
    nothing here is HTML, so a body containing ``<script>`` becomes a text span
    that reads ``<script>`` - and nothing else.
    """
    text = text or ""
    spans: list[dict[str, Any]] = []
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            spans.append({"k": "text", "v": text[pos:match.start()]})
        raw = match.group(0)
        if raw.startswith("`"):
            spans.append({"k": "code", "v": raw[1:-1]})
        elif raw.startswith("**"):
            spans.append({"k": "strong", "v": raw[2:-2]})
        else:
            label, _, target = raw[1:-1].partition("](")
            target = target.rstrip(")")
            if link_is_safe(target):
                spans.append({"k": "link", "v": label, "href": target})
            else:
                # Not a link the preview may follow: the label stays readable and
                # the target is shown as plain text so the operator sees it.
                spans.append({"k": "text", "v": f"{label} ({target})"})
        pos = match.end()
    if pos < len(text):
        spans.append({"k": "text", "v": text[pos:]})
    return spans


def markdown_blocks(text: str) -> list[dict[str, Any]]:
    """The body as GitHub would lay it out: headings, lists, code, quotes.

    Nesting and tables are deliberately out of scope - the drafts this renders do
    not use them, and pretending to support them would be worse than showing the
    line as written.
    """
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append({"kind": "paragraph", "spans": inline_spans(" ".join(paragraph))})
            paragraph.clear()

    lines = (text or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line.strip())
        if fence:                                   # fenced code: read until the close
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            flush()
            blocks.append({"kind": "code", "lang": fence.group(1), "text": "\n".join(code)})
            index += 1
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)
        quote = _QUOTE.match(line)
        if heading:
            flush()
            blocks.append({"kind": "heading", "level": len(heading.group(1)),
                           "spans": inline_spans(heading.group(2).strip())})
        elif bullet or ordered:
            flush()
            items: list[dict[str, Any]] = []
            is_ordered = ordered is not None
            while index < len(lines):
                match = _ORDERED.match(lines[index]) if is_ordered else _BULLET.match(lines[index])
                if not match:
                    break
                items.append({"spans": inline_spans(match.group(1))})
                index += 1
            blocks.append({"kind": "list", "ordered": is_ordered, "items": items})
            continue
        elif quote:
            flush()
            blocks.append({"kind": "quote", "spans": inline_spans(quote.group(1))})
        elif line.strip():
            paragraph.append(line.strip())
        else:
            flush()
        index += 1
    flush()
    return blocks


# ------------------------------------------------------------------- validation
def hashtag_ceiling(platform: str, settings: Any = None) -> tuple[int, str]:
    """The hashtag ceiling and where it comes from.

    Policy wins where it declares one: it is the gate, and a preview that allowed
    more than the gate would be lying.  Otherwise the platform's own ceiling.
    """
    policy = getattr(settings, "policy", None)
    from_policy = int((getattr(policy, "max_hashtags", {}) or {}).get(platform, 0) or 0)
    if from_policy:
        return from_policy, "policy.max_hashtags"
    from_platform = int((PLATFORM_LIMITS.get(platform) or {}).get("hashtags") or 0)
    if from_platform:
        return from_platform, "limite della piattaforma"
    return 0, "nessun limite dichiarato"


def verify_variant(variant: dict[str, Any], *, item: dict[str, Any] | None = None,
                   settings: Any = None) -> dict[str, Any]:
    """Judge one variant's format: required fields, length, links, media.

    Returns ``{ok, error_count, warning_count, findings}``.  ``ok`` is what the
    approval gate reads: an item may be approved when at least one of its
    variants is ``ok``, because publishing is per variant.
    """
    item = item or {}
    findings: list[Finding] = []

    def add(field: str, severity: str, message: str,
            measured: Any = None, limit: Any = None) -> None:
        findings.append(Finding(field, severity, message, measured, limit))

    platform = str(variant.get("platform") or "")
    lang = str(variant.get("lang") or "")
    body = str(variant.get("body") or "")
    hashtags = str(variant.get("hashtags") or "")
    media = [str(m) for m in (variant.get("media") or [])]
    alt_text = str(variant.get("alt_text") or "")
    spec = SPECS.get(platform)

    if spec is None:
        add("platform", ERROR, f"piattaforma non riconosciuta: {platform!r}")
        return _report(findings)
    if lang not in LANGS:
        add("lang", ERROR, f"lingua non supportata: {lang!r} (attese: {', '.join(LANGS)})")

    # 1. the body: required, then its ceiling, then the configured floor.
    if not body.strip():
        add(spec.body_field, ERROR,
            f"il corpo è vuoto: {spec.label} non accetta un post senza testo", 0, spec.body_limit)
    else:
        if len(body) > spec.body_limit:
            add(spec.body_field, ERROR,
                f"{len(body)} caratteri, il tetto di {spec.label} è {spec.body_limit}",
                len(body), spec.body_limit)
        else:
            add(spec.body_field, OK, f"{len(body)}/{spec.body_limit} caratteri",
                len(body), spec.body_limit)
    minimum = int(getattr(getattr(settings, "policy", None), "min_chars", {}).get(platform, 0) or 0)
    if minimum and len(body.strip()) < minimum:
        add(spec.body_field, WARNING,
            f"sotto il minimo configurato per {spec.label} ({minimum} caratteri): "
            "la policy chiede una revisione", len(body.strip()), minimum)

    # 2. links: how many, and whether each one could resolve.
    links = links_in(body)
    broken = [url for url in links if not link_is_wellformed(url)]
    for target in markdown_link_targets(body):
        if not link_is_safe(target):
            add("link", ERROR,
                f"protocollo non ammesso in un link: {target[:60]!r} "
                "(ammessi: http, https, mailto, percorsi relativi)", target[:60], None)
            break
    if broken:
        add("link", ERROR,
            "link non valido (host incompleto o URL troncato): "
            + ", ".join(broken[:2]), len(broken), 0)
    ceiling_links = int(getattr(getattr(settings, "policy", None),
                                "max_links_per_post", 0) or 0)
    if ceiling_links and len(links) > ceiling_links:
        add("link", ERROR,
            f"{len(links)} link nel corpo, il limite configurato è {ceiling_links}",
            len(links), ceiling_links)
    elif links:
        add("link", OK, f"{len(links)} link", len(links), ceiling_links or None)
    if not spec.clickable_links and links:
        add("link", WARNING,
            f"un link dentro la didascalia di {spec.label} non è cliccabile: "
            "resta leggibile, ma la destinazione va indicata in bio", len(links), None)

    # 3. hashtags, against whichever ceiling is lower in practice.
    tags = _HASHTAG.findall(body) + _HASHTAG.findall(hashtags)
    ceiling_tags, tag_source = hashtag_ceiling(platform, settings)
    if ceiling_tags and len(tags) > ceiling_tags:
        add("hashtags", ERROR,
            f"{len(tags)} hashtag, il limite è {ceiling_tags} ({tag_source})",
            len(tags), ceiling_tags)
    elif tags:
        add("hashtags", OK, f"{len(tags)} hashtag", len(tags), ceiling_tags or None)

    # 4. media: the ceiling, and the one platform that refuses without it.
    if spec.media_limit and len(media) > spec.media_limit:
        add("media", ERROR,
            f"{len(media)} allegati, il massimo di {spec.label} è {spec.media_limit}",
            len(media), spec.media_limit)
    if spec.media_required and not media:
        add("media", ERROR,
            f"{spec.label} rifiuta un post senza immagine: allega un'immagine o un carosello",
            0, 1)
    elif media:
        add("media", OK, f"{len(media)} allegati", len(media), spec.media_limit or None)
    if spec.alt_text_limit and media and not alt_text.strip():
        add("alt_text", WARNING,
            "gli allegati non hanno un testo alternativo: chi legge con uno screen reader "
            "non riceve il contenuto dell'immagine", 0, None)
    if spec.alt_text_limit and len(alt_text) > spec.alt_text_limit:
        add("alt_text", ERROR,
            f"testo alternativo di {len(alt_text)} caratteri, il tetto è {spec.alt_text_limit}",
            len(alt_text), spec.alt_text_limit)

    # 5. the title, where the platform has one.
    if spec.title_limit:
        title = str(item.get("title") or "")
        if not title.strip():
            add("title", ERROR, f"{spec.label} richiede un titolo per il contenuto")
        elif len(title) > spec.title_limit:
            add("title", ERROR,
                f"titolo di {len(title)} caratteri, il tetto è {spec.title_limit}",
                len(title), spec.title_limit)
        else:
            add("title", OK, f"titolo di {len(title)}/{spec.title_limit} caratteri",
                len(title), spec.title_limit)

    return _report(findings)


def _report(findings: list[Finding]) -> dict[str, Any]:
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    # Errors first: the panel reads top-down, and the block is the thing to fix.
    ordered = errors + warnings + [f for f in findings if f.severity == OK]
    return {
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "findings": [f.to_dict() for f in ordered],
    }


# ---------------------------------------------------------------------- preview
def preview_variant(variant: dict[str, Any], *, item: dict[str, Any] | None = None,
                    settings: Any = None) -> dict[str, Any]:
    """One variant as the platform would show it, plus its verdict."""
    body = str(variant.get("body") or "")
    platform = str(variant.get("platform") or "")
    spec = SPECS.get(platform)
    measured: dict[str, Any] = {
        "length": len(body),
        "limit": spec.body_limit if spec else 0,
        "urls": links_in(body),
        "hashtags": len(_HASHTAG.findall(body)
                        + _HASHTAG.findall(str(variant.get("hashtags") or ""))),
        "media": len(list(variant.get("media") or [])),
        "alt_text": len(str(variant.get("alt_text") or "")),
    }
    if spec is None:
        measured["remaining"] = 0
    else:
        measured["remaining"] = spec.body_limit - len(body)
        if platform == "x":
            measured["effective_length"] = x_effective_length(body)
            measured["url_weight"] = X_URL_WEIGHT
            measured["thread"] = split_thread(body, spec.body_limit)
        if platform == "instagram":
            measured["feed"] = instagram_caption_cut(body)
        if spec.markdown:
            measured["blocks"] = markdown_blocks(body)
    return {
        "variant_id": int(variant.get("id") or 0),
        "platform": platform,
        "label": spec.label if spec else platform,
        "lang": str(variant.get("lang") or ""),
        "body_field": spec.body_field if spec else "body",
        "body": body,
        "hashtags": str(variant.get("hashtags") or ""),
        "media": [str(m) for m in (variant.get("media") or [])],
        "alt_text": str(variant.get("alt_text") or ""),
        "generator": str(variant.get("generator") or ""),
        "risk": str(variant.get("risk") or ""),
        "spec": spec.to_dict() if spec else {},
        "measured": measured,
        "verify": verify_variant(variant, item=item, settings=settings),
        # The policy's own last verdict, shown next to the format checks rather
        # than reimplemented: two named sections, one gate.
        "policy": {
            "state": str(variant.get("policy_state") or "unchecked"),
            "outcome": str((variant.get("policy_report") or {}).get("outcome") or ""),
            "reasons": list((variant.get("policy_report") or {}).get("reasons") or []),
        },
    }


def accounts_by_platform(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """The first stored account per platform, so the preview can show a real name.

    A preview must not invent a handle: when no account is stored the panel says
    the account is not connected, which is the truth the connector reports too.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        platform = str(row.get("platform") or "")
        if not platform or platform in out:
            continue
        out[platform] = {
            "handle": str(row.get("handle") or ""),
            "status": str(row.get("status") or "unconfigured"),
            "scopes": [str(s) for s in (row.get("scopes") or [])],
        }
    return out


def preview_item(item: dict[str, Any] | None, *, settings: Any = None,
                 accounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every variant of one item, and the number an approval needs.

    ``ready`` holds the variants that could actually go out; ``blocked`` the ones
    with at least one error, each carrying its own findings.  ``approvable`` is
    false when nothing is ready - approving a set of drafts that cannot be
    published would be a decision about nothing.
    """
    item = item or {}
    variants = [preview_variant(v, item=item, settings=settings)
                for v in (item.get("variants") or [])]
    ready = [v["variant_id"] for v in variants if v["verify"]["ok"]]
    blocked = [v["variant_id"] for v in variants if not v["verify"]["ok"]]
    if not variants:
        reason = "questo item non ha varianti: non c'è nulla da pubblicare"
    elif not ready:
        reason = ("nessuna delle {} varianti supera la validazione di formato: "
                  "correggi i campi indicati prima di approvare".format(len(variants)))
    else:
        reason = ""
    return {
        "item_id": int(item.get("id") or 0),
        "title": str(item.get("title") or ""),
        "variants": variants,
        "ready": ready,
        "blocked": blocked,
        "approvable": bool(ready),
        "blocked_reason": reason,
        "error_count": sum(v["verify"]["error_count"] for v in variants),
        "warning_count": sum(v["verify"]["warning_count"] for v in variants),
        "accounts": accounts_by_platform(accounts),
    }


def refusal_message(previews: dict[str, Any]) -> str:
    """The one sentence an approval refusal carries, naming what to fix.

    The findings themselves keep their field names, so the operator can read the
    same list in the panel and in the response.
    """
    details: list[str] = []
    for variant in previews.get("variants") or []:
        errors = [f for f in variant["verify"]["findings"] if f["severity"] == ERROR]
        if not errors:
            continue
        first = errors[0]
        details.append(f"{variant['label']}/{variant['lang']}: {first['message']}")
    tail = f" ({'; '.join(details[:3])})" if details else ""
    return str(previews.get("blocked_reason") or "contenuto non valido") + tail
