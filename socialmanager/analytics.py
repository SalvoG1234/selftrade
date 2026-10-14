"""Analytics: collect the metrics the platform actually returns, and say when it does not.

Two rules from the brief have teeth here:

* **never invent a metric.**  Every row in ``analytics_snapshot`` is written from
  an API response, and when a metric is unavailable the *unavailability* is
  recorded (``source='unavailable'``, ``available=0``) instead of a zero.  The
  dashboard then renders "not available" rather than a number that looks like
  data.
* **keep the distinction between historical, simulated, paper and live.**  A
  report separates engagement metrics (real, from the platform) from any
  performance number (which the bot's own reports own, not this system), and it
  never mixes them.

Metric windows matter too: X exposes non-public and organic metrics only for
posts younger than 30 days, and Instagram insights can lag up to 48 hours, so a
"missing" metric is often just a *too old* or *too new* post.  Both cases are
reported with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from . import obs
from .db import SocialDB, parse_ts, to_ts, utcnow

#: How long a post stays "measurable" per platform, from the platform's own docs.
METRIC_WINDOW_DAYS = {"x": 30, "instagram": 730, "github": 0}

#: Engagement metrics worth storing, per platform (public, documented fields).
TRACKED = {
    "x": ["public_metrics.like_count", "public_metrics.reply_count",
          "public_metrics.repost_count", "public_metrics.quote_count",
          "public_metrics.bookmark_count", "public_metrics.impression_count",
          "non_public_metrics.impression_count", "organic_metrics.impression_count"],
    "instagram": ["likes", "comments", "reach", "saved", "shares", "views",
                  "total_interactions"],
    "github": ["stars", "forks", "open_issues", "watchers"],
}


@dataclass
class CollectionReport:
    checked: int = 0
    stored: int = 0
    unavailable: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "stored": self.stored,
                "unavailable": self.unavailable, "skipped": list(self.skipped),
                "errors": list(self.errors)}


class AnalyticsAgent:
    def __init__(self, settings: Any, db: SocialDB, *, x: Any = None, instagram: Any = None,
                 run_id: str | None = None):
        self.settings = settings
        self.db = db
        self.x = x
        self.instagram = instagram
        self.run_id = run_id

    # ---------------------------------------------------------------- collecting
    def collect(self, *, days: int = 30, limit: int = 25) -> CollectionReport:
        """Ask each platform for fresh numbers on our own posts."""
        report = CollectionReport()
        cutoff = utcnow() - timedelta(days=max(1, int(days)))
        posts = self.db.published_posts(limit=200, state="published")
        for post in posts[:limit]:
            platform = str(post["platform"])
            external_id = str(post.get("external_id") or "")
            published_at = parse_ts(post.get("published_at"))
            report.checked += 1
            if not external_id:
                report.unavailable += 1
                report.skipped.append(f"post {post['id']}: no platform id stored")
                self._record_unavailable(platform, f"post:{post['id']}",
                                         "no external id: the platform never confirmed it")
                continue
            if published_at and platform == "x" and published_at < cutoff:
                report.unavailable += 1
                report.skipped.append(
                    f"post {post['id']}: older than the {METRIC_WINDOW_DAYS['x']}-day window"
                )
                self._record_unavailable(platform, external_id,
                                         f"outside the {METRIC_WINDOW_DAYS['x']}-day metrics window")
                continue
            if platform == "x" and self.x is not None:
                result = self.x.metrics(external_id)
                if not result.available:
                    report.unavailable += 1
                    self._record_unavailable(platform, external_id,
                                             result.reason or "not returned")
                    continue
                for metric, value in result.metrics.items():
                    if isinstance(value, (int, float)):
                        self.db.record_metric(platform, external_id, metric, float(value),
                                              source="api")
                        report.stored += 1
            elif platform == "instagram" and self.instagram is not None:
                media_type = str((post.get("meta") or {}).get("media_type") or "IMAGE")
                result = self.instagram.insights(external_id, media_type)
                if not result.get("available"):
                    report.unavailable += 1
                    self._record_unavailable(platform, external_id,
                                             result.get("reason") or "not returned")
                    continue
                for metric, value in (result.get("metrics") or {}).items():
                    if isinstance(value, (int, float)):
                        self.db.record_metric(platform, external_id, metric, float(value),
                                              source="api")
                        report.stored += 1
            else:
                report.unavailable += 1
                report.skipped.append(
                    f"post {post['id']}: the {platform} connector is not configured"
                )
                self._record_unavailable(platform, external_id,
                                         f"{platform} connector not configured")
        obs.info("analytics.collect", detail=report.to_dict())
        return report

    def _record_unavailable(self, platform: str, reference: str, reason: str) -> None:
        self.db.record_metric(
            platform, reference, "metrics_available", 0.0,
            captured_at=to_ts(utcnow().replace(microsecond=0)), source="unavailable",
            available=False, meta={"reason": reason[:200]},
        )

    # ------------------------------------------------------------------ reports
    def report(self, *, days: int = 30) -> dict[str, Any]:
        """Everything known about the last window - including what is missing."""
        since = to_ts(utcnow() - timedelta(days=max(1, int(days))))
        posts = [p for p in self.db.published_posts(limit=500)
                 if (p.get("published_at") or "") >= since]
        published = [p for p in posts if p["state"] == "published"]
        dry = [p for p in posts if p["state"] == "dry_run"]
        unknown = [p for p in posts if p["state"] == "publishing"]
        failed = [p for p in posts if p["state"] == "failed"]

        by_platform: dict[str, dict[str, Any]] = {}
        for post in posts:
            platform = str(post["platform"])
            bucket = by_platform.setdefault(platform, {
                "posts": 0, "dry_run": 0, "pending_unknown": 0, "failed": 0,
                "metrics_available": 0, "metrics_unavailable": 0, "metrics": {},
            })
            if post["state"] == "published":
                bucket["posts"] += 1
            elif post["state"] == "dry_run":
                bucket["dry_run"] += 1
            elif post["state"] == "publishing":
                bucket["pending_unknown"] += 1
            elif post["state"] == "failed":
                bucket["failed"] += 1

        for row in self.db.metrics(limit=5000):
            if (row.get("captured_at") or "") < since:
                continue
            platform = str(row["platform"])
            bucket = by_platform.setdefault(platform, {
                "posts": 0, "dry_run": 0, "pending_unknown": 0, "failed": 0,
                "metrics_available": 0, "metrics_unavailable": 0, "metrics": {},
            })
            if int(row.get("available") or 0) == 0:
                bucket["metrics_unavailable"] += 1
                continue
            if str(row["metric"]).startswith("metrics_available"):
                continue
            bucket["metrics_available"] += 1
            current = bucket["metrics"].get(str(row["metric"]))
            if current is None or abs(float(row["value"])) > abs(float(current)):
                bucket["metrics"][str(row["metric"])] = float(row["value"])

        feedback = self.db.all(
            """SELECT platform, category, COUNT(*) AS n FROM feedback_item
               WHERE ingested_at >= ? GROUP BY platform, category""", [since])
        return {
            "window_days": int(days),
            "since": since,
            "totals": {
                "published": len(published),
                "dry_run": len(dry),
                "unknown_needs_inspection": len(unknown),
                "failed": len(failed),
                "feedback": sum(int(r["n"]) for r in feedback),
            },
            "by_platform": by_platform,
            "feedback_by_platform": {
                str(r["platform"]): {"category": str(r["category"]), "count": int(r["n"])}
                for r in feedback
            },
            "sources": {
                "published_posts": "recorded by this project when a platform confirmed an id",
                "metrics": "read from the platform API inside its documented window",
                "unavailable": "recorded as unavailable, never filled with a zero",
                "trading_performance": "not measured here: that is the bot's own ledger "
                                       "(docs/CAPITAL.md), kept separate on purpose",
            },
            "generated_at": to_ts(),
        }

    def export(self, *, days: int = 30, fmt: str = "json") -> str:
        report = self.report(days=days)
        if fmt == "csv":
            lines = ["platform,posts,dry_run,unknown,failed,metrics_available,metrics_unavailable"]
            for platform, bucket in sorted(report["by_platform"].items()):
                lines.append(",".join(str(x) for x in (
                    platform, bucket["posts"], bucket["dry_run"], bucket["pending_unknown"],
                    bucket["failed"], bucket["metrics_available"],
                    bucket["metrics_unavailable"],
                )))
            return "\n".join(lines) + "\n"
        import json

        return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)

    def best_and_worst(self, *, platform: str = "x", metric: str = "public_metrics.like_count",
                       days: int = 30) -> dict[str, Any]:
        """A ranked view - only over posts that actually have the metric."""
        since = to_ts(utcnow() - timedelta(days=max(1, int(days))))
        rows = self.db.all(
            """SELECT s.subject_ref, s.value, p.url, p.published_at
               FROM analytics_snapshot s
               LEFT JOIN published_post p ON p.external_id = s.subject_ref
               WHERE s.platform = ? AND s.metric = ? AND s.available = 1 AND s.captured_at >= ?
               ORDER BY s.value DESC""",
            [str(platform), str(metric), since],
        )
        if not rows:
            return {"available": False, "reason": f"no stored values for {metric} on {platform}",
                    "ranking": []}
        return {"available": True, "metric": metric, "ranking": rows,
                "note": "values as reported by the platform API inside its metrics window"}
