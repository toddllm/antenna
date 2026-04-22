"""Antenna MCP server — stdio, reads the same SQLite DB as the CLI.

Exposes six tools for Phase 0:
  list_sources, subscribe, unsubscribe, recent_posts, search_posts, get_post
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from antenna import db
from antenna.config import Config, default_config_path
from antenna.fetcher import fetch_feed

log = logging.getLogger("antenna.mcp")

mcp = FastMCP("antenna")


def _config() -> Config:
    path = os.environ.get("ANTENNA_CONFIG") or str(default_config_path())
    return Config.load(path)


def _serialize_source(row) -> dict[str, Any]:
    next_poll_after = row["next_poll_after"] if "next_poll_after" in row.keys() else None
    last_error = row["last_error"] if "last_error" in row.keys() else None
    due_at = db.parse_iso(next_poll_after)
    if last_error and due_at and due_at > datetime.now(timezone.utc):
        poll_status = "backoff"
    elif last_error:
        poll_status = "error"
    else:
        poll_status = "healthy"
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "last_polled_at": row["last_polled_at"],
        "last_error": last_error,
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        "next_poll_after": next_poll_after,
        "poll_status": poll_status,
    }


def _serialize_post(row, include_body: bool = False) -> dict[str, Any]:
    d = {
        "id": row["id"],
        "source_id": row["source_id"],
        "source_title": row["source_title"] if "source_title" in row.keys() else None,
        "source_url": row["source_url"] if "source_url" in row.keys() else None,
        "url": row["url"],
        "title": row["title"],
        "author": row["author"],
        "published_at": row["published_at"],
        "excerpt": (row["body_text"] or "")[:400],
    }
    if include_body:
        d["body_text"] = row["body_text"]
        d["body_html"] = row["body_html"]
    if "snippet" in row.keys():
        d["snippet"] = row["snippet"]
    return d


# ---------- Tools ----------

@mcp.tool()
def list_sources() -> list[dict]:
    """List all feeds Antenna is tracking, with metadata and poll status."""
    cfg = _config()
    with db.connect(cfg.database) as conn:
        return [_serialize_source(r) for r in db.list_sources(conn)]


@mcp.tool()
def subscribe(url: str, title: str | None = None, tags: list[str] | None = None) -> dict:
    """Add a new feed to Antenna. Performs an initial fetch to validate the URL.

    Args:
        url: RSS, Atom, or JSON Feed URL.
        title: Optional override for the feed's title.
        tags: Optional list of topic tags.
    """
    cfg = _config()
    with db.connect(cfg.database) as conn:
        sid = db.upsert_source(conn, url, title=title, tags=tags or [])
        src = db.get_source(conn, sid)
        # Fire a first fetch so the agent gets confirmation it's working.
        result = fetch_feed(src, first_run_entries=cfg.first_run_entries)
        new_count = 0
        if result.error:
            failures = int(src["consecutive_failures"] or 0) + 1
            backoff_seconds = db.compute_error_backoff_seconds(src["poll_cadence"], failures)
            db.update_source_poll_state(
                conn,
                sid,
                last_error=result.error,
                consecutive_failures=failures,
                next_poll_after=db.future_iso(backoff_seconds),
            )
        else:
            for p in result.posts:
                pid = db.insert_post(conn, p)
                if pid is not None:
                    new_count += 1
                else:
                    db.refresh_post_metadata(conn, p)
            db.update_source_poll_state(
                conn,
                sid,
                etag=result.etag,
                last_modified=result.last_modified,
                title=result.feed_title,
                first_poll_done=True,
                consecutive_failures=0,
            )
        updated = db.get_source(conn, sid)
        return {
            "source_id": sid,
            "url": url,
            "title": (updated["title"] if updated else None) or result.feed_title or title,
            "initial_posts_fetched": new_count,
            "error": result.error,
            "format": None if result.error else ("json_feed" if result.is_json_feed else "rss_or_atom"),
            "poll_status": _serialize_source(updated)["poll_status"] if updated else None,
            "consecutive_failures": int(updated["consecutive_failures"] or 0) if updated else 0,
            "next_poll_after": updated["next_poll_after"] if updated else None,
        }


@mcp.tool()
def unsubscribe(source_id: int) -> dict:
    """Remove a feed and all its posts.

    Args:
        source_id: The id returned by list_sources / subscribe.
    """
    cfg = _config()
    with db.connect(cfg.database) as conn:
        row = db.get_source(conn, source_id)
        if not row:
            return {"ok": False, "error": f"No source with id={source_id}"}
        db.delete_source(conn, source_id)
        return {"ok": True, "removed": row["url"]}


@mcp.tool()
def recent_posts(
    source_id: int | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return the most recent posts across feeds (or one feed).

    Args:
        source_id: If set, limit to this feed.
        since: ISO-8601 UTC timestamp lower bound (e.g. 2026-04-20T00:00:00+00:00).
        limit: Max rows (default 20, cap 200).
    """
    cfg = _config()
    limit = min(max(int(limit), 1), 200)
    with db.connect(cfg.database) as conn:
        rows = db.recent_posts(conn, source_id=source_id, since=since, limit=limit)
        return [_serialize_post(r) for r in rows]


@mcp.tool()
def search_posts(
    query: str,
    source_ids: list[int] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Full-text search post titles + bodies. Uses SQLite FTS5.

    Supports FTS5 query syntax: phrase searches with quotes, AND/OR/NOT,
    prefix with * (e.g. rust*). Returns a highlighted snippet per hit.

    Args:
        query: FTS5 match expression.
        source_ids: Optional list of source IDs to restrict.
        since: ISO-8601 UTC timestamp lower bound.
        limit: Max rows (default 20, cap 100).
    """
    cfg = _config()
    limit = min(max(int(limit), 1), 100)
    with db.connect(cfg.database) as conn:
        rows = db.search_posts(
            conn, query, source_ids=source_ids or None, since=since, limit=limit
        )
        return [_serialize_post(r) for r in rows]


@mcp.tool()
def get_post(post_id: int) -> dict | None:
    """Fetch the full content of a single post (HTML and plain text)."""
    cfg = _config()
    with db.connect(cfg.database) as conn:
        row = db.get_post(conn, post_id)
        if not row:
            return None
        return _serialize_post(row, include_body=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        # MCP uses stdout for protocol — send logs to stderr.
        stream=__import__("sys").stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
