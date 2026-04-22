"""Render post and digest emails via Jinja2."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from antenna.fetcher import strip_html

TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


@dataclass
class RenderedEmail:
    subject: str
    html: str
    from_name: str
    from_address: str
    to_address: str


def _humanize_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.astimezone().strftime("%b %d, %Y %H:%M")


def _excerpt(text: str | None, limit: int = 260) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def render_post(
    post: sqlite3.Row,
    *,
    from_address: str,
    from_name_template: str,
    to_address: str,
    alert: bool = False,
    matched_rules: list[str] | None = None,
    config_hint: str | None = None,
) -> RenderedEmail:
    source_title = post["source_title"] or post["source_url"] or "Antenna"
    template = _env.get_template("post.html")
    html = template.render(
        post={
            "title": post["title"] or "(untitled)",
            "url": post["url"],
            "author": post["author"],
            "published_display": _humanize_date(post["published_at"]),
            "body_html": post["body_html"] or post["body_text"] or "",
        },
        source_title=source_title,
        source_url=post["source_url"],
        alert=alert,
        matched_rules=matched_rules or [],
        config_hint=config_hint,
    )
    subject_prefix = "[Antenna alert] " if alert else ""
    subject = f"{subject_prefix}{source_title}: {post['title'] or '(untitled)'}"
    from_name = from_name_template.replace("{feed_title}", source_title)
    return RenderedEmail(
        subject=subject,
        html=html,
        from_name=from_name,
        from_address=from_address,
        to_address=to_address,
    )


def render_digest(
    posts: list[sqlite3.Row],
    *,
    from_address: str,
    from_name_template: str,
    to_address: str,
    config_hint: str | None = None,
    matched_map: dict[int, list[str]] | None = None,
) -> RenderedEmail:
    """Group posts by source, render digest."""
    matched_map = matched_map or {}
    by_source: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for p in posts:
        key = p["source_title"] or p["source_url"] or "Sources"
        if key not in by_source:
            by_source[key] = []
            order.append(key)
        by_source[key].append(
            {
                "title": p["title"] or "(untitled)",
                "url": p["url"],
                "author": p["author"],
                "published_display": _humanize_date(p["published_at"]),
                "excerpt": _excerpt(p["body_text"]),
                "matched": matched_map.get(int(p["id"]), []),
            }
        )

    groups = [{"source_title": k, "posts": by_source[k]} for k in order]
    today = datetime.now().astimezone().strftime("%A, %b %d, %Y")
    digest_title = "Antenna Digest"
    template = _env.get_template("digest.html")
    html = template.render(
        digest_title=digest_title,
        digest_date=today,
        total=len(posts),
        groups=groups,
        config_hint=config_hint,
    )
    subject = f"Antenna Digest — {today} ({len(posts)} post{'s' if len(posts) != 1 else ''})"
    from_name = from_name_template.replace("{feed_title}", digest_title)
    return RenderedEmail(
        subject=subject,
        html=html,
        from_name=from_name,
        from_address=from_address,
        to_address=to_address,
    )
