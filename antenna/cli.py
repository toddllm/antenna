"""Antenna CLI. Fourteen commands: init, add-feed, import-opml, fetch, sync,
list-sources, recent-posts, search, doctor, render-digest, send-email,
setup-email, test-email, serve-mcp."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from antenna import db, opml
from antenna.config import Config, default_config_path
from antenna.email_setup import assess_smtp_config, build_provider_settings, rewrite_email_config
from antenna.fetcher import poll_all
from antenna.renderer import RenderedEmail, render_digest, render_post
from antenna.rules import decide
from antenna.sender import send_smtp, write_dry_run


def _load_config(path: str | None) -> Config:
    cfg_path = Path(path).expanduser().resolve() if path else default_config_path()
    return Config.load(cfg_path)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


# ---------- Commands ----------

def cmd_init(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    db.init_db(cfg.database)
    print(f"✓ Initialized database at {cfg.database}")
    # Seed from config's feeds list.
    with db.connect(cfg.database) as conn:
        added = 0
        for f in cfg.feeds:
            before = db.get_source_by_url(conn, f.url)
            db.upsert_source(conn, f.url, title=f.title, tags=f.tags)
            if before is None:
                added += 1
        print(f"✓ Loaded {len(cfg.feeds)} feeds from config ({added} new).")
    print(f"Outbox dir: {cfg.outbox}")
    return 0


def cmd_add_feed(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    with db.connect(cfg.database) as conn:
        sid = db.upsert_source(conn, args.url, title=args.title, tags=tags)
        print(f"✓ Added (or updated) feed id={sid}: {args.url}")
    return 0


def cmd_import_opml(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    entries = opml.parse_opml(args.path)
    print(f"Parsed {len(entries)} feeds from OPML.")
    with db.connect(cfg.database) as conn:
        added = 0
        for e in entries:
            tags = [e.category] if e.category else []
            before = db.get_source_by_url(conn, e.url)
            db.upsert_source(conn, e.url, title=e.title, tags=tags)
            if before is None:
                added += 1
        print(f"✓ Imported {added} new feeds ({len(entries) - added} already present).")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    with db.connect(cfg.database) as conn:
        only = None
        if args.source_id:
            only = int(args.source_id)
        summary = poll_all(
            conn,
            first_run_entries=cfg.first_run_entries,
            poll_delay_seconds=cfg.poll_delay_seconds,
            only_source_id=only,
        )
    _print_fetch_summary(summary, verbose=args.verbose)
    return 1 if args.strict and summary["errors"] > 0 else 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    source_id = int(args.source_id) if args.source_id else None
    report, rc = _run_sync(
        cfg,
        source_id=source_id,
        strict=args.strict,
        mode=args.mode,
        since_spec=args.since,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_fetch_summary(report["fetch"], verbose=args.verbose)
        _print_send_summary(report["email"], cfg.outbox)
    return rc


def cmd_list_sources(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    with db.connect(cfg.database) as conn:
        rows = db.list_sources(conn)
        if args.json:
            print(json.dumps(
                [{"id": r["id"], "url": r["url"], "title": r["title"],
                  "tags": json.loads(r["tags_json"] or "[]"),
                  "last_polled_at": r["last_polled_at"],
                  "last_error": r["last_error"],
                  "consecutive_failures": r["consecutive_failures"],
                  "next_poll_after": r["next_poll_after"]} for r in rows],
                indent=2,
            ))
            return 0
        if not rows:
            print("(no sources)")
            return 0
        for r in rows:
            tags = ", ".join(json.loads(r["tags_json"] or "[]"))
            err = f"  ERROR: {r['last_error']}" if r["last_error"] else ""
            backoff = ""
            if r["next_poll_after"]:
                backoff = (
                    f"  BACKOFF until {r['next_poll_after']}"
                    f" (failures: {r['consecutive_failures']})"
                )
            print(f"  [{r['id']:>3}] {r['title'] or '(untitled)':<30}  {r['url']}"
                  + (f"  tags: {tags}" if tags else "") + err + backoff)
    return 0


def cmd_recent_posts(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    since = _parse_since(args.since)
    with db.connect(cfg.database) as conn:
        rows = db.recent_posts(
            conn,
            source_id=int(args.source_id) if args.source_id else None,
            since=since,
            limit=int(args.limit),
        )
        _print_posts(rows, json_out=args.json)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    since = _parse_since(args.since)
    source_ids = None
    if args.source_id:
        source_ids = [int(s.strip()) for s in args.source_id.split(",") if s.strip()]
    with db.connect(cfg.database) as conn:
        rows = db.search_posts(
            conn,
            query=args.query,
            source_ids=source_ids,
            since=since,
            limit=int(args.limit),
        )
        _print_posts(rows, json_out=args.json, show_snippet=True)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    report = _doctor_report(cfg, recent_hours=int(args.recent_hours))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_doctor_report(report)
    return 0


def cmd_render_digest(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    since = _parse_since(args.since or "24h")
    with db.connect(cfg.database) as conn:
        rows = db.undelivered_posts(conn, channel="digest", since=since, limit=500)
        matched_map = _apply_rules_map(cfg.rules, rows)
        # Filter out posts excluded by rules.
        keep = [r for r in rows if r["id"] in matched_map and matched_map[r["id"]] is not None]
        rendered = render_digest(
            keep,
            from_address=cfg.email.from_address,
            from_name_template=cfg.email.from_name_template,
            to_address=cfg.email.to_address,
            config_hint=str(cfg.config_path) if cfg.config_path else None,
            matched_map={pid: rules for pid, rules in matched_map.items() if rules},
        )
        path = write_dry_run(cfg.outbox, rendered, tag="digest")
        print(f"✓ Digest preview written to {path}")
        print(f"  Subject: {rendered.subject}")
        print(f"  Posts:   {len(keep)}")
        if not args.no_record:
            for r in keep:
                db.record_delivery(conn, r["id"], channel="digest_preview", status="sent", detail=str(path))
    return 0


def cmd_send_email(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    mode = args.mode or cfg.default_mode
    since = _parse_since(args.since or ("24h" if mode == "digest" else None))
    dry_run = args.dry_run
    source_id = int(args.source_id) if args.source_id else None
    with db.connect(cfg.database) as conn:
        summary = _send_email_internal(
            cfg,
            conn,
            mode=mode,
            since=since,
            dry_run=dry_run,
            source_id=source_id,
        )
    _print_send_summary(summary, cfg.outbox)
    return 0 if summary["ok"] else 1


def cmd_setup_email(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).expanduser().resolve() if args.config else default_config_path()
    try:
        smtp, email = build_provider_settings(
            provider=args.provider,
            gmail_address=args.gmail_address,
            app_password=args.app_password,
            region=args.region,
            host=args.host,
            port=int(args.port),
            username=args.username,
            password=args.password,
            from_address=args.from_address,
            to_address=args.to_address,
            from_name_template=args.from_name_template,
            use_tls=not args.no_tls,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))

    print(f"Provider: {args.provider}")
    print(f"  smtp:  {smtp['host']}:{smtp['port']}  {'TLS' if smtp['use_tls'] else 'no TLS'}")
    print(f"  route: {email['from_address']} -> {email['to_address']}")
    print(f"  name:  {email['from_name_template']}")

    if args.dry_run:
        print(f"Dry-run only. No config changes written to {cfg_path}.")
        print("Next: `antenna test-email --dry-run` or rerun without `--dry-run` to save.")
        return 0

    try:
        written_path, backup = rewrite_email_config(cfg_path, smtp=smtp, email=email)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc))

    cfg = _load_config(str(written_path))
    assessment = assess_smtp_config(cfg)
    state = "configured" if assessment.configured else "incomplete"
    print(f"Updated: {written_path}")
    print(f"Backup:  {backup}")
    print(f"Status:  {state}  (auth: {assessment.auth_mode})")
    if assessment.missing_fields:
        print("Missing fields after write:")
        for field in assessment.missing_fields:
            print(f"  - {field}")
    else:
        print("Next:    `antenna test-email --dry-run` then `antenna test-email`")
    return 0


def cmd_test_email(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    assessment = assess_smtp_config(cfg)
    missing = assessment.missing_fields
    if args.dry_run:
        missing = [field for field in missing if field not in {"smtp.host", "smtp.username", "smtp.password"}]
    if missing:
        print("Email route is not ready yet.", file=sys.stderr)
        for field in missing:
            print(f"  - missing {field}", file=sys.stderr)
        return 1

    rendered = RenderedEmail(
        subject=args.subject or f"Antenna SMTP test — {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}",
        html=(
            "<html><body>"
            "<h1>Antenna email test</h1>"
            "<p>This is a fixed test message from Antenna.</p>"
            f"<p>SMTP host: <code>{cfg.smtp.host or '(dry-run only)'}</code></p>"
            f"<p>Config: <code>{cfg.config_path}</code></p>"
            "</body></html>"
        ),
        from_name="Antenna Test",
        from_address=cfg.email.from_address,
        to_address=args.to or cfg.email.to_address,
    )
    result = _send_or_dry(cfg, rendered, args.dry_run, tag="smtp-test")
    route = f"{rendered.from_address} -> {rendered.to_address}"
    status = "OK" if result.ok else "FAILED"
    print(f"{status}: {route}")
    print(f"  detail: {result.detail}")
    return 0 if result.ok else 1


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    import os
    if args.config:
        os.environ["ANTENNA_CONFIG"] = args.config
    from antenna.mcp_server import main as mcp_main
    mcp_main()
    return 0


# ---------- Internal helpers ----------

def _send_or_dry(cfg: Config, rendered, dry_run: bool, tag: str):
    if dry_run:
        path = write_dry_run(cfg.outbox, rendered, tag=tag)
        from antenna.sender import SendResult
        return SendResult(ok=True, detail=f"dry-run: {path}")
    return send_smtp(cfg.smtp, rendered)


def _print_fetch_summary(summary: dict[str, Any], verbose: bool) -> None:
    print(
        f"Polled {summary['polled']} feeds; "
        f"{summary['new_posts']} new posts; "
        f"{summary['errors']} errors; "
        f"{summary.get('skipped', 0)} skipped."
    )
    if verbose:
        for pf in summary["per_feed"]:
            if pf.get("skipped_until"):
                print(
                    f"  - {pf['new']:>3} new  {pf['title'] or pf['url']}"
                    f"   [backoff until {pf['skipped_until']}]"
                )
                continue
            marker = "✓" if not pf["error"] else "✗"
            fmt = f"   [{pf['format']}]" if pf.get("format") else ""
            print(
                f"  {marker} {pf['new']:>3} new  {pf['title'] or pf['url']}{fmt}"
                + (f"   [{pf['error']}]" if pf["error"] else "")
            )
    if summary["errors"] > 0 and not verbose:
        print(
            "Feed errors were recorded on the affected sources. "
            "Use `antenna list-sources` to inspect them, or rerun `fetch --strict` "
            "if you want the command to fail on any source error.",
            file=sys.stderr,
        )


def _apply_rules_map(rules, rows) -> dict[int, list[str] | None]:
    """Return {post_id: matched_rules_or_empty} for posts that pass;
    {post_id: None} for posts excluded."""
    out: dict[int, list[str] | None] = {}
    for r in rows:
        decision = decide(
            rules,
            source_url=r["source_url"],
            title=r["title"],
            body_text=r["body_text"],
        )
        if not decision.include:
            out[r["id"]] = None
            continue
        tags = list(decision.matched_rules)
        if decision.alert:
            tags = [f"alert:{t}" for t in tags] or ["alert:rule"]
        out[r["id"]] = tags
    return out


def _parse_since(spec: str | None) -> str | None:
    """Accept '24h', '7d', '30m', or ISO 8601. Return ISO UTC."""
    if not spec:
        return None
    s = spec.strip()
    if s[-1:] in ("h", "d", "m") and s[:-1].isdigit():
        n = int(s[:-1])
        unit = s[-1]
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        raise SystemExit(f"Unrecognized --since spec: {spec!r}. Use e.g. 24h, 7d, or ISO 8601.")


def _send_email_internal(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    mode: str,
    since: str | None,
    dry_run: bool,
    source_id: int | None = None,
) -> dict[str, Any]:
    rows = db.undelivered_posts(
        conn,
        channel="email",
        since=since,
        source_id=source_id,
        limit=500,
    )
    matched_map = _apply_rules_map(cfg.rules, rows)
    keep_rows = [r for r in rows if matched_map.get(r["id"]) is not None]
    summary = {
        "mode": mode,
        "source_id": source_id,
        "dry_run": dry_run,
        "posts_considered": len(rows),
        "posts_selected": len(keep_rows),
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_failed": 0,
        "posts_marked_delivered": 0,
        "detail": None,
        "ok": True,
        "status": "ok",
    }

    if mode == "per_post":
        summary["emails_attempted"] = len(keep_rows)
        for r in keep_rows:
            matched = matched_map.get(r["id"]) or []
            alert = any(m.startswith("alert:") for m in matched)
            rendered = render_post(
                r,
                from_address=cfg.email.from_address,
                from_name_template=cfg.email.from_name_template,
                to_address=cfg.email.to_address,
                alert=alert,
                matched_rules=[m.replace("alert:", "") for m in matched],
                config_hint=str(cfg.config_path) if cfg.config_path else None,
            )
            result = _send_or_dry(cfg, rendered, dry_run, tag="post")
            db.record_delivery(
                conn,
                r["id"],
                channel="dry_run" if dry_run else "email",
                status="sent" if result.ok else "failed",
                detail=result.detail,
            )
            if result.ok:
                summary["emails_sent"] += 1
                summary["posts_marked_delivered"] += 1
            else:
                summary["emails_failed"] += 1
                summary["detail"] = result.detail
        if summary["emails_failed"] > 0:
            summary["ok"] = False
            summary["status"] = "failed"
        elif summary["emails_sent"] == 0:
            summary["status"] = "noop"
        return summary

    if mode == "digest":
        if not keep_rows:
            summary["status"] = "noop"
            return summary
        summary["emails_attempted"] = 1
        rendered = render_digest(
            keep_rows,
            from_address=cfg.email.from_address,
            from_name_template=cfg.email.from_name_template,
            to_address=cfg.email.to_address,
            config_hint=str(cfg.config_path) if cfg.config_path else None,
            matched_map={pid: rules for pid, rules in matched_map.items() if rules},
        )
        result = _send_or_dry(cfg, rendered, dry_run, tag="digest")
        for r in keep_rows:
            db.record_delivery(
                conn,
                r["id"],
                channel="dry_run" if dry_run else "email",
                status="sent" if result.ok else "failed",
                detail=result.detail,
            )
        summary["detail"] = result.detail
        if result.ok:
            summary["emails_sent"] = 1
            summary["posts_marked_delivered"] = len(keep_rows)
        else:
            summary["emails_failed"] = 1
            summary["ok"] = False
            summary["status"] = "failed"
        return summary

    raise SystemExit(f"Unknown mode: {mode}")


def _print_send_summary(summary: dict[str, Any], outbox: Path) -> None:
    if summary["mode"] == "per_post":
        print(
            f"Per-post: {summary['emails_sent']} sent, {summary['emails_failed']} failed"
            + (f"  (dry-run outbox: {outbox})" if summary["dry_run"] else "")
        )
        return

    if summary["mode"] == "digest":
        if summary["emails_attempted"] == 0:
            print("No new posts for digest.")
            return
        print(
            f"Digest: {'sent' if summary['emails_sent'] else 'failed'} "
            f"({summary['posts_selected']} posts)"
            + (f"  (dry-run outbox: {outbox})" if summary["dry_run"] else "")
        )
        return

    print(f"Unknown mode: {summary['mode']}", file=sys.stderr)


def _run_sync(
    cfg: Config,
    *,
    source_id: int | None,
    strict: bool,
    mode: str | None,
    since_spec: str | None,
    dry_run: bool,
) -> tuple[dict[str, Any], int]:
    chosen_mode = mode or cfg.default_mode
    since = _parse_since(since_spec or ("24h" if chosen_mode == "digest" else None))
    with db.connect(cfg.database) as conn:
        fetch_summary = poll_all(
            conn,
            first_run_entries=cfg.first_run_entries,
            poll_delay_seconds=cfg.poll_delay_seconds,
            only_source_id=source_id,
        )
        email_summary = _send_email_internal(
            cfg,
            conn,
            mode=chosen_mode,
            since=since,
            dry_run=dry_run,
            source_id=source_id,
        )
    warnings: list[str] = []
    backed_off = sum(1 for feed in fetch_summary["per_feed"] if feed.get("skipped_until"))
    if fetch_summary["errors"] > 0:
        warnings.append(
            f"{fetch_summary['errors']} feed(s) errored during fetch; healthy feeds still completed."
        )
    if backed_off > 0:
        warnings.append(
            f"{backed_off} feed(s) are still in backoff and were skipped this run."
        )

    needs_attention = not email_summary["ok"] or (strict and fetch_summary["errors"] > 0)
    if needs_attention:
        status = "needs-attention"
    elif fetch_summary["errors"] > 0 or backed_off > 0:
        status = "degraded"
    else:
        status = "ok"
    report = {
        "generated_at": db.now_iso(),
        "status": status,
        "source_id": source_id,
        "mode": chosen_mode,
        "dry_run": dry_run,
        "strict": strict,
        "since": since,
        "fetch": fetch_summary,
        "email": email_summary,
        "warnings": warnings,
    }
    return report, 0 if not needs_attention else 1


def _doctor_report(cfg: Config, recent_hours: int) -> dict[str, Any]:
    recent_hours = max(1, int(recent_hours))
    recent_since = (datetime.now(timezone.utc) - timedelta(hours=recent_hours)).isoformat(timespec="seconds")
    db_exists = cfg.database.exists()
    outbox_exists = cfg.outbox.exists()
    outbox_is_dir = cfg.outbox.is_dir() if outbox_exists else False
    outbox_writable = _path_writable(cfg.outbox)
    smtp_assessment = assess_smtp_config(cfg)
    smtp_ready = smtp_assessment.configured

    sources = {
        "configured_feeds": len(cfg.feeds),
        "database_feeds": 0,
        "healthy": 0,
        "erroring": 0,
        "backed_off": 0,
        "posts_total": 0,
        "posts_recent_window": 0,
        "failing_sources": [],
    }
    deliveries = {
        "pending_email": 0,
        "total": 0,
        "sent": 0,
        "failed": 0,
        "suppressed": 0,
        "recent_window_hours": recent_hours,
        "recent_sent": 0,
        "recent_failed": 0,
        "recent_suppressed": 0,
    }
    actions: list[str] = []

    if db_exists:
        with db.connect(cfg.database) as conn:
            source_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS database_feeds,
                    COALESCE(SUM(CASE WHEN last_error IS NULL THEN 1 ELSE 0 END), 0) AS healthy,
                    COALESCE(SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END), 0) AS erroring,
                    COALESCE(SUM(CASE WHEN next_poll_after IS NOT NULL AND next_poll_after >= ? THEN 1 ELSE 0 END), 0) AS backed_off
                FROM sources
                """,
                (db.now_iso(),),
            ).fetchone()
            sources.update({k: int(source_row[k] or 0) for k in source_row.keys()})

            post_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS posts_total,
                    COALESCE(SUM(CASE WHEN COALESCE(published_at, fetched_at) >= ? THEN 1 ELSE 0 END), 0) AS posts_recent_window
                FROM posts
                """,
                (recent_since,),
            ).fetchone()
            sources["posts_total"] = int(post_row["posts_total"] or 0)
            sources["posts_recent_window"] = int(post_row["posts_recent_window"] or 0)

            delivery_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END), 0) AS sent,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
                    COALESCE(SUM(CASE WHEN status = 'suppressed' THEN 1 ELSE 0 END), 0) AS suppressed,
                    COALESCE(SUM(CASE WHEN sent_at >= ? AND status = 'sent' THEN 1 ELSE 0 END), 0) AS recent_sent,
                    COALESCE(SUM(CASE WHEN sent_at >= ? AND status = 'failed' THEN 1 ELSE 0 END), 0) AS recent_failed,
                    COALESCE(SUM(CASE WHEN sent_at >= ? AND status = 'suppressed' THEN 1 ELSE 0 END), 0) AS recent_suppressed
                FROM deliveries
                """,
                (recent_since, recent_since, recent_since),
            ).fetchone()
            deliveries.update({k: int(delivery_row[k] or 0) for k in delivery_row.keys()})

            pending_row = conn.execute(
                """
                SELECT COUNT(*) AS pending_email
                FROM posts p
                WHERE NOT EXISTS (
                    SELECT 1 FROM deliveries d
                    WHERE d.post_id = p.id AND d.channel = 'email' AND d.status = 'sent'
                )
                """
            ).fetchone()
            deliveries["pending_email"] = int(pending_row["pending_email"] or 0)

            failing_rows = conn.execute(
                """
                SELECT id, title, url, last_error, last_polled_at, consecutive_failures, next_poll_after
                FROM sources
                WHERE last_error IS NOT NULL
                ORDER BY COALESCE(next_poll_after, last_polled_at, created_at) DESC
                LIMIT 5
                """
            ).fetchall()
            sources["failing_sources"] = [
                {
                    "id": int(row["id"]),
                    "title": row["title"] or "",
                    "url": row["url"],
                    "last_error": row["last_error"],
                    "last_polled_at": row["last_polled_at"],
                    "consecutive_failures": int(row["consecutive_failures"] or 0),
                    "next_poll_after": row["next_poll_after"],
                }
                for row in failing_rows
            ]
    else:
        actions.append(
            f"Run `antenna init -c {cfg.config_path}` to create the database and seed feeds from config."
        )

    if not outbox_exists:
        actions.append(f"Create the outbox directory at `{cfg.outbox}` before relying on dry runs or email previews.")
    elif not outbox_is_dir:
        actions.append(f"`{cfg.outbox}` exists but is not a directory. Fix that path in the config.")
    if not outbox_writable:
        actions.append(f"Make `{cfg.outbox}` writable for the account running Antenna.")
    if not smtp_ready:
        missing = ", ".join(smtp_assessment.missing_fields) or "SMTP settings"
        actions.append(
            "Finish email setup before enabling real sends. "
            f"Missing or inconsistent: {missing}. "
            "Fastest path: `antenna setup-email ...`."
        )
    if db_exists and sources["configured_feeds"] != sources["database_feeds"]:
        actions.append(
            "Config feeds and database feeds differ. Run `antenna init` to sync config-managed feeds, then use "
            "`add-feed` only for deliberate local-only additions."
        )
    if db_exists and sources["erroring"] > 0:
        failing = sources["failing_sources"][0]
        actions.append(
            f"Inspect failing feed [{failing['id']}] with `antenna -v fetch --source-id {failing['id']} --strict` "
            "or remove it if the publisher blocks polling permanently."
        )
    if db_exists and deliveries["pending_email"] > 0 and smtp_ready:
        actions.append(
            "Run `antenna sync` to poll and flush pending posts in one step, or `antenna send-email` "
            "if you only want delivery."
        )
    if db_exists and deliveries["recent_failed"] > 0:
        actions.append("Check recent failed deliveries before onboarding testers to avoid silent email loss.")

    status = "healthy" if not actions else "needs-attention"
    return {
        "generated_at": db.now_iso(),
        "status": status,
        "config": {
            "path": str(cfg.config_path) if cfg.config_path else None,
            "database": str(cfg.database),
            "database_exists": db_exists,
            "outbox": str(cfg.outbox),
            "outbox_exists": outbox_exists,
            "outbox_is_dir": outbox_is_dir,
            "outbox_writable": outbox_writable,
        },
        "email": {
            "smtp_host": cfg.smtp.host,
            "smtp_port": int(cfg.smtp.port),
            "use_tls": bool(cfg.smtp.use_tls),
            "from_address": cfg.email.from_address,
            "to_address": cfg.email.to_address,
            "smtp_configured": smtp_ready,
            "smtp_auth_mode": smtp_assessment.auth_mode,
            "smtp_missing_fields": smtp_assessment.missing_fields,
        },
        "sources": sources,
        "deliveries": deliveries,
        "actions": actions,
    }


def _path_writable(path: Path) -> bool:
    target = path if path.exists() else path.parent
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return target.is_dir() and target.stat().st_mode is not None and os.access(target, os.W_OK)


def _print_doctor_report(report: dict[str, Any]) -> None:
    print("Antenna doctor")
    print(f"  Status:    {report['status']}")
    print(f"  Generated: {report['generated_at']}")

    config = report["config"]
    print("\nConfig")
    print(f"  config: {config['path']}")
    print(f"  db:     {config['database']}  ({'exists' if config['database_exists'] else 'missing'})")
    outbox_state = "exists" if config["outbox_exists"] else "missing"
    if config["outbox_exists"] and not config["outbox_is_dir"]:
        outbox_state = "not-a-directory"
    outbox_write = "writable" if config["outbox_writable"] else "not writable"
    print(f"  outbox: {config['outbox']}  ({outbox_state}, {outbox_write})")

    email = report["email"]
    smtp_state = "configured" if email["smtp_configured"] else "incomplete"
    print("\nEmail")
    print(
        f"  smtp: {smtp_state}  "
        f"{email['smtp_host']}:{email['smtp_port']}  "
        f"{'TLS' if email['use_tls'] else 'no TLS'}  "
        f"auth: {email['smtp_auth_mode']}"
    )
    print(f"  route: {email['from_address']} -> {email['to_address']}")
    if email["smtp_missing_fields"]:
        print(f"  missing: {', '.join(email['smtp_missing_fields'])}")

    sources = report["sources"]
    print("\nFeeds")
    print(
        f"  configured: {sources['configured_feeds']}  "
        f"in_db: {sources['database_feeds']}  "
        f"healthy: {sources['healthy']}  "
        f"erroring: {sources['erroring']}  "
        f"backed_off: {sources['backed_off']}"
    )
    print(
        f"  posts: {sources['posts_total']} total  "
        f"{sources['posts_recent_window']} in last {report['deliveries']['recent_window_hours']}h"
    )

    deliveries = report["deliveries"]
    print("\nDeliveries")
    print(
        f"  total: {deliveries['total']}  sent: {deliveries['sent']}  "
        f"failed: {deliveries['failed']}  suppressed: {deliveries['suppressed']}"
    )
    print(
        f"  recent {deliveries['recent_window_hours']}h: sent {deliveries['recent_sent']}  "
        f"failed {deliveries['recent_failed']}  suppressed {deliveries['recent_suppressed']}"
    )
    print(f"  pending email: {deliveries['pending_email']}")

    failing_sources = sources["failing_sources"]
    if failing_sources:
        print("\nFailing feeds")
        for src in failing_sources:
            title = src["title"] or "(untitled)"
            print(f"  [{src['id']}] {title}")
            print(f"      {src['url']}")
            print(f"      last_error: {src['last_error']}")
            if src["next_poll_after"]:
                print(
                    f"      next_retry: {src['next_poll_after']}  "
                    f"(failures: {src['consecutive_failures']})"
                )

    print("\nRecommended next steps")
    if report["actions"]:
        for action in report["actions"]:
            print(f"  - {action}")
    else:
        print("  - No immediate issues found.")


def _print_posts(rows: list[sqlite3.Row], json_out: bool, show_snippet: bool = False) -> None:
    if json_out:
        print(json.dumps(
            [{"id": r["id"], "source_id": r["source_id"],
              "source_title": r["source_title"], "url": r["url"],
              "title": r["title"], "author": r["author"],
              "published_at": r["published_at"],
              "excerpt": (r["body_text"] or "")[:300],
              **({"snippet": r["snippet"]} if show_snippet and "snippet" in r.keys() else {})}
             for r in rows],
            indent=2,
        ))
        return
    if not rows:
        print("(no posts)")
        return
    for r in rows:
        when = (r["published_at"] or "")[:16]
        print(f"  [{r['id']:>5}] {when}  {r['source_title'] or '-':<20.20}  {r['title'] or '(untitled)'}")
        if r["url"]:
            print(f"          {r['url']}")
        if show_snippet and "snippet" in r.keys() and r["snippet"]:
            print(f"          … {r['snippet']}")


# ---------- Argparse ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="antenna",
        description="Local-first feed reader for email and MCP.",
    )
    p.add_argument("-c", "--config", help="Path to antenna.yaml (default: ./antenna.yaml or $ANTENNA_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="Create DB and load feeds from config.")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add-feed", help="Add a single feed.")
    s.add_argument("url")
    s.add_argument("--title")
    s.add_argument("--tags", help="Comma-separated tags.")
    s.set_defaults(func=cmd_add_feed)

    s = sub.add_parser("import-opml", help="Import feeds from an OPML file.")
    s.add_argument("path")
    s.set_defaults(func=cmd_import_opml)

    s = sub.add_parser(
        "fetch",
        help="Poll feeds and store new posts. Source-specific errors are recorded but non-fatal by default.",
    )
    s.add_argument("--source-id", help="Fetch only this source.")
    s.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any feed reports an error during this poll.",
    )
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("sync", help="Poll feeds and immediately deliver pending email in one headless step.")
    s.add_argument("--source-id", help="Fetch and deliver only this source.")
    s.add_argument("--strict", action="store_true",
                   help="Exit non-zero if any feed reports an error during fetch.")
    s.add_argument("--mode", choices=["per_post", "digest"], help="Default from config.")
    s.add_argument("--since", help="Override the window for digest mode.")
    s.add_argument("--dry-run", action="store_true", help="Write to outbox/ instead of sending.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("list-sources", help="List all sources.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list_sources)

    s = sub.add_parser("recent-posts", help="Show recent posts across all feeds.")
    s.add_argument("--source-id")
    s.add_argument("--since", help="e.g. 24h, 7d, or ISO 8601")
    s.add_argument("--limit", default="20")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_recent_posts)

    s = sub.add_parser("search", help="Full-text search posts (FTS5 syntax).")
    s.add_argument("query")
    s.add_argument("--source-id", help="Comma-separated source IDs")
    s.add_argument("--since")
    s.add_argument("--limit", default="20")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("doctor", help="Summarize install health, feed errors, and delivery backlog.")
    s.add_argument("--recent-hours", default="24", help="Window used for recent post and delivery counts.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("render-digest", help="Render a preview digest HTML to the outbox.")
    s.add_argument("--since", help="Default: 24h")
    s.add_argument("--no-record", action="store_true",
                   help="Don't record a digest_preview delivery row.")
    s.set_defaults(func=cmd_render_digest)

    s = sub.add_parser("send-email", help="Send undelivered posts via SMTP (or --dry-run).")
    s.add_argument("--mode", choices=["per_post", "digest"], help="Default from config.")
    s.add_argument("--source-id", help="Send only posts from this source.")
    s.add_argument("--since", help="Override the window for digest mode.")
    s.add_argument("--dry-run", action="store_true", help="Write to outbox/ instead of sending.")
    s.set_defaults(func=cmd_send_email)

    s = sub.add_parser("setup-email", help="Write a provider preset into antenna.yaml with a safe backup.")
    s.add_argument("--provider", choices=["gmail", "ses", "generic"], required=True)
    s.add_argument("--gmail-address", help="Used for Gmail username, from_address, and default to_address.")
    s.add_argument("--app-password", help="Gmail App Password used with --provider gmail.")
    s.add_argument("--region", default="us-east-1", help="SES region for the SMTP hostname.")
    s.add_argument("--host", help="SMTP host for --provider generic.")
    s.add_argument("--port", default="587", help="SMTP port. Default: 587")
    s.add_argument("--username", help="SMTP username for SES or generic SMTP.")
    s.add_argument("--password", help="SMTP password for SES or generic SMTP.")
    s.add_argument("--from-address", help="From address for sent mail.")
    s.add_argument("--to-address", help="Destination inbox. Defaults to from-address or Gmail address.")
    s.add_argument("--from-name-template", default="{feed_title}", help="Default sender display-name template.")
    s.add_argument("--no-tls", action="store_true", help="Disable TLS for generic SMTP or special cases.")
    s.add_argument("--dry-run", action="store_true", help="Preview the preset without rewriting the config.")
    s.set_defaults(func=cmd_setup_email)

    s = sub.add_parser("test-email", help="Send or preview a fixed test message before your first real sync.")
    s.add_argument("--to", help="Override email.to_address just for this test.")
    s.add_argument("--subject", help="Custom subject line for the test message.")
    s.add_argument("--dry-run", action="store_true", help="Write a preview to outbox/ instead of sending.")
    s.set_defaults(func=cmd_test_email)

    s = sub.add_parser("serve-mcp", help="Run the Antenna MCP stdio server.")
    s.set_defaults(func=cmd_serve_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
