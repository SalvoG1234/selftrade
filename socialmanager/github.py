"""The GitHub connector: read-only, and structurally unable to be anything else.

The mission forbids the Social Manager from touching code, merges or issue
administration.  That is enforced here rather than promised in a document:
every request goes through :meth:`GitHubConnector._read`, which refuses any
method other than ``GET``, and :meth:`_graphql`, which is the single, audited
exception because GitHub's GraphQL API *is* a POST - and it only ever sends a
``query`` document, with no ``mutation`` accepted.

What it collects, and why each kind exists:

===================  ===============================================================
``release``          the only kind that reliably means "something shipped";
``commit``           development activity, filtered to substantive messages;
``issue``            questions and bug reports; also where feedback starts;
``discussion``       opt-in, needs a token; the richest technical conversation;
``readme``           what the project currently *claims*, which the content
                     engine must not contradict.
===================  ===============================================================

Nothing is generated here.  The connector's output is rows in ``content_source``,
and a report of what was new, what changed and what could not be read - the
content engine decides what (if anything) is worth saying.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable

from . import obs, secrets
from .db import SocialDB, jdumps, jloads, parse_ts, to_ts, utcnow
from .http import HttpClient, Response, Transport

REQUIRED_SCOPES = ("public_repo (read)",)  # only when a token is used at all
DISCUSSION_SCOPES = ("read:discussion",)

#: Commit messages that carry no news value: skip them so the calendar is not spam.
NOISE_PREFIXES = (
    "merge ", "merge branch", "merge pull request", "revert \"", "wip", "fixup!",
    "chore(deps)", "bump ", "release ", "docs: typo", "typo",
)
MIN_COMMIT_CHARS = 12


def _iso_or_none(value: str | None) -> str | None:
    """Normalise a platform timestamp to our UTC format, or pass it through."""
    parsed = parse_ts(value)
    return to_ts(parsed) if parsed else (value or None)


@dataclass
class SyncReport:
    """What one sync actually did - the shape the dashboard and the CLI print."""

    repository: str
    authenticated: bool
    fetched: dict[str, int] = field(default_factory=dict)
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rate_limit: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "authenticated": self.authenticated,
            "fetched": dict(self.fetched),
            "new": list(self.new),
            "changed": list(self.changed),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "rate_limit": dict(self.rate_limit),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class GitHubConnector:
    """Read a repository's public signals into ``content_source``."""

    def __init__(
        self,
        settings: Any,
        db: SocialDB | None = None,
        transport: Transport | None = None,
        run_id: str | None = None,
        sleep=None,
    ):
        self.settings = settings
        self.db = db
        self.cfg = settings.github
        self.repo = str(settings.project.repo)
        self.run_id = run_id
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.http = HttpClient(
            connector="github",
            db=db,
            transport=transport,
            timeout=self.cfg.timeout,
            max_retries=2,
            backoff_s=0.6,
            user_agent=self.cfg.user_agent,
            secret_names=(self.cfg.token_env,),
            run_id=run_id,
            **kwargs,
        )

    # ------------------------------------------------------------------ status
    @property
    def token(self) -> str | None:
        return secrets.read(self.cfg.token_env)

    def readiness(self) -> dict[str, Any]:
        """Names, never values: what is configured and what is missing."""
        token = self.token is not None
        return {
            "connector": "github",
            "enabled": bool(self.cfg.enabled),
            "repository": self.repo,
            "api_base": self.cfg.api_base,
            "authenticated": token,
            "token_env": self.cfg.token_env,
            "token_present": token,
            "token_fingerprint": secrets.fingerprint_of_env(self.cfg.token_env),
            "discussions_enabled": bool(self.cfg.discussions),
            "mode": "authenticated" if token else "anonymous",
            "writes": "none: this connector only issues GET (and read-only GraphQL queries)",
            "missing": [] if token else [
                f"{self.cfg.token_env} is not set - public reads still work at "
                "60 requests/hour and discussions are unavailable"
            ],
            "rate_limit_hint": "5000/h authenticated, 60/h anonymous",
            "scopes_needed": list(REQUIRED_SCOPES) + (
                list(DISCUSSION_SCOPES) if self.cfg.discussions else []
            ),
        }

    def health(self) -> dict[str, Any]:
        """One cheap call that proves the connector can read - and nothing more."""
        response = self._read("/repos/{repo}", expect_404_ok=True)
        if response is None:
            return {"ok": False, "detail": "no response"}
        data = response.json() or {}
        return {
            "ok": response.ok,
            "status": response.status,
            "repo": data.get("full_name"),
            "private": data.get("private"),
            "default_branch": data.get("default_branch"),
            "stars": (data.get("stargazers_count") if response.ok else None),
            "open_issues": (data.get("open_issues_count") if response.ok else None),
            "error": response.error,
            "remaining": response.int_header("x-ratelimit-remaining", -1),
        }

    # ------------------------------------------------------------------- reads
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        headers.update(self.http.bearer(self.cfg.token_env))
        if extra:
            headers.update(extra)
        return headers

    def _read(self, path: str, params: dict[str, Any] | None = None,
              accept: str | None = None, expect_404_ok: bool = False) -> Response | None:
        """The only way this class is allowed to touch the network."""
        url = f"{self.cfg.api_base}{path.format(repo=self.repo)}"
        extra = {"Accept": accept} if accept else None
        response = self.http.get(url, params=params, headers=self._headers(extra))
        if response is None:
            return None
        if not response.ok and not (expect_404_ok and response.status == 404):
            obs.warning(
                "github.read_failed",
                connector="github",
                detail={"path": path, "status": response.status, "error": response.error},
            )
        return response

    def _graphql(self, query: str, variables: dict[str, Any]) -> Response | None:
        if "mutation" in query.lower():
            raise ValueError("this connector only sends read-only GraphQL queries")
        if not self.token:
            return None
        return self.http.graphql(
            f"{self.cfg.api_base}/graphql",
            query,
            variables,
            headers=self._headers(),
            timeout=self.cfg.timeout,
        )

    # ------------------------------------------------------------------- sync
    def sync(
        self,
        *,
        kinds: Iterable[str] = ("release", "commit", "issue", "discussion", "readme"),
        since_days: int | None = None,
        store: bool = True,
    ) -> SyncReport:
        """Fetch the configured kinds and store them.  Never raises for HTTP errors."""
        kinds = {str(k) for k in kinds}
        report = SyncReport(
            repository=self.repo,
            authenticated=self.token is not None,
            started_at=to_ts(),
        )
        days = int(since_days if since_days is not None else self.cfg.since_days)

        if not self.cfg.enabled:
            report.skipped.append("connector disabled in configuration")
            report.finished_at = to_ts()
            return report

        if "readme" in kinds and self.cfg.include_readme:
            self._sync_readme(report, store)
        if "release" in kinds:
            self._sync_releases(report, store)
        if "commit" in kinds:
            self._sync_commits(report, days, store)
        if "issue" in kinds:
            self._sync_issues(report, days, store)
        if "discussion" in kinds:
            self._sync_discussions(report, store)
        self._sync_repo_stats(report)

        report.finished_at = to_ts()
        if self.db is not None and store:
            self.db.audit(
                "github.sync",
                actor="github-connector",
                subject=self.repo,
                detail={
                    "fetched": report.fetched,
                    "new": len(report.new),
                    "changed": len(report.changed),
                    "errors": report.errors[:5],
                },
                run_id=self.run_id,
            )
        return report

    # ------------------------------------------------------------- each kind
    def _sync_readme(self, report: SyncReport, store: bool) -> None:
        response = self._read("/repos/{repo}/readme", expect_404_ok=True)
        if response is None or not response.ok:
            report.errors.append(
                f"readme: {response.status if response else 0} {response.error if response else ''}".strip()
            )
            return
        data = response.json() or {}
        content = ""
        if data.get("encoding") == "base64" and data.get("content"):
            try:
                content = base64.b64decode(data["content"]).decode("utf-8", "replace")
            except (ValueError, TypeError):
                content = ""
        elif isinstance(data.get("content"), str):
            content = data["content"]
        if not content:
            report.skipped.append("readme: empty or unreadable content")
            return
        report.fetched["readme"] = 1
        self._store(
            report, "readme", f"readme:{data.get('path', 'README.md')}",
            url=str(data.get("html_url") or self.settings.project.repo_url),
            title="README",
            body=content,
            published_at=None,
            meta={"sha": data.get("sha"), "path": data.get("path")},
            store=store,
        )

    def _sync_releases(self, report: SyncReport, store: bool) -> None:
        limit = max(0, int(self.cfg.releases_limit))
        if limit == 0:
            return
        response = self._read(
            "/repos/{repo}/releases",
            {"per_page": min(100, limit)},
        )
        if response is None or not response.ok:
            report.errors.append(
                f"releases: {response.status if response else 0} {response.error if response else ''}".strip()
            )
            return
        releases = response.json() or []
        if not isinstance(releases, list):
            report.errors.append("releases: unexpected payload")
            return
        report.fetched["release"] = len(releases)
        self._update_rate(report, response)
        for release in releases:
            tag = str(release.get("tag_name") or release.get("name") or release.get("id"))
            self._store(
                report, "release", f"release:{tag}",
                url=str(release.get("html_url", "")),
                title=str(release.get("name") or tag),
                author=str((release.get("author") or {}).get("login", "")),
                body=str(release.get("body") or ""),
                published_at=str(release.get("published_at") or release.get("created_at") or ""),
                meta={
                    "tag": tag,
                    "draft": bool(release.get("draft")),
                    "prerelease": bool(release.get("prerelease")),
                    "target": release.get("target_commitish"),
                },
                store=store,
            )

    def _sync_commits(self, report: SyncReport, since_days: int, store: bool) -> None:
        limit = max(0, int(self.cfg.commits_limit))
        if limit == 0:
            return
        since = (utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self._read(
            "/repos/{repo}/commits",
            {"since": since, "per_page": min(100, limit)},
        )
        if response is None or not response.ok:
            report.errors.append(
                f"commits: {response.status if response else 0} {response.error if response else ''}".strip()
            )
            return
        commits = response.json() or []
        if not isinstance(commits, list):
            report.errors.append("commits: unexpected payload")
            return
        report.fetched["commit"] = len(commits)
        self._update_rate(report, response)
        for entry in commits:
            sha = str(entry.get("sha") or "")
            if not sha:
                continue
            message = str((entry.get("commit") or {}).get("message") or "")
            subject_line = message.splitlines()[0].strip() if message else ""
            if not self._is_substantive(subject_line):
                report.skipped.append(f"commit:{sha[:8]} (no news value)")
                continue
            self._store(
                report, "commit", f"commit:{sha[:12]}",
                url=str(entry.get("html_url", "")),
                title=subject_line[:200],
                author=str(((entry.get("author") or {}).get("login"))
                           or ((entry.get("commit") or {}).get("author") or {}).get("name", "")),
                body=message,
                published_at=str(((entry.get("commit") or {}).get("author") or {}).get("date", "")),
                meta={"sha": sha, "branch": self.settings.project.repo},
                store=store,
            )

    def _sync_issues(self, report: SyncReport, since_days: int, store: bool) -> None:
        limit = max(0, int(self.cfg.issues_limit))
        if limit == 0:
            return
        since = (utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self._read(
            "/repos/{repo}/issues",
            {"state": "all", "sort": "updated", "direction": "desc", "since": since,
             "per_page": min(100, limit)},
        )
        if response is None or not response.ok:
            report.errors.append(
                f"issues: {response.status if response else 0} {response.error if response else ''}".strip()
            )
            return
        issues = response.json() or []
        if not isinstance(issues, list):
            report.errors.append("issues: unexpected payload")
            return
        report.fetched["issue"] = len(issues)
        self._update_rate(report, response)
        for issue in issues:
            number = issue.get("number")
            if number is None:
                continue
            is_pr = "pull_request" in issue
            kind = "pr" if is_pr else "issue"
            ref = f"{kind}:{number}"
            labels = [str((l or {}).get("name", "")) for l in (issue.get("labels") or [])]
            self._store(
                report, kind, ref,
                url=str(issue.get("html_url", "")),
                title=str(issue.get("title") or ""),
                author=str((issue.get("user") or {}).get("login", "")),
                body=str(issue.get("body") or ""),
                published_at=str(issue.get("created_at") or ""),
                meta={
                    "number": number,
                    "state": issue.get("state"),
                    "comments": issue.get("comments"),
                    "labels": labels,
                    "updated_at": issue.get("updated_at"),
                    "is_pull_request": is_pr,
                },
                store=store,
            )

    def _sync_discussions(self, report: SyncReport, store: bool) -> None:
        if not self.cfg.discussions:
            report.skipped.append("discussions disabled in configuration")
            return
        if not self.token:
            report.skipped.append(
                f"discussions need {self.cfg.token_env} with read:discussion"
            )
            return
        owner, _, name = self.repo.partition("/")
        query = """
        query($owner:String!,$name:String!,$first:Int!){
          repository(owner:$owner,name:$name){
            discussions(first:$first, orderBy:{field:UPDATED_AT, direction:DESC}){
              nodes{
                number title url body createdAt updatedAt
                author{login}
                category{name}
                comments{totalCount}
              }
            }
          }
        }
        """
        response = self._graphql(query, {"owner": owner, "name": name, "first": 20})
        if response is None:
            report.skipped.append("discussions: no token, nothing requested")
            return
        if not response.ok:
            report.errors.append(f"discussions: {response.status} {response.error}".strip())
            return
        payload = response.json() or {}
        nodes = (
            ((payload.get("data") or {}).get("repository") or {}).get("discussions") or {}
        ).get("nodes") or []
        report.fetched["discussion"] = len(nodes)
        if payload.get("errors"):
            report.errors.append(f"discussions: graphql errors {payload['errors'][:1]}")
        for node in nodes:
            number = node.get("number")
            if number is None:
                continue
            self._store(
                report, "discussion", f"discussion:{number}",
                url=str(node.get("url", "")),
                title=str(node.get("title") or ""),
                author=str((node.get("author") or {}).get("login", "")),
                body=str(node.get("body") or ""),
                published_at=str(node.get("createdAt") or ""),
                meta={
                    "number": number,
                    "category": (node.get("category") or {}).get("name"),
                    "comments": (node.get("comments") or {}).get("totalCount"),
                    "updated_at": node.get("updatedAt"),
                },
                store=store,
            )

    def _sync_repo_stats(self, report: SyncReport) -> None:
        """Repository numbers as *metrics*: real, attributed, and never a claim."""
        response = self._read("/repos/{repo}", expect_404_ok=True)
        if response is None or not response.ok:
            return
        data = response.json() or {}
        report.fetched["repo"] = 1
        self._update_rate(report, response)
        if self.db is None or not data:
            return
        captured = to_ts()
        for metric, field_name in (
            ("stars", "stargazers_count"),
            ("forks", "forks_count"),
            ("open_issues", "open_issues_count"),
            ("watchers", "subscribers_count"),
        ):
            value = data.get(field_name)
            if value is None:
                continue
            self.db.record_metric(
                "github", f"repo:{self.repo}", metric, float(value),
                captured_at=captured, source="api",
                meta={"endpoint": f"/repos/{self.repo}"},
            )

    # ---------------------------------------------------------------- helpers
    def _store(
        self,
        report: SyncReport,
        kind: str,
        external_ref: str,
        *,
        url: str = "",
        title: str = "",
        author: str = "",
        body: str = "",
        published_at: str | None = None,
        meta: dict[str, Any] | None = None,
        store: bool,
    ) -> None:
        if not store or self.db is None:
            report.new.append(external_ref)
            return
        _, changed = self.db.upsert_source(
            kind,
            external_ref,
            url=url, title=title, author=author, body=body,
            published_at=_iso_or_none(published_at),
            meta=meta or {},
        )
        if changed:
            report.changed.append(external_ref)
            report.new.append(external_ref)

    def _update_rate(self, report: SyncReport, response: Response) -> None:
        remaining = response.int_header("x-ratelimit-remaining", -1)
        if remaining >= 0:
            report.rate_limit = {
                "remaining": remaining,
                "limit": response.int_header("x-ratelimit-limit", -1),
                "reset_epoch": response.int_header("x-ratelimit-reset", -1),
                "resource": response.header("x-ratelimit-resource"),
            }

    @staticmethod
    def _is_substantive(subject_line: str) -> bool:
        text = (subject_line or "").strip()
        if len(text) < MIN_COMMIT_CHARS:
            return False
        lowered = text.lower()
        return not any(lowered.startswith(prefix) for prefix in NOISE_PREFIXES)


def recent_activity(db: SocialDB, kinds: Iterable[str], limit: int = 40) -> list[dict[str, Any]]:
    """Newest stored sources of the given kinds - the content engine's input."""
    wanted = [str(k) for k in kinds]
    if not wanted:
        return []
    marks = ", ".join("?" for _ in wanted)
    rows = db.all(
        f"""SELECT * FROM content_source WHERE kind IN ({marks})
            ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?""",
        [*wanted, limit],
    )
    for row in rows:
        row["meta"] = jloads(row["meta"], {})
    return rows


def describe_source(row: dict[str, Any]) -> str:
    """One-line human summary, used in the dashboard and in the CLI."""
    return f"{row.get('kind')}:{row.get('external_ref')} {row.get('title', '')}".strip()


def digest_of(body: str) -> str:
    import hashlib

    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


__all__ = ["GitHubConnector", "SyncReport", "recent_activity", "describe_source",
           "digest_of", "jdumps"]
