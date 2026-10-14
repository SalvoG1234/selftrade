"""The content engine: real repo events in, bilingual drafts out.

Two things separate this from "ask a model and post the answer":

**1. Facts come first.**  Every draft is built from a
:class:`~socialmanager.models.ContentIdea` whose facts are assembled from stored
``content_source`` rows - a release, a cluster of commits, an issue, a docs file.
Each fact carries the URL that proves it.  The generator may use only those
facts, and :func:`unsupported_numbers` re-reads the finished body to catch a
number that is not in them (a fabricated star count, a made-up return).

**2. The model is optional.**  Every category has a hand-written template in
Italian and in English - written separately, not translated - so the engine
still produces honest drafts when Ollama is down.  The drafts say which path
produced them (``template`` or ``llm:<model>``), and the operator sees it in the
Content Studio.

Nothing here publishes, and nothing here decides.  It proposes; the policy
engine judges; a human (or the configured autonomy mode) decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import obs
from .db import SocialDB, to_ts
from .llm import SYSTEM_RULES, VARIANT_SCHEMA, LlmProvider, UnavailableProvider
from .models import (
    Category,
    ContentIdea,
    Fact,
    Platform,
    Risk,
    VariantDraft,
    normalize_body,
)

#: Which platform a category speaks to by default.
DEFAULT_TARGETS: dict[str, list[str]] = {
    Category.RELEASE: [str(Platform.X), str(Platform.GITHUB), str(Platform.INSTAGRAM)],
    Category.DEV_UPDATE: [str(Platform.X)],
    Category.BUILD_IN_PUBLIC: [str(Platform.X), str(Platform.INSTAGRAM)],
    Category.TECH_EXPLAINER: [str(Platform.X), str(Platform.INSTAGRAM)],
    Category.EXPERIMENT: [str(Platform.X), str(Platform.INSTAGRAM)],
    Category.LIMITS: [str(Platform.X), str(Platform.GITHUB)],
    Category.DASHBOARD_DEMO: [str(Platform.INSTAGRAM), str(Platform.X)],
    Category.FEEDBACK_REQUEST: [str(Platform.X), str(Platform.GITHUB)],
    Category.TUTORIAL: [str(Platform.GITHUB), str(Platform.X)],
    Category.CONTRIBUTE: [str(Platform.GITHUB), str(Platform.X)],
}

#: The risk a category starts at, before its text is examined.
BASE_RISK: dict[str, Risk] = {
    Category.RELEASE: Risk.LOW,
    Category.DEV_UPDATE: Risk.LOW,
    Category.BUILD_IN_PUBLIC: Risk.LOW,
    Category.TECH_EXPLAINER: Risk.MEDIUM,
    Category.EXPERIMENT: Risk.MEDIUM,
    Category.LIMITS: Risk.MEDIUM,
    Category.DASHBOARD_DEMO: Risk.MEDIUM,
    Category.FEEDBACK_REQUEST: Risk.LOW,
    Category.TUTORIAL: Risk.LOW,
    Category.CONTRIBUTE: Risk.LOW,
}

BASE_HASHTAGS: dict[str, str] = {
    Category.RELEASE: "#OpenSource #QuantTrading #Python",
    Category.DEV_UPDATE: "#BuildInPublic #Python #QuantTrading",
    Category.BUILD_IN_PUBLIC: "#BuildInPublic #OpenSource #Python",
    Category.TECH_EXPLAINER: "#QuantTrading #Backtesting #Python",
    Category.EXPERIMENT: "#QuantTrading #Research #Backtesting",
    Category.LIMITS: "#BuildInPublic #QuantTrading",
    Category.DASHBOARD_DEMO: "#Python #OpenSource #DataViz",
    Category.FEEDBACK_REQUEST: "#OpenSource #QuantTrading",
    Category.TUTORIAL: "#Python #Tutorial #QuantTrading",
    Category.CONTRIBUTE: "#OpenSource #Python #GoodFirstIssue",
}

#: Source kinds that can justify an idea, in the order the engine prefers them.
IDEA_SOURCE_KINDS = ("release", "commit", "issue", "pr", "discussion", "docs", "readme")

#: Categories whose drafts get an image.  Strings, not enum members: a set of
#: members would never match the ``str`` a draft carries (see models.CATEGORY_LABELS).
MEDIA_CATEGORIES = frozenset({
    str(Category.RELEASE), str(Category.BUILD_IN_PUBLIC), str(Category.DASHBOARD_DEMO),
    str(Category.EXPERIMENT), str(Category.LIMITS), str(Category.TUTORIAL),
    str(Category.CONTRIBUTE),
})

#: Function words that separate the two languages.  ``in`` is deliberately absent
#: from both lists: it is the same word in Italian and in English, and counting it
#: once produced false "wrong language" flags on short Italian posts.
ITALIAN_MARKERS = (
    " il ", " lo ", " la ", " gli ", " le ", " che ", " non ", " con ", " per ", " una ",
    " uno ", " un ", " di ", " del ", " della ", " della ", " dei ", " delle ", " degli ",
    " e ", " è ", " sono ", " nel ", " nella ", " sul ", " sulla ", " al ", " alla ",
    " come ", " più ", " senza ", " anche ", " solo ", " questo ", " questa ", " ogni ",
    " tutti ", " puoi ", " essere ", " dove ", " quando ", " perche ", " cosi ", " guida ",
    " documentazione ", " repository ", " progetto ", " nessun ", " pochi ", " provare ",
)
ENGLISH_MARKERS = (
    " the ", " and ", " with ", " for ", " this ", " that ", " not ", " is ", " are ",
    " from ", " what ", " how ", " of ", " on ", " it ", " we ", " you ", " can ",
    " your ", " will ", " have ", " here ", " now ", " all ", " when ", " because ",
    " guide ", " documentation ", " repository ", " project ", " every ", " only ", " howto ",
)

#: Below this many words a post is too short to judge, and a wrong "this is not
#: Italian" verdict is expensive: it forces a human review on a fine draft.
MIN_WORDS_FOR_LANGUAGE_CHECK = 8

_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


# ---------------------------------------------------------------------- briefs
@dataclass
class ContentBrief:
    """What the engine is asked to say, and why it is allowed to say it."""

    key: str
    category: Category
    title: str
    summary: str
    facts: list[Fact]
    sources: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    risk: Risk = Risk.LOW
    context: dict[str, Any] = field(default_factory=dict)
    planned_for: str | None = None


@dataclass
class GenerationReport:
    run_id: str
    provider: dict[str, Any] = field(default_factory=dict)
    briefs: int = 0
    items: list[int] = field(default_factory=list)
    variants: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "briefs": self.briefs,
            "items": list(self.items),
            "variants": self.variants,
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


def covered_refs(db: SocialDB) -> set[str]:
    """Source refs already used by an item - so the engine proposes only news."""
    out: set[str] = set()
    for row in db.all("SELECT source_refs FROM content_item"):
        try:
            import json

            out.update(str(x) for x in json.loads(row["source_refs"] or "[]"))
        except (TypeError, ValueError):
            continue
    return out


def index_local_docs(db: SocialDB, root: str | Path = ".", limit: int = 40) -> int:
    """Index ``docs/*.md`` as sources, so drafts can cite the project's own docs."""
    docs_dir = Path(root) / "docs"
    if not docs_dir.is_dir():
        return 0
    stored = 0
    files = sorted(docs_dir.glob("*.md"))[:limit]
    for path in files:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        headings = [h.strip() for h in _HEADING.findall(body)][:12]
        db.upsert_source(
            "docs",
            f"docs/{path.name}",
            url=f"docs/{path.name}",
            title=headings[0] if headings else path.name,
            body=body[:20000],
            meta={"headings": headings, "bytes": len(body)},
        )
        stored += 1
    return stored


def facts_from_sources(sources: Iterable[dict[str, Any]]) -> list[Fact]:
    """Turn stored sources into checkable claims, each with its URL."""
    facts: list[Fact] = []
    for row in sources:
        kind = str(row.get("kind", ""))
        ref = str(row.get("external_ref", ""))
        url = str(row.get("url", ""))
        title = str(row.get("title", "")).strip()
        excerpt = normalize_body(str(row.get("body", ""))[:280])
        if kind == "release":
            tag = str((row.get("meta") or {}).get("tag") or ref)
            facts.append(Fact(
                claim=f"Release {tag} - {title or tag} esiste nel repository",
                source_url=url, kind="release", excerpt=excerpt))
        elif kind == "commit":
            facts.append(Fact(
                claim=f"Commit {ref.split(':', 1)[-1][:8]}: {title}",
                source_url=url, kind="commit", excerpt=excerpt))
        elif kind in {"issue", "pr"}:
            meta = row.get("meta") or {}
            facts.append(Fact(
                claim=f"{kind} {ref} ({title}) - stato: {meta.get('state', 'unknown')}",
                source_url=url, kind="issue", excerpt=excerpt))
        elif kind == "discussion":
            facts.append(Fact(
                claim=f"Discussion {ref} - {title}", source_url=url,
                kind="discussion", excerpt=excerpt))
        elif kind in {"docs", "readme"}:
            facts.append(Fact(
                claim=f"Il documento {ref} e' pubblicato nel repository",
                source_url=url, kind="docs", excerpt=excerpt))
    return facts


# --------------------------------------------------------------------- checks
#: Percentages first: a bare ``\d{3,}`` would otherwise match the digits of "240%".
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\s?%|\b\d{3,}(?:[.,]\d+)?\b")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff]"
)
_URL = re.compile(r"https?://[^\s)>\]]+")


def unsupported_numbers(body: str, facts: Iterable[Fact]) -> list[str]:
    """Numbers of three or more digits (or percentages) with no fact behind them.

    A fabricated metric is the single most damaging mistake this project could
    make, so the check is deliberately *strict about big numbers and silent about
    small ones*: "3 platforms" needs no citation, "1 240% return" does.
    """
    haystack = " ".join(f"{f.claim} {f.excerpt}" for f in facts)
    haystack = haystack.replace(",", ".").replace(" ", "")
    found: list[str] = []
    for match in _NUMBER.findall(body or ""):
        token = match.replace(",", ".").replace(" ", "")
        if token and token not in haystack and match not in found:
            found.append(match)
    return found


def looks_like_language(text: str, lang: str) -> bool:
    """A cheap sanity check that an ``it`` variant is not silently English.

    Deliberately lenient: a short post is always accepted, and a post is only
    rejected when the *other* language's markers clearly outnumber its own.  The
    cost of a false negative here is a flagged draft; the cost of a false
    positive is an English body shipped as Italian, so the test looks for the
    obvious case - a whole body written in the wrong language.
    """
    padded = f" {normalize_body(text)} "
    if len(padded.split()) < MIN_WORDS_FOR_LANGUAGE_CHECK:
        return True
    markers = ITALIAN_MARKERS if lang == "it" else ENGLISH_MARKERS
    other = ENGLISH_MARKERS if lang == "it" else ITALIAN_MARKERS
    hits = sum(1 for marker in markers if marker in padded)
    misses = sum(1 for marker in other if marker in padded)
    # Only the clear case is a failure: at least two markers of the other
    # language, and at least as many as this one's.
    return misses < 2 or hits >= misses


def clean_body(text: str) -> str:
    """Normalise whitespace and strip the markdown that reads badly as a post."""
    out = str(text or "").replace("\r\n", "\n").strip()
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"^\s*[-*]\s+", "- ", out, flags=re.MULTILINE)
    return out.strip()


def clean_hashtags(text: str, limit: int) -> str:
    tags = [t for t in re.findall(r"#\w+", str(text or ""))]
    seen, ordered = set(), []
    for tag in tags:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(tag)
    return " ".join(ordered[: max(0, limit)])


def count_emojis(text: str) -> int:
    return len(_EMOJI.findall(text or ""))


def links_in(text: str) -> list[str]:
    return _URL.findall(text or "")


# ------------------------------------------------------------------ templates
def _bullets(lines: list[str], limit: int = 4) -> str:
    picked = [f"- {line.strip()}" for line in lines[:limit] if line and line.strip()]
    return "\n".join(picked) if picked else "- (nessun dettaglio disponibile)"


def render_template(brief: ContentBrief, platform: str, lang: str, ctx: dict[str, Any]) -> str:
    """Hand-written copy per category and language.  Separate, never translated.

    A short outage of the source line (an empty summary, an English body with no
    ``summary_en``) must not leave a hole, so the assembled text is tidied before it
    is returned: the copy is never machine-translated and never carries a sentence
    from the other language.
    """
    return re.sub(r"\n{3,}", "\n\n", _template_body(brief, platform, lang, ctx)).strip()


def _template_body(brief: ContentBrief, platform: str, lang: str, ctx: dict[str, Any]) -> str:
    """The per-language copy itself; ``render_template`` normalises the result."""
    brand = ctx.get("brand", "SelfTrade")
    repo = ctx.get("repo_url", "")
    category = str(brief.category)
    bullets_it = _bullets(ctx.get("bullets_it") or ctx.get("bullets") or [])
    bullets_en = _bullets(ctx.get("bullets_en") or ctx.get("bullets") or [])
    tag = ctx.get("tag") or ""
    n_commits = ctx.get("commits")
    # ``link`` is the single allowed link for this platform; docs are a fallback.
    docs_hint = ctx.get("link") or ctx.get("docs_hint", "")
    # The brief's own summary is written in Italian (it is the operator's line and
    # the notes column).  A brief that spans both languages supplies ``summary_it``
    # and ``summary_en``; without that, an English post would carry an Italian
    # sentence in the middle, which is exactly the machine-translation smell the
    # brief warns about.  So the Italian line is used for Italian only: an English
    # variant with no English summary drops the sentence rather than importing one.
    summary = str(ctx.get(f"summary_{lang}") or (brief.summary if lang == "it" else ""))

    if category == Category.RELEASE:
        if lang == "it":
            return (
                f"🚀 {brand} {tag} e' disponibile.\n\n"
                f"{summary}\n\nCosa contiene:\n{bullets_it}\n\n"
                f"Repo: {repo}\n\n"
                "E' un progetto sperimentale: i risultati passati sono simulazioni, "
                "non promesse. Se qualcosa si rompe, apri una issue — e' utile."
            ).strip()
        return (
            f"🚀 {brand} {tag} is out.\n\n{summary}\n\nWhat is in it:\n{bullets_en}\n\n"
            f"Repo: {repo}\n\n"
            "It is an experimental project: past results are simulations, not promises. "
            "If something breaks, open an issue — that is useful."
        ).strip()

    if category == Category.DEV_UPDATE:
        if lang == "it":
            return (
                f"Su {brand} questa settimana: {n_commits or 'alcuni'} commit.\n\n"
                f"{bullets_it}\n\nIl codice e' qui: {repo}"
            ).strip()
        return (
            f"This week on {brand}: {n_commits or 'a few'} commits.\n\n{bullets_en}\n\n"
            f"Code: {repo}"
        ).strip()

    if category == Category.BUILD_IN_PUBLIC:
        if lang == "it":
            return (
                f"Sto costruendo {brand} in pubblico: un ciclo di ricerca quantitativa "
                "che impara, misura e ammette quando non funziona.\n\n"
                f"Cosa e' cambiato adesso:\n{bullets_it}\n\n"
                f"Tutto e' nel repository: {docs_hint}\n"
                "Se ti interessa il tema, dimmi cosa guarderesti per primo."
            ).strip()
        return (
            f"Building {brand} in public: a quantitative-research loop that learns, "
            "measures, and admits when it does not work.\n\n"
            f"What changed just now:\n{bullets_en}\n\n"
            f"All of it lives here: {docs_hint}\n"
            "If this is your field, tell me what you would look at first."
        ).strip()

    if category == Category.TECH_EXPLAINER:
        if lang == "it":
            return (
                f"Come funziona {ctx.get('topic', brand)}.\n\n{summary}\n\n"
                f"{bullets_it}\n\nDocumentazione: {docs_hint}"
            ).strip()
        return (
            f"How {ctx.get('topic', brand)} works.\n\n{summary}\n\n{bullets_en}\n\n"
            f"Docs: {docs_hint}"
        ).strip()

    if category == Category.EXPERIMENT:
        if lang == "it":
            return (
                f"Esperimento su {brand}: {ctx.get('topic', 'una modifica del motore')}.\n\n"
                f"{summary}\n\n{bullets_it}\n\n"
                "Sono risultati su dati storici: non dicono nulla sul futuro.\n"
                f"Metodo e report: {docs_hint}"
            ).strip()
        return (
            f"Experiment on {brand}: {ctx.get('topic', 'a change to the engine')}.\n\n"
            f"{summary}\n\n{bullets_en}\n\n"
            "These are historical-data results: they say nothing about the future.\n"
            f"Method and report: {docs_hint}"
        ).strip()

    if category == Category.LIMITS:
        if lang == "it":
            return (
                f"Cosa non funziona ancora in {brand}.\n\n{bullets_it}\n\n"
            "Lo scrivo perche' un limite dichiarato vale piu' di una demo perfetta.\n"
            f"Dettagli e issue: {docs_hint}"
            ).strip()
        return (
            f"What does not work yet in {brand}.\n\n{bullets_en}\n\n"
            "I am writing this down because a declared limit is worth more than a "
            f"perfect demo.\nDetails and issues: {docs_hint}"
        ).strip()

    if category == Category.DASHBOARD_DEMO:
        if lang == "it":
            return (
                f"La dashboard di {brand}, senza numeri inventati.\n\n{bullets_it}\n\n"
                f"Tutto gira in locale: {docs_hint}"
            ).strip()
        return (
            f"The {brand} dashboard, with no invented numbers.\n\n{bullets_en}\n\n"
            f"It runs locally: {docs_hint}"
        ).strip()

    if category == Category.FEEDBACK_REQUEST:
        if lang == "it":
            return (
                f"Cerco critiche su {brand}, non complimenti.\n\n{bullets_it}\n\n"
                "Se il metodo ti sembra sbagliato, dimmi dove e perche'.\n"
                f"Codice: {docs_hint}"
            ).strip()
        return (
            f"I am looking for criticism of {brand}, not compliments.\n\n{bullets_en}\n\n"
            "If you think the methodology is wrong, say where and why.\n"
            f"Code: {docs_hint}"
        ).strip()

    if category == Category.TUTORIAL:
        if lang == "it":
            return (
                f"Provare {brand} in pochi minuti.\n\n{bullets_it}\n\n"
                f"Guida completa: {docs_hint}"
            ).strip()
        return (
            f"Trying {brand} in a few minutes.\n\n{bullets_en}\n\n"
            f"Full guide: {docs_hint}"
        ).strip()

    if lang == "it":
        return (
            f"{brand} cerca contributi.\n\n{bullets_it}\n\n"
            f"Come iniziare: {docs_hint}"
        ).strip()
    return (
        f"{brand} is looking for contributors.\n\n{bullets_en}\n\n"
        f"How to start: {docs_hint}"
    ).strip()


# --------------------------------------------------------------------- engine
class ContentEngine:
    """Builds briefs from real events and turns them into stored drafts."""

    def __init__(
        self,
        settings: Any,
        db: SocialDB,
        provider: LlmProvider | None = None,
        *,
        visuals: Callable[..., Any] | None = None,
        run_id: str | None = None,
    ):
        self.settings = settings
        self.db = db
        self.provider = provider or UnavailableProvider("not supplied")
        self.visuals = visuals
        self.run_id = run_id or f"content-{to_ts()}"

    # ------------------------------------------------------------- briefing
    def briefs(self, *, limit: int = 6, include_readme: bool = False,
               only_uncovered: bool = True) -> list[ContentBrief]:
        sources = self.db.sources(limit=200)
        kinds = list(IDEA_SOURCE_KINDS)
        usable = [s for s in sources if s["kind"] in kinds]
        covered = covered_refs(self.db) if only_uncovered else set()
        new = [s for s in usable if s["external_ref"] not in covered]

        out: list[ContentBrief] = []
        releases = [s for s in new if s["kind"] == "release"]
        for row in releases[:2]:
            out.append(self._release_brief(row))
        commits = [s for s in new if s["kind"] == "commit"]
        if commits:
            out.append(self._commit_brief(commits))
        issues = [s for s in new if s["kind"] in {"issue", "pr"}]
        if issues:
            out.extend(self._issue_briefs(issues))
        if not out and not new:
            out.append(self._build_in_public_brief(usable))
        docs = [s for s in new if s["kind"] == "docs"]
        if docs and len(out) < 2:
            out.append(self._docs_brief(docs))
        if include_readme:
            readme = [s for s in new if s["kind"] == "readme"]
            if readme:
                out.append(self._docs_brief(readme))
        return out[: max(0, limit)]

    def _release_brief(self, row: dict[str, Any]) -> ContentBrief:
        meta = row.get("meta") or {}
        tag = str(meta.get("tag") or row["external_ref"].split(":", 1)[-1])
        body_lines = [
            line.strip(" -*#") for line in str(row.get("body") or "").splitlines()
            if line.strip() and not line.strip().startswith("[")
        ][:6]
        facts = facts_from_sources([row])
        facts.append(Fact(
            claim=f"Il repository pubblico e' {self.settings.project.repo_url}",
            source_url=self.settings.project.repo_url, kind="repo"))
        return ContentBrief(
            key=f"release:{tag}",
            category=Category.RELEASE,
            title=f"Release {tag}: {row.get('title') or tag}",
            summary=f"{row.get('title') or tag} e' la release piu' recente del progetto.",
            facts=facts,
            sources=[str(row["external_ref"])],
            platforms=list(DEFAULT_TARGETS[Category.RELEASE]),
            risk=BASE_RISK[Category.RELEASE],
            context={
                "tag": tag,
                "release_url": row.get("url") or "",
                "bullets": body_lines,
                "bullets_it": body_lines,
                "bullets_en": body_lines,
                "summary_it": f"{row.get('title') or tag} e' la release piu' recente del progetto.",
                "summary_en": (f"{row.get('title') or tag} is the project's most recent "
                               "release."),
                "prerelease": bool(meta.get("prerelease")),
            },
        )

    def _commit_brief(self, rows: list[dict[str, Any]]) -> ContentBrief:
        rows = rows[:8]
        facts = facts_from_sources(rows)
        bullets = [str(r.get("title") or "") for r in rows]
        return ContentBrief(
            key=f"commits:{rows[0].get('external_ref')}",
            category=Category.DEV_UPDATE,
            title=f"Aggiornamento di sviluppo ({len(rows)} commit)",
            summary="Cosa e' cambiato nel repository nell'ultimo periodo.",
            facts=facts,
            sources=[str(r["external_ref"]) for r in rows],
            platforms=list(DEFAULT_TARGETS[Category.DEV_UPDATE]),
            risk=BASE_RISK[Category.DEV_UPDATE],
            context={
                "commits": len(rows),
                "bullets": bullets,
                "bullets_it": bullets,
                "bullets_en": bullets,
            },
        )

    def _issue_briefs(self, rows: list[dict[str, Any]]) -> list[ContentBrief]:
        out: list[ContentBrief] = []
        open_issues = [r for r in rows if str((r.get("meta") or {}).get("state")) == "open"]
        bugs = [r for r in open_issues
                if any("bug" in str(l).lower() for l in (r.get("meta") or {}).get("labels", []))]
        if bugs:
            out.append(self._issue_brief(
                bugs, Category.LIMITS,
                "Limiti e problemi aperti nel repository",
                "Alcuni problemi noti, dichiarati apertamente."))
        starter = [r for r in open_issues
                   if any("good first" in str(l).lower()
                          for l in (r.get("meta") or {}).get("labels", []))]
        if starter:
            out.append(self._issue_brief(
                starter, Category.CONTRIBUTE,
                "Buone prime issue per contribuire",
                "Punti di ingresso adatti a un primo contributo."))
        if not out and open_issues:
            out.append(self._issue_brief(
                open_issues[:4], Category.FEEDBACK_REQUEST,
                "Domande aperte dal repository",
                "Questioni su cui serve un parere esterno."))
        return out

    def _issue_brief(self, rows: list[dict[str, Any]], category: Category,
                     title: str, summary: str) -> ContentBrief:
        facts = facts_from_sources(rows)
        bullets = [f"#{((r.get('meta') or {}).get('number'))} {r.get('title')}" for r in rows[:5]]
        return ContentBrief(
            key=f"{category}:{rows[0]['external_ref']}",
            category=category,
            title=title,
            summary=summary,
            facts=facts,
            sources=[str(r["external_ref"]) for r in rows],
            platforms=list(DEFAULT_TARGETS.get(category, [str(Platform.X)])),
            risk=BASE_RISK.get(category, Risk.LOW),
            context={"bullets": bullets, "bullets_it": bullets, "bullets_en": bullets,
                     "issue_url": rows[0].get("url") or ""},
        )

    def _build_in_public_brief(self, sources: list[dict[str, Any]]) -> ContentBrief:
        recent = sources[:3]
        facts = facts_from_sources(recent) or [Fact(
            claim=f"Il repository {self.settings.project.repo} e' pubblico",
            source_url=self.settings.project.repo_url, kind="repo")]
        return ContentBrief(
            key="build_in_public:standing",
            category=Category.BUILD_IN_PUBLIC,
            title="Build in public: stato del progetto",
            summary="Nessuna novita' sostanziale: racconto a che punto sono, senza inventare.",
            facts=facts,
            sources=[str(r["external_ref"]) for r in recent],
            platforms=list(DEFAULT_TARGETS[Category.BUILD_IN_PUBLIC]),
            risk=Risk.LOW,
            context={"bullets": [str(r.get("title") or "") for r in recent],
                     "bullets_it": [str(r.get("title") or "") for r in recent],
                     "bullets_en": [str(r.get("title") or "") for r in recent]},
        )

    def _docs_brief(self, rows: list[dict[str, Any]]) -> ContentBrief:
        row = rows[0]
        headings = (row.get("meta") or {}).get("headings") or []
        facts = facts_from_sources([row])
        topic = str(row.get("title") or row["external_ref"]).strip()
        return ContentBrief(
            key=f"docs:{row['external_ref']}",
            category=Category.TECH_EXPLAINER,
            title=f"Spiegazione tecnica: {topic}",
            summary=f"Dal documento {row['external_ref']} del repository.",
            facts=facts,
            sources=[str(row["external_ref"])],
            platforms=list(DEFAULT_TARGETS[Category.TECH_EXPLAINER]),
            risk=BASE_RISK[Category.TECH_EXPLAINER],
            context={
                "topic": topic,
                "docs_hint": str(row.get("url") or ""),
                "summary_it": f"Dal documento {row['external_ref']} del repository.",
                "summary_en": (f"From the repository's own {row['external_ref']} - "
                               "the section headings below are the document's own."),
                "bullets": [str(h) for h in headings[:4]],
                "bullets_it": [str(h) for h in headings[:4]],
                "bullets_en": [str(h) for h in headings[:4]],
            },
        )

    # ------------------------------------------------------------ generation
    def generate(self, brief: ContentBrief) -> ContentIdea:
        base = {
            "brand": self.settings.project.brand,
            "repo_url": self.settings.project.repo_url,
            "docs_hint": self.settings.project.docs_url,
            **brief.context,
        }
        # One link per post, chosen once (policy.max_links_per_post defaults to 1).
        base["link"] = base.get("release_url") or base.get("issue_url") or base["repo_url"]
        ctx = base
        variants: list[VariantDraft] = []
        notes: list[str] = []
        risk = brief.risk
        for platform in brief.platforms:
            for lang in self.settings.editorial.languages:
                draft = self._variant(brief, platform, lang, ctx, notes)
                variants.append(draft)
                if draft.risk in (Risk.HIGH, Risk.SENSITIVE):
                    risk = Risk.SENSITIVE if risk == Risk.SENSITIVE else Risk.HIGH
                elif draft.risk == Risk.MEDIUM and risk == Risk.LOW:
                    risk = Risk.MEDIUM
        media = self._media(brief, ctx)
        for draft in variants:
            if str(draft.platform) == str(Platform.INSTAGRAM) and media:
                draft.media = list(media)
        idea = ContentIdea(
            title=brief.title,
            category=brief.category,
            summary=brief.summary,
            risk=risk,
            facts=brief.facts,
            variants=variants,
            sources=brief.sources,
            planned_for=brief.planned_for,
            notes="; ".join(notes[:4]),
        )
        return idea

    def _variant(self, brief: ContentBrief, platform: str, lang: str,
                 ctx: dict[str, Any], notes: list[str]) -> VariantDraft:
        limits = {"x": 280, "instagram": 2200, "github": 8000}.get(platform, 1000)
        # Instagram captions are not clickable: the single link points at the repo.
        platform_ctx = {**ctx}
        if platform == str(Platform.INSTAGRAM):
            platform_ctx["link"] = ctx["repo_url"]
            platform_ctx["docs_hint"] = ctx["repo_url"]
        generated = self._from_llm(brief, platform, lang, platform_ctx, limits)
        if generated is None:
            body = render_template(brief, platform, lang, platform_ctx)
            generator = "template"
            risk = brief.risk
            hashtags = BASE_HASHTAGS.get(str(brief.category), "") if platform != "github" else ""
            alt_text = f"{self.settings.project.brand}: {brief.title}"
            extra_risk = None
        else:
            body, hashtags, alt_text, extra_risk, generated_risk = generated
            generator = f"llm:{self.provider.model}"
            risk = generated_risk or brief.risk

        body = clean_body(body)
        if not looks_like_language(body, lang):
            notes.append(f"{platform}/{lang}: la lingua non sembra {lang}")
            risk = Risk.HIGH if risk in (Risk.LOW, Risk.MEDIUM) else risk
        unsupported = unsupported_numbers(body, brief.facts)
        if unsupported:
            notes.append(f"{platform}/{lang}: numeri non coperti dalle fonti: "
                         + ", ".join(unsupported[:3]))
            risk = Risk.HIGH if risk == Risk.LOW else risk
        if len(body) > limits:
            body = body[: limits - 1].rstrip() + "…"
            notes.append(f"{platform}/{lang}: troncato al limite di {limits} caratteri")
        hashtags = clean_hashtags(
            hashtags, self.settings.policy.max_hashtags.get(platform, 3)
        )
        return VariantDraft(
            platform=Platform(platform),
            lang=lang,
            body=body,
            hashtags=hashtags,
            generator=generator,
            risk=risk,
            alt_text=(alt_text or "")[:1000],
        )

    def _from_llm(self, brief: ContentBrief, platform: str, lang: str,
                  ctx: dict[str, Any], limit: int
                  ) -> tuple[str, str, str, None, Risk] | None:
        if not self.provider.available():
            return None
        facts = "\n".join(f"- {f.claim} [{f.source_url}]" for f in brief.facts[:12])
        user = (
            f"PLATFORM: {platform} (body limit {limit} characters)\n"
            f"LANGUAGE: {'Italian' if lang == 'it' else 'English'}\n"
            f"CATEGORY: {brief.category} ({brief.title})\n"
            f"CONTEXT (IT): {ctx.get('summary_it') or brief.summary}\n"
            f"CONTEXT (EN): {ctx.get('summary_en') or brief.summary}\n\n"
            f"FACTS (the only claims you may make):\n{facts}\n\n"
            f"Links you may use: {ctx.get('repo_url')} , {ctx.get('release_url', '')} , "
            f"{ctx.get('docs_hint', '')}\n"
            f"Rules for this platform: "
            + ("one link at most, max 3 hashtags" if platform == "x"
               else "caption up to 2200 characters, hashtags in the separate field"
               if platform == "instagram"
               else "markdown is allowed, be concrete about what changed")
            + "\n\nReturn JSON: {body, hashtags, alt_text, risk, unsupported}."
        )
        result = self.provider.chat_json(SYSTEM_RULES, user, VARIANT_SCHEMA)
        if not result.ok or not result.data:
            obs.warning("content.llm_failed",
                        detail={"platform": platform, "lang": lang, "error": result.error})
            return None
        body = str(result.data.get("body") or "").strip()
        if not body:
            return None
        unknown = result.data.get("unsupported")
        if unknown:
            obs.warning("content.llm_declared_unsupported",
                        detail={"platform": platform, "lang": lang,
                                "items": str(unknown)[:300]})
        try:
            risk = Risk(str(result.data.get("risk") or brief.risk))
        except ValueError:
            risk = brief.risk
        if risk == Risk.LOW and brief.risk == Risk.MEDIUM:
            risk = Risk.MEDIUM
        return (
            body,
            str(result.data.get("hashtags") or ""),
            str(result.data.get("alt_text") or ""),
            None,
            risk,
        )

    def _media(self, brief: ContentBrief, ctx: dict[str, Any]) -> list[str]:
        if self.visuals is None:
            return []
        if str(brief.category) not in MEDIA_CATEGORIES:
            return []
        try:
            paths = self.visuals(brief, ctx)
        except Exception as exc:  # noqa: BLE001 - a missing image must not stop a draft
            obs.warning("content.visual_failed", detail={"error": type(exc).__name__})
            return []
        return [str(p) for p in (paths or [])]

    # ------------------------------------------------------------- persisting
    def persist(self, idea: ContentIdea, *, state: str = "draft") -> int:
        facts = [f.to_dict() for f in idea.facts]
        item_id = self.db.create_item(
            idea.title,
            str(idea.category),
            summary=idea.summary,
            risk=idea.risk,
            state=state,
            facts=facts,
            source_refs=idea.sources,
            planned_for=idea.planned_for,
            origin="agent",
            notes=idea.notes,
        )
        for variant in idea.variants:
            self.db.upsert_variant(
                item_id,
                variant.platform,
                variant.lang,
                variant.body,
                hashtags=variant.hashtags,
                media=variant.media,
                alt_text=variant.alt_text,
                generator=variant.generator,
                risk=variant.risk,
                content_hash=variant.content_hash(),
            )
        self.db.audit(
            "content.generated",
            actor="content-engine",
            subject=f"content_item:{item_id}",
            detail={
                "category": str(idea.category),
                "risk": str(idea.risk),
                "variants": len(idea.variants),
                "sources": idea.sources[:6],
                "generator": sorted({v.generator for v in idea.variants}),
            },
            run_id=self.run_id,
        )
        return item_id

    def run(self, *, limit: int = 6, only_uncovered: bool = True) -> GenerationReport:
        report = GenerationReport(run_id=self.run_id)
        report.provider = self.provider.describe()
        briefs = self.briefs(limit=limit, only_uncovered=only_uncovered)
        report.briefs = len(briefs)
        for brief in briefs:
            try:
                idea = self.generate(brief)
            except Exception as exc:  # noqa: BLE001 - one bad brief must not stop a run
                report.errors.append(f"{brief.key}: {type(exc).__name__}: {exc}")
                continue
            if not idea.variants:
                report.skipped.append(f"{brief.key}: no targets configured")
                continue
            item_id = self.persist(idea)
            report.items.append(item_id)
            report.variants += len(idea.variants)
        obs.info("content.run", detail=report.to_dict())
        return report
