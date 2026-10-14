"""The community agent: read feedback, classify it, and propose - never spam.

The brief's prohibitions are implemented as code paths that do not exist rather
than as good intentions:

* **no automatic issue creation.**  :meth:`CommunityAgent.propose_issues` returns
  *proposals*; creating an issue needs ``autonomy.allow_issue_proposals`` and a
  human approval, and the GitHub connector has no write method at all in this
  phase.
* **no repeat replies.**  :meth:`reply_draft` refuses when the same author
  already received a reply, and every reply goes to the approval queue.
* **no mass messaging / engagement farming.**  :meth:`ingest` only reads what the
  API returns; there is no "search for people talking about trading" path, and
  the policy engine blocks follow-for-follow phrasing.

Classification is deterministic first: keyword rules in both languages, a
priority from severity words and source, and a *cluster key* built from the
normalised topic words so that three separate reports of the same Windows install
failure become one row in Feedback Intelligence instead of three.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable

from . import obs
from .db import SocialDB, to_ts, utcnow
from .models import Fact, Platform, Risk, normalize_body

#: Feedback categories from the brief.
CATEGORIES = (
    "bug_report",
    "feature_request",
    "install_issue",
    "security",
    "methodology_critique",
    "question",
    "suggestion",
    "praise",
    "spam",
)

PRIORITY_ORDER = ("critical", "high", "normal", "low")

#: Keyword rules, Italian and English together: the audience writes both.
RULES: dict[str, tuple[str, ...]] = {
    "security": ("security", "vulnerability", "cve", "exploit", "sicurezza", "vulnerabilita",
                 "leak", "leaked", "token exposed", "private key"),
    "install_issue": ("pip install", "does not install", "won't install", "cannot install",
                      "importerror", "modulenotfounderror", "no module named",
                      "installazione", "non si installa", "errore di installazione",
                      "setup fails", "dependency"),
    "bug_report": ("traceback", "exception", "crash", "bug", "errore", "non funziona",
                   "broken", "regression", "fails", "fallisce", "stacktrace"),
    "feature_request": ("feature request", "would be nice", "please add", "add support",
                        "sarebbe utile", "potreste aggiungere", "request", "supporto per"),
    "methodology_critique": ("overfitting", "look-ahead", "lookahead", "survivorship",
                             "data snooping", "snooping", "p-hack", "methodology",
                             "metodologia", "statistically", "statisticamente",
                             "no transaction costs", "slippage", "not realistic",
                             "non e' realistico", "curve fitting"),
    "question": ("how do i", "how to", "come si", "come faccio", "?"),
    "praise": ("thanks", "thank you", "great work", "awesome", "ottimo", "grazie",
               "bello", "nice work", "cool"),
    "spam": ("crypto giveaway", "dm me", "telegram me", "click here", "free money",
             "invest now", "seguimi", "follow back", "promo", "discount code",
             "viagra", "casino"),
    "suggestion": ("suggestion", "maybe you could", "consider", "suggerimento",
                   "potresti", "io userei", "would help"),
}

#: How a category maps onto a starting priority.
BASE_PRIORITY = {
    "security": "critical",
    "bug_report": "high",
    "install_issue": "high",
    "methodology_critique": "normal",
    "feature_request": "normal",
    "question": "normal",
    "suggestion": "low",
    "praise": "low",
    "spam": "low",
}

URGENT_WORDS = ("data loss", "perdita di dati", "crash on start", "cannot start",
                "non si avvia", "blocker", "urgent", "urgente", "corrupted")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "with", "without", "this", "that",
    "is", "are", "was", "were", "be", "been", "to", "of", "in", "on", "at", "it",
    "i", "you", "we", "they", "he", "she", "my", "your", "our", "their", "when",
    "what", "how", "why", "not", "no", "yes", "do", "does", "did", "can", "could",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "e", "ed", "che", "non",
    "per", "con", "su", "da", "in", "di", "del", "della", "dei", "delle", "come",
    "quando", "perche", "anche", "ma", "se", "mi", "ti", "si", "ho", "hai", "ha",
}


@dataclass
class Classification:
    category: str
    priority: str
    summary: str
    sentiment: str
    cluster_key: str
    matched: list[str] = field(default_factory=list)
    confidence: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "priority": self.priority,
            "summary": self.summary,
            "sentiment": self.sentiment,
            "cluster_key": self.cluster_key,
            "matched": list(self.matched),
            "confidence": self.confidence,
        }


def keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Zàèéìòù']{4,}", normalize_body(text))
    return [w for w in words if w not in STOPWORDS][:40]


def cluster_key(text: str) -> str:
    """A topic signature: the first three distinct content words, sorted.

    Three, and only the *first* few, on purpose: two people reporting the same
    install failure write "ModuleNotFoundError" and "dependencies" where the
    other wrote one and not the other, and a fourth keyword splits the cluster
    precisely when the reports agree on the symptom.
    """
    seen: list[str] = []
    for word in keywords(text):
        if word not in seen:
            seen.append(word)
    return "-".join(sorted(seen[:3]))


def classify(body: str, *, source: str = "github", comments: int = 0) -> Classification:
    """Deterministic classification, in Italian and English, with no model."""
    text = normalize_body(body or "")
    matched: dict[str, list[str]] = {}
    for category, needles in RULES.items():
        hits = [needle for needle in needles if needle in text]
        if hits:
            matched[category] = hits
    if not matched:
        category = "question" if len(keywords(text)) >= 3 else "spam"
    else:
        # Security beats everything; spam is only chosen when nothing else fits.
        order = ["security", "install_issue", "bug_report", "methodology_critique",
                 "feature_request", "question", "suggestion", "praise", "spam"]
        category = next(c for c in order if c in matched)
        if category == "spam" and any(
            other in matched for other in ("bug_report", "install_issue", "question")
        ):
            category = "bug_report"
    priority = BASE_PRIORITY.get(category, "normal")
    if any(word in text for word in URGENT_WORDS):
        priority = "critical"
    if category == "bug_report" and comments >= 3:
        priority = "critical" if priority == "high" else "high"
    if source == "x" and category in {"bug_report", "install_issue"}:
        priority = "high"
    negative = ("broken", "fails", "error", "errore", "non funziona", "worst", "pessimo",
                "useless", "inutile", "disappointed", "deluso", "vulnerab", "leak",
                "exposed", "regression", "crash")
    positive = ("thanks", "grazie", "great", "ottimo", "love", "adoro", "nice", "bello")
    sentiment = (
        "negative" if any(w in text for w in negative)
        else "positive" if any(w in text for w in positive)
        else "neutral"
    )
    first_line = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
    summary = re.sub(r"\s+", " ", first_line)[:180]
    return Classification(
        category=category,
        priority=priority,
        summary=summary,
        sentiment=sentiment,
        cluster_key=cluster_key(body) if category != "spam" else "spam",
        matched=matched.get(category, []),
    )


class CommunityAgent:
    def __init__(self, settings: Any, db: SocialDB, *, github: Any = None, x: Any = None,
                 instagram: Any = None, notify: Any = None, run_id: str | None = None):
        self.settings = settings
        self.db = db
        self.github = github
        self.x = x
        self.instagram = instagram
        self.notify = notify
        self.run_id = run_id

    # ---------------------------------------------------------------- ingesting
    def ingest_github(self, *, since_days: int = 30, limit: int = 50) -> dict[str, Any]:
        """Issues *and their comments* become feedback items.  Read-only."""
        # Reading the *stored* issues needs no live connector: the sync is what
        # talks to GitHub, and this step works on what it left behind.
        out = {"fetched": 0, "new": 0, "classified": 0, "skipped": 0, "errors": []}
        if self.db.count("content_source", "kind IN ('issue','pr')") == 0:
            out["errors"].append("no issue sources stored: run `sync` first")
            return out
        rows = [s for s in self.db.sources(limit=500) if s["kind"] in {"issue", "pr"}]
        for row in rows[:limit]:
            meta = row.get("meta") or {}
            if str(meta.get("state")) == "closed" and not meta.get("comments"):
                out["skipped"] += 1
                continue
            out["fetched"] += 1
            feedback_id, created = self.db.upsert_feedback(
                "github",
                f"{row['external_ref']}:body",
                row.get("body") or row.get("title") or "",
                url=str(row.get("url") or ""),
                author=str(row.get("author") or ""),
                created_at_platform=row.get("published_at"),
                meta={"kind": row["kind"], "number": meta.get("number"),
                      "labels": meta.get("labels") or []},
            )
            if created:
                out["new"] += 1
            result = classify(
                f"{row.get('title', '')}\n{row.get('body', '')}",
                source="github", comments=int(meta.get("comments") or 0),
            )
            if self._apply(feedback_id, result, source="github"):
                out["classified"] += 1
        obs.info("community.ingest_github", detail=out)
        return out

    def ingest_x_mentions(self, *, user_id: str, limit: int = 20) -> dict[str, Any]:
        out = {"fetched": 0, "new": 0, "classified": 0, "errors": []}
        if self.x is None:
            out["errors"].append("x connector not configured")
            return out
        mentions = self.x.mentions(user_id, limit=limit)
        for mention in mentions:
            external_id = str(mention.get("id") or "")
            if not external_id:
                continue
            out["fetched"] += 1
            feedback_id, created = self.db.upsert_feedback(
                "x", external_id, str(mention.get("text") or ""),
                url=f"https://x.com/i/status/{external_id}",
                author=str(mention.get("author_id") or ""),
                created_at_platform=str(mention.get("created_at") or ""),
                meta={"conversation_id": mention.get("conversation_id")},
            )
            if created:
                out["new"] += 1
            if self._apply(feedback_id, classify(str(mention.get("text") or ""), source="x"),
                           source="x"):
                out["classified"] += 1
        return out

    def ingest_instagram_comments(self, *, media_id: str, limit: int = 25) -> dict[str, Any]:
        out = {"fetched": 0, "new": 0, "classified": 0, "errors": []}
        if self.instagram is None:
            out["errors"].append("instagram connector not configured")
            return out
        for comment in self.instagram.comments(media_id, limit=limit):
            external_id = str(comment.get("id") or "")
            if not external_id:
                continue
            out["fetched"] += 1
            feedback_id, created = self.db.upsert_feedback(
                "instagram", external_id, str(comment.get("text") or ""),
                author=str(comment.get("username") or ""),
                created_at_platform=str(comment.get("timestamp") or ""),
                meta={"media_id": media_id, "like_count": comment.get("like_count")},
            )
            if created:
                out["new"] += 1
            if self._apply(feedback_id, classify(str(comment.get("text") or ""), source="instagram"),
                           source="instagram"):
                out["classified"] += 1
        return out

    def _apply(self, feedback_id: int, result: Classification, *, source: str) -> bool:
        self.db.classify_feedback(
            feedback_id,
            category=result.category,
            priority=result.priority,
            summary=result.summary,
            sentiment=result.sentiment,
            cluster_key=result.cluster_key,
            state="triaged",
        )
        self.db.audit(
            "community.classified", actor="community-agent", subject=f"feedback:{feedback_id}",
            detail={"category": result.category, "priority": result.priority,
                    "cluster": result.cluster_key, "source": source},
            run_id=self.run_id,
        )
        if self.notify is not None and (result.priority == "critical"
                                        or result.category == "security"):
            try:
                self.notify.feedback_urgent(feedback_id, result.category, result.summary)
            except Exception:  # noqa: BLE001
                pass
        return True

    def classify_all(self, *, limit: int = 200) -> int:
        """Re-classify rows that were stored before the rules existed."""
        changed = 0
        for row in self.db.feedback(state="new", limit=limit):
            result = classify(row["body"], source=str(row["platform"]))
            self._apply(int(row["id"]), result, source=str(row["platform"]))
            changed += 1
        return changed

    # ------------------------------------------------------------------ replies
    def reply_draft(self, feedback_id: int, *, lang: str | None = None) -> dict[str, Any]:
        """A *proposal*: a reply text, its risks, and why a human must send it."""
        row = self.db.get("feedback_item", feedback_id)
        if row is None:
            raise KeyError(f"feedback {feedback_id} not found")
        author = str(row.get("author") or "")
        if author and self._has_replied(author, str(row["platform"])):
            return {
                "ok": False,
                "reason": f"@ {author} already received a reply on {row['platform']}: "
                          "replying again is not allowed",
            }
        if str(row.get("category")) == "spam":
            return {"ok": False, "reason": "spam is never answered"}
        language = lang or ("it" if (row.get("lang") or "it") == "it" else "en")
        category = str(row.get("category"))
        if language == "it":
            body = (
                f"Grazie per il messaggio su {self.settings.project.brand}.\n\n"
                f"Sul punto \"{(row.get('summary') or '')[:90]}\": "
                + _reply_body_it(category, self.settings)
            )
        else:
            body = (
                f"Thanks for the note about {self.settings.project.brand}.\n\n"
                f"On \"{(row.get('summary') or '')[:90]}\": "
                + _reply_body_en(category, self.settings)
            )
        draft = {
            "feedback_id": feedback_id,
            "platform": row["platform"],
            "lang": language,
            "body": body[:600],
            "category": category,
            "risk": "medium" if category in {"bug_report", "install_issue",
                                             "methodology_critique"} else "low",
            "requires_approval": True,
            "reason": "outbound replies are held for a human in every autonomy mode",
        }
        self.db.audit(
            "community.reply_drafted", actor="community-agent", subject=f"feedback:{feedback_id}",
            detail={"category": category, "lang": language},
            run_id=self.run_id,
        )
        return {"ok": True, "draft": draft}

    def _has_replied(self, author: str, platform: str) -> bool:
        rows = self.db.all(
            """SELECT meta FROM published_post
               WHERE platform = ? AND json_extract(meta, '$.reply_author') = ?""",
            [str(platform), author],
        )
        return bool(rows)

    def record_reply(self, author: str, platform: str, external_id: str, url: str = "") -> int:
        """Called *after* a human sent a reply, so the no-repeat rule has data."""
        return self.db.insert("published_post", {
            "platform": str(platform),
            "external_id": external_id or None,
            "url": url,
            "content_hash": f"reply:{author}:{external_id}",
            "state": "published",
            "dedupe_key": f"reply:{platform}:{author}:{external_id}",
            "published_at": to_ts(),
            "meta": json.dumps({"reply_author": author, "kind": "reply"}),
        })

    # ------------------------------------------------------------- intelligence
    def groups(self, *, min_size: int = 2) -> list[dict[str, Any]]:
        clusters = self.db.feedback_clusters(min_size=min_size)
        out = []
        for cluster in clusters:
            items = self.db.all(
                """SELECT id, platform, author, summary, priority, url
                   FROM feedback_item WHERE cluster_key = ? ORDER BY id DESC LIMIT 10""",
                [cluster["cluster_key"]],
            )
            out.append({
                "cluster_key": cluster["cluster_key"],
                "category": cluster["category"],
                "count": int(cluster["n"]),
                "items": items,
                "suggested_action": _cluster_action(str(cluster["category"]), int(cluster["n"])),
            })
        return out

    def propose_issues(self, *, min_size: int = 3, limit: int = 25) -> dict[str, Any]:
        """Draft issues for recurring problems - as proposals, never as API calls."""
        if not self.settings.autonomy.allow_issue_proposals:
            return {
                "ok": False,
                "reason": "autonomy.allow_issue_proposals is false: proposals are recorded "
                          "but never opened on GitHub, and this build has no issue-writing code",
                "proposals": [],
            }
        proposals = []
        for group in self.groups(min_size=min_size)[:limit]:
            issue = {
                "title": f"[feedback] {group['category'].replace('_', ' ')}: {group['count']} reports",
                "body": _issue_body(group),
                "labels": _issue_labels(group["category"]),
                "based_on": [item["id"] for item in group["items"]],
                "duplicate_check": "already open",
                "requires_approval": True,
            }
            proposals.append(issue)
        self.db.audit(
            "community.issues_proposed", actor="community-agent", subject="github",
            detail={"count": len(proposals)},
            run_id=self.run_id,
        )
        return {"ok": True, "reason": "proposals require a human before they are opened",
                "proposals": proposals}

    def report(self, *, days: int = 7) -> dict[str, Any]:
        since = to_ts(utcnow() - timedelta(days=max(1, int(days))))
        rows = self.db.all(
            "SELECT category, priority, COUNT(*) AS n FROM feedback_item "
            "WHERE ingested_at >= ? GROUP BY category, priority",
            [since],
        )
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(str(row["category"]), {})[str(row["priority"])] = int(row["n"])
        total = self.db.scalar(
            "SELECT COUNT(*) FROM feedback_item WHERE ingested_at >= ?", [since], 0)
        by_platform = {
            str(r["platform"]): int(r["n"]) for r in self.db.all(
                "SELECT platform, COUNT(*) AS n FROM feedback_item WHERE ingested_at >= ? "
                "GROUP BY platform", [since])
        }
        return {
            "window_days": int(days),
            "since": since,
            "total": int(total),
            "by_category": counts,
            "by_platform": by_platform,
            "open_critical": self.db.count(
                "feedback_item", "priority = 'critical' AND state <> 'ignored'"),
            "clusters": self.groups(min_size=2)[:10],
            "proposals": self.propose_issues(),
            "note": ("counts only ever include feedback this project actually received "
                     "through an API; nothing here is estimated"),
        }


def _reply_body_it(category: str, settings: Any) -> str:
    docs = settings.project.docs_url
    if category == "bug_report":
        return ("l'ho registrato come bug. Se puoi, allega il comando esatto e l'output completo: "
                f"cosi' posso riprodurlo. Stato e limiti noti sono in {docs}.")
    if category == "install_issue":
        return ("i problemi di installazione sono i piu' utili che possiamo ricevere. Quale "
                "sistema operativo e quale versione di Python? Aggiungo il caso alla guida "
                "di setup appena riusciamo a riprodurlo.")
    if category == "methodology_critique":
        return ("e' una critica giusta da fare. Il progetto misura su dati storici con "
                "commissioni e slippage simulati, e il report dice dove il metodo cede: "
                "se vedi un errore specifico, indica il file e la riga e lo correggo.")
    if category == "feature_request":
        return "lo tengo come proposta: non e' in sviluppo finche' non e' nel repository."
    if category == "security":
        return ("grazie per la segnalazione: per i problemi di sicurezza preferisco il canale "
                "privato indicato in SECURITY.md, cosi' non resta pubblico finche' non e' corretto.")
    if category == "praise":
        return "grazie. Se vuoi essere utile davvero, la critica piu' dura e' quella che serve."
    return "rispondo volentieri: se la domanda riguarda l'uso, la guida e' la strada piu' rapida."


def _reply_body_en(category: str, settings: Any) -> str:
    docs = settings.project.docs_url
    if category == "bug_report":
        return ("noted as a bug. If you can, include the exact command and the full output so it "
                f"can be reproduced. Known limits are in {docs}.")
    if category == "install_issue":
        return ("installation problems are the most useful thing we can get. Which OS and which "
                "Python version? I will add the case to the setup guide once it reproduces.")
    if category == "methodology_critique":
        return ("that is a fair criticism to make. The project measures on historical data with "
                "simulated fees and slippage, and the reports show where the method gives up; "
                "if you see a specific mistake, name the file and line and I will fix it.")
    if category == "feature_request":
        return "keeping it as a proposal: it is not in development until it is in the repository."
    if category == "security":
        return ("thanks for the report: for security issues I would rather use the private "
                "channel in SECURITY.md so it does not stay public before it is fixed.")
    if category == "praise":
        return "thank you. If you want to be useful, the harshest criticism is the one that helps."
    return "happy to answer: if it is a usage question, the guide is the fastest route."


def _cluster_action(category: str, count: int) -> str:
    if category == "security":
        return "escalate privately to SECURITY.md; do not discuss details in public"
    if category == "bug_report" and count >= 3:
        return "propose one issue that groups the reports, then link each comment"
    if category == "methodology_critique":
        return "answer with the specific evidence, or record the limitation in the docs"
    if category == "install_issue" and count >= 2:
        return "improve the setup documentation; the recurring case is the spec"
    if category == "feature_request":
        return "add to the roadmap discussion, do not promise a date"
    return "review manually"


def _issue_body(group: dict[str, Any]) -> str:
    lines = [
        f"Recurring feedback ({group['count']} reports, cluster `{group['cluster_key']}`).",
        "",
        "Sources:",
    ]
    for item in group["items"]:
        lines.append(f"- {item['platform']} {item['url'] or '(no url)'} — {item['summary']}")
    lines += ["", "Collected through the official APIs; no user was contacted automatically."]
    return "\n".join(lines)


def _issue_labels(category: str) -> list[str]:
    return {
        "bug_report": ["bug", "feedback"],
        "install_issue": ["documentation", "feedback"],
        "methodology_critique": ["discussion", "methodology"],
        "feature_request": ["enhancement", "feedback"],
        "security": ["security"],
    }.get(category, ["feedback"])
