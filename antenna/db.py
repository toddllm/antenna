"""SQLite schema + helpers. Uses FTS5 for post search."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL UNIQUE,
    title             TEXT,
    tags_json         TEXT NOT NULL DEFAULT '[]',
    etag              TEXT,
    last_modified     TEXT,
    poll_cadence      INTEGER NOT NULL DEFAULT 900,  -- seconds
    created_at        TEXT NOT NULL,
    last_polled_at    TEXT,
    last_error        TEXT,
    first_poll_done   INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_poll_after   TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    stable_id     TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    author        TEXT,
    published_at  TEXT,
    body_html     TEXT,
    body_text     TEXT,
    fetched_at    TEXT NOT NULL,
    UNIQUE (source_id, stable_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_source_published
    ON posts(source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_published
    ON posts(published_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    title, body_text, author,
    content='posts', content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, title, body_text, author)
    VALUES (new.id, new.title, new.body_text, new.author);
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, body_text, author)
    VALUES ('delete', old.id, old.title, old.body_text, old.author);
END;
CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, body_text, author)
    VALUES ('delete', old.id, old.title, old.body_text, old.author);
    INSERT INTO posts_fts(rowid, title, body_text, author)
    VALUES (new.id, new.title, new.body_text, new.author);
END;

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    channel_type    TEXT NOT NULL,            -- 'email' | 'mcp'
    channel_config  TEXT NOT NULL DEFAULT '{}',
    rules_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subs_source ON subscriptions(source_id);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    post_id         INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,            -- 'email' | 'alert' | 'digest' | 'dry_run'
    status          TEXT NOT NULL,            -- 'sent' | 'failed' | 'suppressed'
    sent_at         TEXT NOT NULL,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS idx_deliveries_post ON deliveries(post_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_sent ON deliveries(sent_at DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "sources", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "sources", "next_poll_after", "TEXT")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: Path) -> None:
    with connect(path):
        pass


# ---------- Source helpers ----------

def upsert_source(
    conn: sqlite3.Connection,
    url: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> int:
    tags_json = json.dumps(tags or [])
    row = conn.execute("SELECT id FROM sources WHERE url = ?", (url,)).fetchone()
    if row:
        sid = row["id"]
        # Update title/tags if provided and different.
        if title:
            conn.execute("UPDATE sources SET title = ? WHERE id = ?", (title, sid))
        if tags is not None:
            conn.execute("UPDATE sources SET tags_json = ? WHERE id = ?", (tags_json, sid))
        return sid
    conn.execute(
        "INSERT INTO sources (url, title, tags_json, created_at) VALUES (?, ?, ?, ?)",
        (url, title, tags_json, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def get_source_by_url(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sources ORDER BY COALESCE(title, url)"
    ).fetchall()


def update_source_poll_state(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    last_error: str | None = None,
    title: str | None = None,
    first_poll_done: bool | None = None,
    consecutive_failures: int | None = None,
    next_poll_after: str | None = None,
    touch_last_polled: bool = True,
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if touch_last_polled:
        fields.append("last_polled_at = ?")
        params.append(now_iso())
    if etag is not None:
        fields.append("etag = ?")
        params.append(etag)
    if last_modified is not None:
        fields.append("last_modified = ?")
        params.append(last_modified)
    if last_error is not None:
        fields.append("last_error = ?")
        params.append(last_error)
    else:
        fields.append("last_error = NULL")
    if title is not None:
        fields.append("title = COALESCE(title, ?)")
        params.append(title)
    if first_poll_done is not None:
        fields.append("first_poll_done = ?")
        params.append(1 if first_poll_done else 0)
    if consecutive_failures is not None:
        fields.append("consecutive_failures = ?")
        params.append(consecutive_failures)
    if next_poll_after is not None:
        fields.append("next_poll_after = ?")
        params.append(next_poll_after)
    elif consecutive_failures == 0:
        fields.append("next_poll_after = NULL")
    params.append(source_id)
    conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", params)


def compute_error_backoff_seconds(
    poll_cadence: int,
    consecutive_failures: int,
    max_backoff_seconds: int = 86400,
) -> int:
    cadence = max(60, int(poll_cadence or 900))
    exponent = max(1, consecutive_failures)
    return min(cadence * (2 ** exponent), max_backoff_seconds)


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# ---------- Post helpers ----------

@dataclass
class NewPost:
    source_id: int
    stable_id: str
    url: str | None
    title: str | None
    author: str | None
    published_at: str | None  # ISO8601 UTC
    body_html: str | None
    body_text: str | None


def insert_post(conn: sqlite3.Connection, p: NewPost) -> int | None:
    """Insert post; return new post id, or None if duplicate."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO posts
          (source_id, stable_id, url, title, author, published_at,
           body_html, body_text, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            p.source_id,
            p.stable_id,
            p.url,
            p.title,
            p.author,
            p.published_at,
            p.body_html,
            p.body_text,
            now_iso(),
        ),
    )
    if cur.rowcount == 0:
        return None
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def refresh_post_metadata(conn: sqlite3.Connection, p: NewPost) -> None:
    """Refresh stored metadata for an existing post without changing delivery state."""
    conn.execute(
        """
        UPDATE posts
        SET url = COALESCE(?, url),
            title = COALESCE(?, title),
            author = COALESCE(?, author),
            published_at = COALESCE(?, published_at),
            body_html = COALESCE(?, body_html),
            body_text = COALESCE(?, body_text)
        WHERE source_id = ? AND stable_id = ?
        """,
        (
            p.url,
            p.title,
            p.author,
            p.published_at,
            p.body_html,
            p.body_text,
            p.source_id,
            p.stable_id,
        ),
    )


def recent_posts(
    conn: sqlite3.Connection,
    source_id: int | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    clauses = []
    params: list[Any] = []
    if source_id is not None:
        clauses.append("p.source_id = ?")
        params.append(source_id)
    if since is not None:
        clauses.append("COALESCE(p.published_at, p.fetched_at) >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"""
        SELECT p.*, s.url AS source_url, s.title AS source_title
        FROM posts p JOIN sources s ON s.id = p.source_id
        {where}
        ORDER BY COALESCE(p.published_at, p.fetched_at) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def search_posts(
    conn: sqlite3.Connection,
    query: str,
    source_ids: list[int] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    params: list[Any] = [query]
    clauses: list[str] = []
    if source_ids:
        placeholders = ",".join(["?"] * len(source_ids))
        clauses.append(f"p.source_id IN ({placeholders})")
        params.extend(source_ids)
    if since:
        clauses.append("COALESCE(p.published_at, p.fetched_at) >= ?")
        params.append(since)
    where = f"AND {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"""
        SELECT p.*, s.url AS source_url, s.title AS source_title,
               snippet(posts_fts, 1, '<mark>', '</mark>', ' … ', 12) AS snippet
        FROM posts_fts f
        JOIN posts p ON p.id = f.rowid
        JOIN sources s ON s.id = p.source_id
        WHERE posts_fts MATCH ?
        {where}
        ORDER BY COALESCE(p.published_at, p.fetched_at) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def get_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.*, s.url AS source_url, s.title AS source_title
        FROM posts p JOIN sources s ON s.id = p.source_id
        WHERE p.id = ?
        """,
        (post_id,),
    ).fetchone()


# ---------- Subscription / delivery helpers ----------

def ensure_subscription(
    conn: sqlite3.Connection,
    source_id: int,
    channel_type: str,
    channel_config: dict | None = None,
    rules: dict | None = None,
) -> int:
    row = conn.execute(
        "SELECT id FROM subscriptions WHERE source_id = ? AND channel_type = ?",
        (source_id, channel_type),
    ).fetchone()
    if row:
        return int(row["id"])
    conn.execute(
        """INSERT INTO subscriptions
           (source_id, channel_type, channel_config, rules_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            source_id,
            channel_type,
            json.dumps(channel_config or {}),
            json.dumps(rules or {}),
            now_iso(),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def record_delivery(
    conn: sqlite3.Connection,
    post_id: int,
    channel: str,
    status: str,
    subscription_id: int | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO deliveries
           (subscription_id, post_id, channel, status, sent_at, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (subscription_id, post_id, channel, status, now_iso(), detail),
    )


def delivered_post_ids(
    conn: sqlite3.Connection, channel: str
) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT post_id FROM deliveries WHERE channel = ? AND status = 'sent'",
        (channel,),
    ).fetchall()
    return {int(r["post_id"]) for r in rows}


def undelivered_posts(
    conn: sqlite3.Connection,
    channel: str,
    since: str | None = None,
    source_id: int | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Posts not yet delivered to the given channel, newest first."""
    params: list[Any] = [channel]
    clauses: list[str] = []
    if source_id is not None:
        clauses.append("p.source_id = ?")
        params.append(source_id)
    if since:
        clauses.append("COALESCE(p.published_at, p.fetched_at) >= ?")
        params.append(since)
    extra = f"AND {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"""
        SELECT p.*, s.url AS source_url, s.title AS source_title
        FROM posts p JOIN sources s ON s.id = p.source_id
        WHERE NOT EXISTS (
            SELECT 1 FROM deliveries d
            WHERE d.post_id = p.id AND d.channel = ? AND d.status = 'sent'
        )
        {extra}
        ORDER BY COALESCE(p.published_at, p.fetched_at) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
