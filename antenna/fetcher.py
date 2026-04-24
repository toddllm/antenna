"""Feed fetcher: RSS, Atom, JSON Feed. Normalizes into NewPost rows."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urljoin

import feedparser

from antenna import db

log = logging.getLogger(__name__)

USER_AGENT = "Antenna/0.1.1 (+https://antennafeed.com)"
REQUEST_TIMEOUT = 20  # seconds


# --------- Helpers ---------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    text = _TAG_RE.sub(" ", s)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


_URL_ATTR_RE = re.compile(r'\b(src|href|poster)=(["\'])([^"\']+)\2', re.IGNORECASE)
_SRCSET_RE = re.compile(r'\bsrcset=(["\'])([^"\']+)\1', re.IGNORECASE)

# Shortcode placeholders some CMS feeds (IEEE Spectrum, Medium, WordPress embeds)
# emit INSTEAD of a real <iframe>. The placeholder is an empty <p> whose class name
# identifies the platform. The real embed can be recovered from the article page.
_EMPTY_YT_SHORTCODE_RE = re.compile(
    r'<(p|div)[^>]*class="[^"]*shortcode-media-youtube[^"]*"[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)
_ARTICLE_IFRAME_YT_RE = re.compile(
    r'<iframe[^>]*src=["\']https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{6,})(?:[?&"\'][^"\']*)?["\']',
    re.IGNORECASE,
)


def _youtube_thumb_link(video_id: str) -> str:
    watch = f"https://www.youtube.com/watch?v={video_id}"
    thumb = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    return (
        f'<p style="margin:1em 0"><a href="{watch}" target="_blank" rel="noopener">'
        f'<img src="{thumb}" alt="Watch on YouTube" '
        f'style="max-width:560px;width:100%;border:0;border-radius:6px" /></a>'
        f'<br/><small><a href="{watch}" target="_blank" rel="noopener">'
        f"▶ Watch on YouTube</a></small></p>"
    )


def recover_youtube_embeds(html_str: str | None, article_url: str | None) -> str | None:
    """Replace empty YouTube shortcode placeholders with email-safe thumbnail links,
    recovering the video ID from the original article page."""
    if not html_str or not article_url:
        return html_str
    if "shortcode-media-youtube" not in html_str:
        return html_str
    try:
        req = urllib.request.Request(article_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            page = resp.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("Could not fetch article %s for YT recovery: %s", article_url, e)
        return html_str
    ids = _ARTICLE_IFRAME_YT_RE.findall(page)
    if not ids:
        return html_str
    idx = 0

    def _sub(m: re.Match) -> str:
        nonlocal idx
        if idx >= len(ids):
            return m.group(0)
        vid = ids[idx]
        idx += 1
        return _youtube_thumb_link(vid)

    return _EMPTY_YT_SHORTCODE_RE.sub(_sub, html_str)


def absolutize_urls(html_str: str | None, base_url: str | None) -> str | None:
    if not html_str or not base_url:
        return html_str

    def _sub_attr(m: re.Match) -> str:
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        if val.startswith(("data:", "mailto:", "javascript:", "cid:", "#")):
            return m.group(0)
        return f'{attr}={quote}{urljoin(base_url, val)}{quote}'

    def _sub_srcset(m: re.Match) -> str:
        quote, raw = m.group(1), m.group(2)
        parts = []
        for candidate in raw.split(","):
            bits = candidate.strip().split(None, 1)
            if not bits:
                continue
            url = bits[0]
            descriptor = f" {bits[1]}" if len(bits) > 1 else ""
            if not url.startswith(("data:", "cid:")):
                url = urljoin(base_url, url)
            parts.append(url + descriptor)
        return f'srcset={quote}{", ".join(parts)}{quote}'

    out = _URL_ATTR_RE.sub(_sub_attr, html_str)
    out = _SRCSET_RE.sub(_sub_srcset, out)
    return out


def _to_iso(dt_like: Any) -> str | None:
    """Coerce various datetime representations to ISO 8601 UTC."""
    if dt_like is None:
        return None
    if isinstance(dt_like, datetime):
        if dt_like.tzinfo is None:
            dt_like = dt_like.replace(tzinfo=timezone.utc)
        return dt_like.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(dt_like, time.struct_time):
        try:
            # RSS parsers usually hand us UTC tuples. `time.mktime()` treats them
            # as local time, which shifts stored publish times by the machine's
            # timezone offset. `calendar.timegm()` preserves the intended UTC.
            ts = calendar.timegm(dt_like)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None
    if isinstance(dt_like, str):
        # Try RFC 2822 first, then ISO
        try:
            return parsedate_to_datetime(dt_like).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            pass
        try:
            return datetime.fromisoformat(dt_like.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None
    return None


def _hash_id(parts: Iterable[str]) -> str:
    h = hashlib.sha1()
    for p in parts:
        if p:
            h.update(p.encode("utf-8", "replace"))
            h.update(b"\x1f")
    return h.hexdigest()


def _poll_due(source: sqlite3.Row, now: datetime | None = None) -> bool:
    next_poll_after = source["next_poll_after"] if "next_poll_after" in source.keys() else None
    due_at = db.parse_iso(next_poll_after)
    if due_at is None:
        return True
    return due_at <= (now or datetime.now(timezone.utc))


def _is_agent_eyes_source(source: sqlite3.Row) -> bool:
    try:
        return "agent-eyes" in json.loads(source["tags_json"] or "[]")
    except Exception:
        return False


# --------- Fetching ---------

@dataclass
class FetchResult:
    feed_title: str | None
    etag: str | None
    last_modified: str | None
    posts: list[db.NewPost]
    is_json_feed: bool
    error: str | None = None


def _http_get(url: str, etag: str | None, last_modified: str | None) -> tuple[int, dict, bytes | None]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if etag:
        req.add_header("If-None-Match", etag)
    if last_modified:
        req.add_header("If-Modified-Since", last_modified)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, dict(e.headers or {}), None
        raise


def fetch_feed(
    source: sqlite3.Row,
    first_run_entries: int = 3,
) -> FetchResult:
    """Fetch one feed. Decide RSS/Atom vs JSON Feed based on content-type / URL / body sniff."""
    url = source["url"]
    first_poll_done = bool(source["first_poll_done"])
    try:
        status, headers, body = _http_get(url, source["etag"], source["last_modified"])
    except Exception as e:
        return FetchResult(None, None, None, [], False, error=f"HTTP error: {e}")

    if status == 304 or body is None:
        return FetchResult(None, None, None, [], False)

    content_type = (headers.get("Content-Type") or "").lower()
    etag = headers.get("ETag")
    last_modified = headers.get("Last-Modified")

    is_json_feed = (
        "json" in content_type
        or url.endswith(".json")
        or body.lstrip().startswith(b"{")
    )

    if is_json_feed:
        try:
            posts, feed_title = _parse_json_feed(body, source["id"])
        except Exception as e:
            return FetchResult(None, etag, last_modified, [], True, error=f"JSON Feed parse: {e}")
    else:
        try:
            posts, feed_title = _parse_rss_atom(body, source["id"])
        except Exception as e:
            return FetchResult(None, etag, last_modified, [], False, error=f"RSS/Atom parse: {e}")

    # First-run cap: trim to most recent N entries.
    if not first_poll_done and first_run_entries > 0:
        # entries are in chronological order from feed; newest first usually.
        posts = posts[:first_run_entries]

    return FetchResult(
        feed_title=feed_title,
        etag=etag,
        last_modified=last_modified,
        posts=posts,
        is_json_feed=is_json_feed,
    )


def _parse_rss_atom(body: bytes, source_id: int) -> tuple[list[db.NewPost], str | None]:
    parsed = feedparser.parse(body)
    feed_title = parsed.feed.get("title") if hasattr(parsed, "feed") else None
    out: list[db.NewPost] = []
    for e in parsed.entries:
        stable = (
            e.get("id")
            or e.get("guid")
            or e.get("link")
            or _hash_id([e.get("title", ""), str(e.get("published", ""))])
        )
        link = e.get("link")
        title = e.get("title")
        author = e.get("author") or (e.get("authors", [{}])[0].get("name") if e.get("authors") else None)
        published = _to_iso(e.get("published_parsed") or e.get("updated_parsed") or e.get("published") or e.get("updated"))
        body_html = None
        if e.get("content"):
            # content is a list of dicts per feedparser
            body_html = "\n".join(c.get("value", "") for c in e["content"] if c.get("value"))
        if not body_html:
            body_html = e.get("summary") or e.get("description")
        body_html = recover_youtube_embeds(body_html, link)
        body_html = absolutize_urls(body_html, link)
        body_text = strip_html(body_html)
        out.append(
            db.NewPost(
                source_id=source_id,
                stable_id=stable,
                url=link,
                title=title,
                author=author,
                published_at=published,
                body_html=body_html,
                body_text=body_text,
            )
        )
    return out, feed_title


def _parse_json_feed(body: bytes, source_id: int) -> tuple[list[db.NewPost], str | None]:
    doc = json.loads(body.decode("utf-8", "replace"))
    feed_title = doc.get("title")
    items = doc.get("items") or []
    out: list[db.NewPost] = []
    for it in items:
        stable = it.get("id") or it.get("url") or _hash_id([it.get("title", ""), it.get("date_published", "")])
        url = it.get("url") or it.get("external_url")
        title = it.get("title")
        author = None
        if "author" in it and isinstance(it["author"], dict):
            author = it["author"].get("name")
        elif "authors" in it and it["authors"]:
            author = it["authors"][0].get("name")
        published = _to_iso(it.get("date_published") or it.get("date_modified"))
        body_html = it.get("content_html")
        body_html = absolutize_urls(body_html, url)
        body_text = it.get("content_text") or strip_html(body_html)
        if not body_html and body_text:
            body_html = f"<p>{html.escape(body_text)}</p>"
        out.append(
            db.NewPost(
                source_id=source_id,
                stable_id=stable,
                url=url,
                title=title,
                author=author,
                published_at=published,
                body_html=body_html,
                body_text=body_text,
            )
        )
    return out, feed_title


def poll_all(
    conn: sqlite3.Connection,
    first_run_entries: int = 3,
    poll_delay_seconds: float = 0.5,
    only_source_id: int | None = None,
) -> dict:
    """Poll all configured sources; return a summary dict."""
    if only_source_id is not None:
        rows = [db.get_source(conn, only_source_id)]
        rows = [r for r in rows if r]
    else:
        rows = db.list_sources(conn)
    summary = {"polled": 0, "new_posts": 0, "errors": 0, "skipped": 0, "per_feed": []}
    now = datetime.now(timezone.utc)
    for source in rows:
        if _is_agent_eyes_source(source):
            summary["skipped"] += 1
            summary["per_feed"].append(
                {
                    "url": source["url"],
                    "title": source["title"],
                    "new": 0,
                    "error": None,
                    "format": "agent_eyes",
                    "skipped_until": None,
                    "skipped_reason": "agent_eyes_source",
                }
            )
            continue
        if only_source_id is None and not _poll_due(source, now=now):
            summary["skipped"] += 1
            summary["per_feed"].append(
                {
                    "url": source["url"],
                    "title": source["title"],
                    "new": 0,
                    "error": None,
                    "format": None,
                    "skipped_until": source["next_poll_after"],
                }
            )
            continue
        summary["polled"] += 1
        log.info("Polling %s", source["url"])
        result = fetch_feed(source, first_run_entries=first_run_entries)
        new_count = 0
        if result.error:
            summary["errors"] += 1
            failures = int(source["consecutive_failures"] or 0) + 1
            backoff_seconds = db.compute_error_backoff_seconds(source["poll_cadence"], failures)
            db.update_source_poll_state(
                conn,
                source["id"],
                last_error=result.error,
                consecutive_failures=failures,
                next_poll_after=db.future_iso(backoff_seconds),
            )
        else:
            stop_after_first_duplicate = bool(source["first_poll_done"])
            for p in result.posts:
                pid = db.insert_post(conn, p)
                if pid is not None:
                    new_count += 1
                else:
                    db.refresh_post_metadata(conn, p)
                    if stop_after_first_duplicate:
                        break
            db.update_source_poll_state(
                conn,
                source["id"],
                etag=result.etag,
                last_modified=result.last_modified,
                title=result.feed_title,
                first_poll_done=True,
                consecutive_failures=0,
            )
        summary["new_posts"] += new_count
        summary["per_feed"].append(
            {
                "url": source["url"],
                "title": result.feed_title or source["title"],
                "new": new_count,
                "error": result.error,
                "format": "json_feed" if result.is_json_feed else "rss_or_atom",
                "skipped_until": None,
            }
        )
        if poll_delay_seconds > 0:
            time.sleep(poll_delay_seconds)
    return summary
