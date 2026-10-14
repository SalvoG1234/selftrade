"""The command line: the same operations the dashboard performs, scriptable.

Nothing here bypasses a gate.  ``publish --live`` still needs the policy engine to
say *pass*, an approval where one is required, and a ready connector; with no
arguments it *simulates*, which is what an operator wants most of the time.  The
symmetry is intentional: this is what cron, GitHub Actions and the cloud worker
call, so it must be impossible to script around the safety layer - and it is,
because the safety layer lives inside the orchestrator and publisher, not here.

Every command prints plain text (no rich, no colour codes that break in a log)
and every one accepts ``--json`` where a machine reads it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, config as config_mod, obs, secrets
from . import planner as planner_mod
from .db import SocialDB
from .models import EditorialState, Platform
from .orchestrator import Orchestrator

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2


# ------------------------------------------------------------------- printing
def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str, sort_keys=True))


def _table(rows: list[dict[str, Any]], columns: Sequence[tuple[str, str]],
           empty: str = "(nothing to show)") -> None:
    if not rows:
        print(empty)
        return
    widths = {key: len(label) for key, label in columns}
    for row in rows:
        for key, _ in columns:
            widths[key] = max(widths[key], len(str(row.get(key, ""))[:60]))
    header = "  ".join(label.ljust(widths[key]) for key, label in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row.get(key, ""))[:60].ljust(widths[key]) for key, _ in columns))


def _bullets(lines: list[str]) -> None:
    for line in lines:
        print(f"- {line}")


# ----------------------------------------------------------------- handshake
def _orchestrator(args: argparse.Namespace) -> Orchestrator:
    settings = config_mod.load(getattr(args, "config", None))
    return Orchestrator(settings, db_path=getattr(args, "db", None))


def _load_db(args: argparse.Namespace) -> SocialDB:
    settings = config_mod.load(getattr(args, "config", None))
    return SocialDB.open(getattr(args, "db", None) or settings.database.url,
                         timeout=settings.database.timeout)


# ------------------------------------------------------------------- commands
def cmd_version(_: argparse.Namespace) -> int:
    print(f"socialmanager {__version__}")
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    """Create the config file and the database, then say what is still missing."""
    path = Path(args.config or config_mod.DEFAULT_CONFIG_PATH)
    settings = config_mod.load(path)
    if not path.exists():
        path.write_text(config_mod.dumps(settings), encoding="utf-8")
        print(f"wrote {path} (defaults: draft mode, connectors off)")
    else:
        print(f"{path} already exists: left untouched")
    db = SocialDB.open(args.db or settings.database.url, timeout=settings.database.timeout)
    print(f"database ready: {db.path} (schema v{db.health()['schema_version']})")
    db.close()
    orchestrator = Orchestrator(settings, db_path=args.db or settings.database.url)
    accounts = orchestrator.refresh_accounts()
    print(f"recorded {len(accounts)} accounts (status only, no credentials stored)")
    doctor = orchestrator.doctor()
    print("\nNext steps:")
    for check in doctor["checks"]:
        if check["check"].startswith(("connector.", "llm")) and not check["ok"]:
            print(f"  · {check['check']}: {check['detail'][:120]}")
    print("  · python -m socialmanager sync      # read the repository (read-only)")
    print("  · python -m socialmanager produce   # write bilingual drafts")
    print("  · python -m socialmanager serve     # open the dashboard")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        report = orchestrator.doctor()
    if args.json:
        _print_json(report)
        return EXIT_OK
    print(f"environment: {report['environment']['platform']} · python "
          f"{report['environment']['python']}")
    print(f"config: {report['environment']['config_path']} "
          f"({'found' if report['environment']['config_existed'] else 'defaults'})")
    print(f"database: {report['environment']['database']}\n")
    _table(report["checks"], (("check", "CHECK"), ("ok", "OK"), ("detail", "DETAIL")))
    print()
    print("VERDICT: " + ("ready" if report["ok"] else "needs attention (see the rows above)"))
    if report["problems"]:
        print("\nConfiguration problems:")
        _bullets(report["problems"][:10])
    return EXIT_OK if report["ok"] else EXIT_PROBLEM


def cmd_status(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        status = orchestrator.status()
    if args.json:
        _print_json(status)
        return EXIT_OK
    stats = status["stats"]
    print(f"project      : {status['project']} ({status['brand']})")
    print(f"mode         : {status['mode']} — {status['mode_label']}")
    print(f"kill switch  : {'ENGAGED' if status['kill_switch'] else 'released'}")
    print(f"languages    : {', '.join(status['languages'])}")
    print(f"items        : {stats['items']} ({stats['items_by_state']})")
    print(f"variants     : {stats['variants']}")
    print(f"scheduled    : {stats['scheduled_pending']} pending")
    print(f"published    : {stats['published']} real, {stats['dry_run_posts']} simulated")
    print(f"approvals    : {status['approvals']}")
    print(f"feedback     : {stats['feedback']} ({stats['feedback_new']} new)")
    print(f"sources      : {stats['sources']} · api calls: {stats['api_calls']} "
          f"({stats['api_errors']} failed)")
    print(f"audit        : {stats['audit_events']} events, chain "
          f"{'ok' if status['database']['audit_chain']['ok'] else 'BROKEN'}")
    print(f"llm          : {status['llm']}")
    print("\nconnectors")
    for name, state in status["connectors"].items():
        flag = "ready" if state.get("ready") else "draft/simulation"
        print(f"  {name:10s} {flag:18s} {('; '.join(state.get('missing') or [])[:90])}")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        result = orchestrator.sync(since_days=args.since_days)
    if args.json:
        _print_json(result.to_dict())
        return EXIT_OK
    sync = result.detail["sync"]
    print(f"repository  : {sync['repository']} ({'authenticated' if sync['authenticated'] else 'anonymous'})")
    print(f"fetched     : {sync['fetched']}")
    print(f"new/changed : {len(sync['new'])}")
    print(f"skipped     : {len(sync['skipped'])}")
    print(f"rate limit  : {sync['rate_limit']}")
    print(f"docs indexed: {result.detail['docs_indexed']}")
    print(f"community   : {result.detail['community']}")
    if sync["errors"]:
        print("errors:")
        _bullets(sync["errors"][:6])
    _bullets(result.messages)
    return EXIT_OK if result.ok else EXIT_PROBLEM


def cmd_produce(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        result = orchestrator.produce(limit=args.limit)
    if args.json:
        _print_json(result.to_dict())
        return EXIT_OK
    detail = result.detail
    print(f"briefs      : {detail['briefs']}")
    print(f"items       : {len(detail['items'])} -> {detail['items']}")
    print(f"variants    : {detail['variants']} ({detail['policy']})")
    print(f"generator   : {detail['provider']}")
    if detail["approval_requests"]:
        print(f"approvals   : queued for items {detail['approval_requests']}")
    _bullets(result.messages)
    return EXIT_OK


def cmd_drafts(args: argparse.Namespace) -> int:
    db = _load_db(args)
    rows = db.items(state=args.state, category=args.category, platform=args.platform,
                    limit=args.limit)
    out = []
    for row in rows:
        out.append({
            "id": row["id"], "state": row["state"], "category": row["category"],
            "risk": row["risk"], "title": row["title"], "planned_for": row["planned_for"],
            "variants": row["variant_count"],
        })
    if args.json:
        _print_json(out)
        return EXIT_OK
    _table(out, (("id", "ID"), ("state", "STATE"), ("platform_risk", "")))
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    db = _load_db(args)
    item = db.item(args.item_id)
    if item is None:
        print(f"no content_item {args.item_id}")
        return EXIT_PROBLEM
    if args.json:
        _print_json(item)
        return EXIT_OK
    print(f"#{item['id']} {item['title']}")
    print(f"  category {item['category']} · risk {item['risk']} · state {item['state']}")
    print(f"  planned for {item['planned_for'] or 'not scheduled'} · sources {item['source_refs']}")
    print(f"  approval: {item['approval']['state'] if item['approval'] else 'none'}")
    print("  FACTS (every claim must be one of these):")
    for fact in item["facts"]:
        print(f"    · {fact.get('claim')}  [{fact.get('source_url')}]")
    for variant in item["variants"]:
        print(f"\n  --- {variant['platform']}/{variant['lang']} "
              f"({variant['char_count']} chars, {variant['generator']}, "
              f"policy {variant['policy_state']}) ---")
        for line in str(variant["body"]).splitlines():
            print(f"    {line}")
        if variant["hashtags"]:
            print(f"    {variant['hashtags']}")
        if variant["media"]:
            print(f"    media: {variant['media']}")
        reasons = (variant.get("policy_report") or {}).get("reasons") or []
        if reasons:
            print("    policy notes:")
            for reason in reasons[:4]:
                print(f"      ! {reason}")
    if item.get("notes"):
        print(f"\n  notes: {item['notes']}")
    return EXIT_OK


def cmd_policy(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        engine = orchestrator.policy()
        if args.variant:
            report = engine.evaluate_and_store(args.variant, action="draft")
            payload = report.to_dict()
        else:
            payload = {"variants": []}
            for variant in orchestrator.db.variants(limit=args.limit):
                report = engine.evaluate_and_store(int(variant["id"]), action="draft")
                payload["variants"].append({
                    "variant_id": variant["id"], "platform": variant["platform"],
                    "lang": variant["lang"], "outcome": report.outcome,
                    "reasons": report.reasons,
                })
    if args.json:
        _print_json(payload)
        return EXIT_OK
    if args.variant:
        print(f"variant {args.variant}: {payload['outcome']}")
        _bullets(payload["reasons"] or ["no findings"])
        return EXIT_OK
    _table(payload["variants"], (("variant_id", "ID"), ("platform", "PLATFORM"),
                                 ("lang", "LANG"), ("outcome", "OUTCOME")))
    blocked = [v for v in payload["variants"] if v["outcome"] != "pass"]
    for row in blocked[:5]:
        print(f"  #{row['variant_id']}: {'; '.join(row['reasons'][:2])}")
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        result = orchestrator.plan(schedule=args.schedule, week_start=args.week,
                                   limit=args.limit)
    if args.json:
        _print_json(result.detail)
        return EXIT_OK
    plan = result.detail
    print(f"week of {plan['week_start']} ({plan['timezone']}")
    print(f"targets {plan['targets']} · planned {plan['filled']}")
    _table(plan["slots"], (("run_at", "WHEN"), ("platform", "PLATFORM"),
                           ("decision", "DECISION"), ("category", "CATEGORY"),
                           ("title", "TITLE")))
    if plan["skipped"]:
        print("\nnot scheduled:")
        for row in plan["skipped"][:8]:
            print(f"  · {row['platform'] or '-'} {row['title'][:50]}: {row['reason']}")
    _bullets(result.messages)
    return EXIT_OK


def cmd_calendar(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        if args.initial:
            payload = {"initial_proposal": orchestrator.planner().initial_calendar(days=args.days)}
        else:
            payload = orchestrator.planner().calendar(start=args.week, days=args.days)
    if args.json:
        _print_json(payload)
        return EXIT_OK
    if "initial_proposal" in payload:
        print(f"initial proposal ({args.days} days) — nothing here is scheduled:")
        rows = [{"date": e["date"], "time": e["time"], "platform": e["platform"],
                 "label": e["label"], "status": e["status"]}
                for e in payload["initial_proposal"]]
        _table(rows, (("date", "DATE"), ("time", "TIME"), ("platform", "PLATFORM"),
                      ("label", "CATEGORY"), ("status", "STATUS")))
        return EXIT_OK
    print(f"calendar from {payload['start']} in {payload['timezone']}"
          + ("" if payload["timezone_available"] else " (timezone db unavailable: UTC)"))
    print(f"targets {payload['targets']} · filled {payload['filled']}")
    for day in payload["days"]:
        if not day["slots"]:
            print(f"  {day['date']}: —")
            continue
        for slot in day["slots"]:
            print(f"  {day['date']} {slot['local_time']} {slot['platform']:9s} "
                  f"{slot['state']:9s} {slot['title'][:44]}")
    return EXIT_OK


def cmd_approvals(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        manager = orchestrator.approvals()
        rows = manager.history(limit=args.limit) if args.history else manager.pending(args.limit)
    if args.json:
        _print_json(rows)
        return EXIT_OK
    _table([{"id": r["id"], "subject": f"{r['subject_type']}:{r['subject_id']}",
             "risk": r["risk"], "state": r["state"], "title": r.get("item_title") or "",
             "reason": r["reason"]}
            for r in rows],
           (("id", "ID"), ("subject", "SUBJECT"), ("risk", "RISK"), ("state", "STATE"),
            ("title", "TITLE")),
           empty="(no approvals)")
    for row in rows[:6]:
        if row["state"] == "pending":
            print(f"  #{row['id']} {row['reason'][:100]}")
    return EXIT_OK


def cmd_decide(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        row = orchestrator.decide(args.item_id, args.decision, note=args.note,
                                  actor=args.actor)
    print(f"item {args.item_id}: {row.get('state', args.decision)} (by {args.actor})")
    return EXIT_OK


def cmd_publish(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        publisher = orchestrator.publisher()
        simulate = not args.live
        results = []
        if args.item:
            results = publisher.publish_item(args.item, platforms=args.platform,
                                             simulate=simulate, actor=args.actor)
        elif args.variant:
            results = [publisher.publish_variant(args.variant, simulate=simulate,
                                                 actor=args.actor)]
        else:
            results = publisher.run_due(limit=args.limit, worker="cli", simulate=simulate)
    payload = [r.to_dict() for r in results]
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"{'live' if args.live else 'simulation'} · {len(results)} target(s)")
    if not results:
        print("nothing due (schedule something first: `plan --schedule`)")
        return EXIT_OK
    _table([{**row, "reason": row["reason"][:70]} for row in payload],
           (("platform", "PLATFORM"), ("variant_id", "VARIANT"), ("state", "STATE"),
            ("reason", "REASON")))
    for row in results:
        if not row.ok:
            print(f"  {row.platform}: {row.reason[:160]}")
    return EXIT_OK


def cmd_tick(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        result = orchestrator.tick(simulate=not args.live, limit=args.limit,
                                   worker=args.worker)
    if args.json:
        _print_json(result.to_dict())
        return EXIT_OK
    print(f"{len(result.detail['results'])} job(s) handled")
    _table(result.detail["results"], (("platform", "PLATFORM"), ("variant_id", "VARIANT"),
                                      ("state", "STATE"), ("reason", "REASON")))
    _bullets(result.messages)
    return EXIT_OK


def cmd_feedback(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        result = orchestrator.feedback(ingest=not args.no_ingest)
    if args.json:
        _print_json(result.detail)
        return EXIT_OK
    report = result.detail["report"]
    print(f"feedback in the last {report['window_days']} days: {report['total']}")
    print(f"by category: {report['by_category']}")
    print(f"critical open: {report['open_critical']}")
    if report["clusters"]:
        print("\nrecurring groups:")
        for group in report["clusters"][:5]:
            print(f"  · {group['category']} x{group['count']}: {group['suggested_action']}")
    if not report["proposals"].get("ok"):
        print(f"\nissue proposals: {report['proposals']['reason']}")
    return EXIT_OK


def cmd_analytics(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        result = orchestrator.analytics(days=args.days, collect=not args.no_collect)
    if args.export:
        from .analytics import AnalyticsAgent

        agent = AnalyticsAgent(orchestrator.settings, orchestrator.db, x=orchestrator.x,
                               instagram=orchestrator.instagram)
        text = agent.export(days=args.days, fmt=args.export)
        out = Path(args.out or f"reports/social-analytics-{args.days}d.{args.export}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"exported {out}")
        return EXIT_OK
    if args.json:
        _print_json(result.detail)
        return EXIT_OK
    report = result.detail["report"]
    print(f"window {report['window_days']} days since {report['since']}")
    print(f"published {report['totals']['published']} · simulated {report['totals']['dry_run']} "
          f"· unknown {report['totals']['unknown_needs_inspection']} "
          f"· failed {report['totals']['failed']}")
    for platform, bucket in sorted(report["by_platform"].items()):
        print(f"  {platform:10s} posts {bucket['posts']:3d} metrics "
              f"{bucket['metrics_available']:3d} unavailable {bucket['metrics_unavailable']:3d}")
        for metric, value in sorted(bucket["metrics"].items())[:6]:
            print(f"      {metric} = {value}")
    if result.detail.get("collected"):
        print(f"\ncollection: {result.detail['collected']}")
    return EXIT_OK


def cmd_killswitch(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        if args.action == "engage":
            payload = orchestrator.set_kill_switch(True, actor=args.actor)
        elif args.action == "release":
            payload = orchestrator.set_kill_switch(False, actor=args.actor)
        else:
            from .policy import kill_switch_engaged

            payload = {"kill_switch": kill_switch_engaged(orchestrator.db, orchestrator.settings)}
    _print_json(payload)
    return EXIT_OK


def cmd_accounts(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        rows = orchestrator.refresh_accounts() if args.refresh else orchestrator.db.accounts()
    if args.json:
        _print_json(rows)
        return EXIT_OK
    _table([{**row, "scopes": ", ".join(row.get("scopes") or [])} for row in rows],
           (("platform", "PLATFORM"), ("handle", "HANDLE"), ("status", "STATUS"),
            ("token_env", "TOKEN ENV (name only)"), ("scopes", "SCOPES")),
           empty="(no accounts recorded: run `accounts --refresh`)")
    print("\ncredentials are referenced by variable name; no value is stored or printed.")
    return EXIT_OK


def cmd_connectors(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        payload = {name: orchestrator.connector_state(name)
                   for name in (str(Platform.GITHUB), str(Platform.X),
                                str(Platform.INSTAGRAM))}
    if args.json:
        _print_json(payload)
        return EXIT_OK
    for name, state in payload.items():
        print(f"\n=== {name} ===")
        for key in ("enabled", "ready", "dry_run", "mode", "authenticated", "api_base",
                    "handle", "ig_user_id", "token_env", "token_present",
                    "token_fingerprint", "auth_flow", "graph_version"):
            if key in state:
                print(f"  {key:18s} {state[key]}")
        missing = state.get("missing") or []
        if missing:
            print("  missing / notes:")
            _bullets([str(m) for m in missing[:6]])
    return EXIT_OK


def cmd_settings(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        if args.key and args.value is not None:
            try:
                value = json.loads(args.value)
            except ValueError:
                value = args.value
            payload = orchestrator.set_setting(args.key, value, actor=args.actor)
            _print_json(payload)
            return EXIT_OK
        payload = {"stored": orchestrator.db.settings(),
                   "effective": orchestrator.settings.to_dict()}
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print("stored settings:")
    if not payload["stored"]:
        print("  (none)")
    for key, value in payload["stored"].items():
        print(f"  {key} = {json.dumps(value, ensure_ascii=False)[:120]}")
    print("\neffective configuration (environment variables by name only):")
    print(config_mod.dumps(orchestrator.settings))
    return EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    db = _load_db(args)
    if args.verify:
        payload = db.verify_audit()
        _print_json(payload)
        return EXIT_OK if payload["ok"] else EXIT_PROBLEM
    rows = db.audit_tail(limit=args.limit, action=args.action)
    if args.json:
        _print_json(rows)
        return EXIT_OK
    _table([{**r, "detail": json.dumps(r["detail"], ensure_ascii=False)[:60]} for r in rows],
           (("id", "ID"), ("ts", "WHEN"), ("actor", "ACTOR"), ("action", "ACTION"),
            ("subject", "SUBJECT"), ("detail", "DETAIL")),
           empty="(no audit events)")
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    with _orchestrator(args) as orchestrator:
        path = orchestrator.backup(args.directory)
    print(f"backup written: {path}")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    rows = obs.tail(limit=args.limit, level=args.level, contains=args.contains)
    if args.json:
        _print_json(rows)
        return EXIT_OK
    for row in rows:
        print(json.dumps(row, ensure_ascii=False)[:200])
    if not rows:
        print("(the in-memory log is empty: this process has not logged yet)")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """Write the deliverables an operator actually reviews: drafts and the calendar."""
    out_dir = Path(args.out or "reports/social")
    out_dir.mkdir(parents=True, exist_ok=True)
    db = _load_db(args)
    settings = config_mod.load(getattr(args, "config", None))
    items = db.items(limit=args.limit)
    # The slot lives in ``scheduled_post``, not on the item: an item that has been
    # scheduled without one would otherwise be exported as "planned for: None".
    slots: dict[int, list[dict[str, Any]]] = {}
    for row in db.scheduled(limit=500):
        slots.setdefault(int(row["item_id"]), []).append(row)
    drafts = []
    for item in items:
        full = db.item(int(item["id"])) or {}
        planned = slots.get(int(item["id"]), [])
        drafts.append({"item": {k: v for k, v in full.items() if k != "variants"},
                       "slots": [{"platform": r["platform"], "run_at": r["run_at"],
                                  "state": r["state"]} for r in planned],
                       "variants": full.get("variants", [])})
    (out_dir / "drafts.json").write_text(
        json.dumps(drafts, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# Drafts (bilingual, generated)", "",
             "Every entry below is a **draft**. Nothing here has been published.", ""]
    for entry in drafts:
        item = entry["item"]
        when = ", ".join(f"{s['platform']} {s['run_at']} ({s['state']})"
                         for s in entry.get("slots") or [])
        lines += [f"## #{item['id']} {item['title']}", "",
                  f"- category: `{item['category']}`", f"- risk: `{item['risk']}`",
                  f"- state: `{item['state']}`",
                  f"- planned for: {when or 'not scheduled'}", "",
                  "**Facts (sources of every claim):**", ""]
        for fact in item.get("facts", []):
            lines.append(f"- {fact.get('claim')} — {fact.get('source_url')}")
        lines.append("")
        for variant in entry["variants"]:
            lines += [f"### {variant['platform']} / {variant['lang']}",
                      f"*generator: {variant['generator']} · policy: {variant['policy_state']} · "
                      f"{variant['char_count']} chars*", "", "```text",
                      variant["body"], "```", ""]
    (out_dir / "drafts.md").write_text("\n".join(lines), encoding="utf-8")
    planner = planner_mod.EditorialPlanner(settings, db)
    (out_dir / "calendar-initial.json").write_text(
        json.dumps(planner.initial_calendar(days=14), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / "calendar.json").write_text(
        json.dumps(planner.calendar(days=7), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"exported to {out_dir}: drafts.json, drafts.md, calendar.json, calendar-initial.json")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from .dashboard import serve

    with _orchestrator(args) as orchestrator:
        return serve(orchestrator, host=args.host, port=args.port,
                     no_auth=args.no_auth)


def cmd_token_check(args: argparse.Namespace) -> int:
    """Which credential variables are set - the names, and whether they are set."""
    settings = config_mod.load(getattr(args, "config", None))
    rows = [{
        "variable": name,
        "present": secrets.present(name),
        "fingerprint": secrets.fingerprint_of_env(name),
    } for name in settings.secret_names()]
    if args.json:
        _print_json(rows)
        return EXIT_OK
    _table(rows, (("variable", "VARIABLE"), ("present", "SET"), ("fingerprint", "FINGERPRINT")),
           empty="(configuration names no variables)")
    print("\nvalues are never read here: only presence and a one-way fingerprint.")
    return EXIT_OK


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="socialmanager",
        description="SelfTrade Social Manager - proposes, verifies, and only then publishes.",
    )
    parser.add_argument("--config", help="path to social.yaml (default: ./social.yaml)")
    parser.add_argument("--db", help="override the database path")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str, *, json_flag: bool = True):
        # ``--json`` is accepted both before and after the subcommand.  SUPPRESS
        # keeps the subparser default from overwriting the value set before it.
        child = sub.add_parser(name, help=help_text)
        if json_flag:
            child.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                               help=argparse.SUPPRESS)
        child.set_defaults(func=func)
        return child

    add("version", cmd_version, "print the version", json_flag=False)

    p = add("init", cmd_init, "create the config and the database")
    p.add_argument("--no-auth", action="store_true", help=argparse.SUPPRESS)

    add("doctor", cmd_doctor, "check the environment, the config and the connectors")

    add("status", cmd_status, "one screen of what exists right now")

    p = add("sync", cmd_sync, "read the repository (read-only) and index the docs")
    p.add_argument("--since-days", type=int, default=None)

    p = add("produce", cmd_produce, "generate bilingual drafts from real events")
    p.add_argument("--limit", type=int, default=6)

    p = add("drafts", cmd_drafts, "list drafts")
    p.add_argument("--state", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--platform", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = add("show", cmd_show, "print one item with its variants and facts")
    p.add_argument("item_id", type=int)

    p = add("policy", cmd_policy, "run the deterministic gate over drafts")
    p.add_argument("--variant", type=int, default=None)
    p.add_argument("--limit", type=int, default=25)

    p = add("plan", cmd_plan, "build the editorial calendar")
    p.add_argument("--schedule", action="store_true", help="write scheduled_post rows")
    p.add_argument("--week", default=None, help="ISO date of the week start")
    p.add_argument("--limit", type=int, default=None)

    p = add("calendar", cmd_calendar, "show the week (or the initial proposal)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--week", default=None)
    p.add_argument("--initial", action="store_true", help="proposal for the first two weeks")

    p = add("approvals", cmd_approvals, "the approval queue")
    p.add_argument("--history", action="store_true")
    p.add_argument("--limit", type=int, default=50)

    p = add("approve", cmd_decide, "approve an item")
    p.add_argument("item_id", type=int)
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="operator")
    p.set_defaults(decision="approved")

    p = add("reject", cmd_decide, "reject an item")
    p.add_argument("item_id", type=int)
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="operator")
    p.set_defaults(decision="rejected")

    p = add("publish", cmd_publish, "publish (or simulate) ready content")
    p.add_argument("--item", type=int, default=None)
    p.add_argument("--variant", type=int, default=None)
    p.add_argument("--platform", action="append", default=None)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--live", action="store_true", help="really publish (needs a ready connector)")
    p.add_argument("--actor", default="operator")

    p = add("tick", cmd_tick, "one worker tick: lease due jobs and handle them")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--live", action="store_true")
    p.add_argument("--worker", default="cli")

    p = add("feedback", cmd_feedback, "ingest and report feedback")
    p.add_argument("--no-ingest", action="store_true")

    p = add("analytics", cmd_analytics, "collect metrics and report")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--no-collect", action="store_true")
    p.add_argument("--export", choices=["json", "csv"], default=None)
    p.add_argument("--out", default=None)

    p = add("killswitch", cmd_killswitch, "engage, release or read the stop button", json_flag=False)
    p.add_argument("action", choices=["engage", "release", "status"])
    p.add_argument("--actor", default="operator")

    p = add("accounts", cmd_accounts, "recorded accounts and their credential names")
    p.add_argument("--refresh", action="store_true")

    add("connectors", cmd_connectors, "connector readiness, scopes and limits")

    p = add("settings", cmd_settings, "read or set stored settings")
    p.add_argument("key", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None)
    p.add_argument("--actor", default="operator")

    p = add("audit", cmd_audit, "the append-only audit log")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--action", default=None)

    p = add("backup", cmd_backup, "write a consistent copy of the database")
    p.add_argument("--directory", default=None)

    p = add("logs", cmd_logs, "the in-memory structured log of this process")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--level", default=None)
    p.add_argument("--contains", default=None)

    p = add("export", cmd_export, "write drafts and calendars to a directory")
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = add("serve", cmd_serve, "start the admin dashboard", json_flag=False)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-auth", action="store_true")

    add("token-check", cmd_token_check, "which credential variables are set (names only)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    obs.bind_secret_names(config_mod.load(getattr(args, "config", None)).secret_names())
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PROBLEM
