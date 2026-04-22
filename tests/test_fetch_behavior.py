import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from antenna import cli, db, fetcher, mcp_server
from antenna.fetcher import _to_iso


class FetchTimeTests(unittest.TestCase):
    def test_struct_time_is_treated_as_utc(self) -> None:
        self.assertEqual(_to_iso(time.gmtime(0)), "1970-01-01T00:00:00+00:00")


class FetchExitBehaviorTests(unittest.TestCase):
    def _run_fetch(self, *, strict: bool) -> int:
        args = SimpleNamespace(config=None, source_id=None, verbose=False, strict=strict)
        summary = {"polled": 2, "new_posts": 3, "errors": 1, "per_feed": []}
        cfg = SimpleNamespace(database=":memory:", first_run_entries=3, poll_delay_seconds=0.0)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("antenna.cli._load_config", return_value=cfg), \
             mock.patch("antenna.cli.db.connect", return_value=contextlib.nullcontext(object())), \
             mock.patch("antenna.cli.poll_all", return_value=summary), \
             contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(stderr):
            return cli.cmd_fetch(args)

    def test_fetch_is_tolerant_by_default(self) -> None:
        self.assertEqual(self._run_fetch(strict=False), 0)

    def test_fetch_can_fail_strictly(self) -> None:
        self.assertEqual(self._run_fetch(strict=True), 1)


class RefreshPostMetadataTests(unittest.TestCase):
    def test_refresh_post_metadata_updates_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "antenna.db"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                source_id = db.upsert_source(conn, "https://example.com/feed", title="Example", tags=[])
                original = db.NewPost(
                    source_id=source_id,
                    stable_id="post-1",
                    url="https://example.com/posts/1",
                    title="Example Post",
                    author="Author",
                    published_at="2026-04-21T07:00:34+00:00",
                    body_html="<p>old</p>",
                    body_text="old",
                )
                post_id = db.insert_post(conn, original)
                self.assertIsNotNone(post_id)

                refreshed = db.NewPost(
                    source_id=source_id,
                    stable_id="post-1",
                    url="https://example.com/posts/1",
                    title="Example Post",
                    author="Author",
                    published_at="2026-04-21T02:00:34+00:00",
                    body_html="<p>new</p>",
                    body_text="new",
                )
                db.refresh_post_metadata(conn, refreshed)

                row = conn.execute(
                    "SELECT published_at, body_text FROM posts WHERE id = ?",
                    (post_id,),
                ).fetchone()
                self.assertEqual(row["published_at"], "2026-04-21T02:00:34+00:00")
                self.assertEqual(row["body_text"], "new")


class FeedBackoffTests(unittest.TestCase):
    def test_poll_all_skips_source_during_backoff_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "antenna.db"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                source_id = db.upsert_source(conn, "https://example.com/feed", title="Example", tags=[])
                db.update_source_poll_state(
                    conn,
                    source_id,
                    last_error="HTTP error: 403",
                    consecutive_failures=2,
                    next_poll_after=db.future_iso(3600),
                    touch_last_polled=False,
                )
                with mock.patch("antenna.fetcher.fetch_feed") as mock_fetch:
                    summary = fetcher.poll_all(conn, first_run_entries=0, poll_delay_seconds=0)

                self.assertEqual(summary["polled"], 0)
                self.assertEqual(summary["skipped"], 1)
                mock_fetch.assert_not_called()

    def test_first_run_cap_does_not_backfill_older_history_on_later_polls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "antenna.db"
            db.init_db(db_path)
            posts = [
                db.NewPost(
                    source_id=1,
                    stable_id="post-a",
                    url="https://example.com/a",
                    title="A",
                    author="Author",
                    published_at="2026-04-21T12:00:00+00:00",
                    body_html="<p>A</p>",
                    body_text="A",
                ),
                db.NewPost(
                    source_id=1,
                    stable_id="post-b",
                    url="https://example.com/b",
                    title="B",
                    author="Author",
                    published_at="2026-04-20T12:00:00+00:00",
                    body_html="<p>B</p>",
                    body_text="B",
                ),
                db.NewPost(
                    source_id=1,
                    stable_id="post-c",
                    url="https://example.com/c",
                    title="C",
                    author="Author",
                    published_at="2026-04-19T12:00:00+00:00",
                    body_html="<p>C</p>",
                    body_text="C",
                ),
            ]
            first_result = fetcher.FetchResult(
                feed_title="Example Feed",
                etag=None,
                last_modified=None,
                posts=posts[:1],
                is_json_feed=False,
            )
            second_result = fetcher.FetchResult(
                feed_title="Example Feed",
                etag=None,
                last_modified=None,
                posts=posts,
                is_json_feed=False,
            )
            with db.connect(db_path) as conn:
                source_id = db.upsert_source(conn, "https://example.com/feed.xml", title="Example", tags=[])
                for post in posts:
                    post.source_id = source_id

                with mock.patch("antenna.fetcher.fetch_feed", side_effect=[first_result, second_result]):
                    first = fetcher.poll_all(conn, first_run_entries=1, poll_delay_seconds=0)
                    second = fetcher.poll_all(conn, first_run_entries=1, poll_delay_seconds=0)

                self.assertEqual(first["new_posts"], 1)
                self.assertEqual(second["new_posts"], 0)
                stored = conn.execute("SELECT COUNT(*) AS n FROM posts").fetchone()
                self.assertEqual(stored["n"], 1)


class DoctorCommandTests(unittest.TestCase):
    def test_doctor_json_surfaces_feed_and_delivery_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg_path = root / "antenna.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "database: antenna.db",
                        "outbox: outbox",
                        "",
                        "smtp:",
                        "  host: smtp.example.test",
                        "  port: 587",
                        "  username: smoke@example.test",
                        '  password: "secret"',
                        "  use_tls: true",
                        "",
                        "email:",
                        "  from_address: smoke@example.test",
                        "  from_name_template: \"{feed_title}\"",
                        "  to_address: inbox@example.test",
                        "",
                        "feeds:",
                        "  - url: https://example.com/feed.xml",
                        "    title: Example Feed",
                    ]
                )
            )

            db_path = root / "antenna.db"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                source_id = db.upsert_source(conn, "https://example.com/feed.xml", title="Example Feed", tags=[])
                post_id = db.insert_post(
                    conn,
                    db.NewPost(
                        source_id=source_id,
                        stable_id="post-1",
                        url="https://example.com/posts/1",
                        title="Example Post",
                        author="Author",
                        published_at="2026-04-21T07:00:34+00:00",
                        body_html="<p>hello</p>",
                        body_text="hello world",
                    ),
                )
                self.assertIsNotNone(post_id)
                db.update_source_poll_state(
                    conn,
                    source_id,
                    last_error="HTTP error: 403",
                    consecutive_failures=2,
                    next_poll_after=db.future_iso(3600),
                    touch_last_polled=False,
                )
                db.record_delivery(conn, post_id, channel="email", status="failed", detail="smtp timeout")

            args = SimpleNamespace(config=str(cfg_path), recent_hours="24", json=True, verbose=False)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.cmd_doctor(args)

            self.assertEqual(rc, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "needs-attention")
            self.assertEqual(report["sources"]["configured_feeds"], 1)
            self.assertEqual(report["sources"]["database_feeds"], 1)
            self.assertEqual(report["sources"]["erroring"], 1)
            self.assertEqual(report["sources"]["failing_sources"][0]["title"], "Example Feed")
            self.assertEqual(report["deliveries"]["failed"], 1)
            self.assertEqual(report["deliveries"]["pending_email"], 1)
            self.assertTrue(report["config"]["outbox_writable"])
            self.assertTrue(any("fetch --source-id" in action for action in report["actions"]))


class SendEmailScopeTests(unittest.TestCase):
    def test_send_email_can_scope_to_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "antenna.db"
            db.init_db(db_path)
            cfg = SimpleNamespace(
                database=db_path,
                outbox=root / "outbox",
                config_path=root / "antenna.yaml",
                default_mode="per_post",
                email=SimpleNamespace(
                    from_address="feeds@example.test",
                    from_name_template="{feed_title}",
                    to_address="user@example.test",
                ),
                smtp=SimpleNamespace(host="smtp.example.test", port=587, username="", password="", use_tls=True),
                rules=[],
            )

            with db.connect(db_path) as conn:
                first = db.upsert_source(conn, "https://example.com/one.xml", title="One", tags=[])
                second = db.upsert_source(conn, "https://example.com/two.xml", title="Two", tags=[])
                one_post = db.insert_post(
                    conn,
                    db.NewPost(
                        source_id=first,
                        stable_id="one-post",
                        url="https://example.com/one-post",
                        title="One post",
                        author="Author",
                        published_at="2026-04-21T07:00:34+00:00",
                        body_html="<p>one</p>",
                        body_text="one",
                    ),
                )
                two_post = db.insert_post(
                    conn,
                    db.NewPost(
                        source_id=second,
                        stable_id="two-post",
                        url="https://example.com/two-post",
                        title="Two post",
                        author="Author",
                        published_at="2026-04-21T07:00:34+00:00",
                        body_html="<p>two</p>",
                        body_text="two",
                    ),
                )
                self.assertIsNotNone(one_post)
                self.assertIsNotNone(two_post)
                with mock.patch("antenna.cli._send_or_dry", return_value=SimpleNamespace(ok=True, detail="ok")):
                    summary = cli._send_email_internal(
                        cfg,
                        conn,
                        mode="per_post",
                        since=None,
                        dry_run=False,
                        source_id=first,
                    )

                self.assertTrue(summary["ok"])
                self.assertEqual(summary["emails_sent"], 1)
                sent_rows = conn.execute(
                    "SELECT post_id, channel, status FROM deliveries ORDER BY id"
                ).fetchall()
                self.assertEqual(len(sent_rows), 1)
                self.assertEqual(sent_rows[0]["post_id"], one_post)
                pending = db.undelivered_posts(conn, channel="email", source_id=second, limit=10)
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["id"], two_post)


class SyncCommandTests(unittest.TestCase):
    def test_sync_json_reports_fetch_and_email(self) -> None:
        cfg = SimpleNamespace(
            database=":memory:",
            first_run_entries=1,
            poll_delay_seconds=0.0,
            default_mode="per_post",
            outbox=Path("/tmp/outbox"),
        )
        fetch_summary = {"polled": 1, "new_posts": 2, "errors": 0, "skipped": 0, "per_feed": []}
        email_summary = {
            "mode": "per_post",
            "source_id": 7,
            "dry_run": True,
            "posts_considered": 2,
            "posts_selected": 2,
            "emails_attempted": 2,
            "emails_sent": 2,
            "emails_failed": 0,
            "posts_marked_delivered": 2,
            "detail": None,
            "ok": True,
            "status": "ok",
        }
        args = SimpleNamespace(
            config=None,
            source_id="7",
            strict=False,
            mode=None,
            since=None,
            dry_run=True,
            json=True,
            verbose=False,
        )
        stdout = io.StringIO()
        with mock.patch("antenna.cli._load_config", return_value=cfg), \
             mock.patch("antenna.cli._run_sync", return_value=(
                 {
                     "generated_at": "2026-04-21T14:00:00+00:00",
                     "status": "ok",
                     "source_id": 7,
                     "mode": "per_post",
                     "dry_run": True,
                     "strict": False,
                     "since": None,
                     "fetch": fetch_summary,
                     "email": email_summary,
                 },
                 0,
             )), \
             contextlib.redirect_stdout(stdout):
            rc = cli.cmd_sync(args)

        self.assertEqual(rc, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["source_id"], 7)
        self.assertEqual(report["fetch"]["new_posts"], 2)
        self.assertEqual(report["email"]["emails_sent"], 2)


class SyncStatusTests(unittest.TestCase):
    def test_sync_marks_partial_feed_failure_as_degraded(self) -> None:
        cfg = SimpleNamespace(
            database=":memory:",
            first_run_entries=1,
            poll_delay_seconds=0.0,
            default_mode="per_post",
        )
        fetch_summary = {"polled": 2, "new_posts": 1, "errors": 1, "skipped": 0, "per_feed": []}
        email_summary = {
            "mode": "per_post",
            "source_id": None,
            "dry_run": True,
            "posts_considered": 1,
            "posts_selected": 1,
            "emails_attempted": 1,
            "emails_sent": 1,
            "emails_failed": 0,
            "posts_marked_delivered": 1,
            "detail": None,
            "ok": True,
            "status": "ok",
        }
        with mock.patch("antenna.cli.db.connect", return_value=contextlib.nullcontext(object())), \
             mock.patch("antenna.cli.poll_all", return_value=fetch_summary), \
             mock.patch("antenna.cli._send_email_internal", return_value=email_summary):
            report, rc = cli._run_sync(
                cfg,
                source_id=None,
                strict=False,
                mode=None,
                since_spec=None,
                dry_run=True,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("feed(s) errored", report["warnings"][0])

    def test_sync_marks_backed_off_feed_as_degraded(self) -> None:
        cfg = SimpleNamespace(
            database=":memory:",
            first_run_entries=1,
            poll_delay_seconds=0.0,
            default_mode="per_post",
        )
        fetch_summary = {
            "polled": 1,
            "new_posts": 0,
            "errors": 0,
            "skipped": 1,
            "per_feed": [
                {
                    "url": "https://example.com/feed.xml",
                    "title": "Example Feed",
                    "new": 0,
                    "error": None,
                    "format": None,
                    "skipped_until": "2026-04-21T16:00:00+00:00",
                }
            ],
        }
        email_summary = {
            "mode": "per_post",
            "source_id": None,
            "dry_run": True,
            "posts_considered": 0,
            "posts_selected": 0,
            "emails_attempted": 0,
            "emails_sent": 0,
            "emails_failed": 0,
            "posts_marked_delivered": 0,
            "detail": None,
            "ok": True,
            "status": "noop",
        }
        with mock.patch("antenna.cli.db.connect", return_value=contextlib.nullcontext(object())), \
             mock.patch("antenna.cli.poll_all", return_value=fetch_summary), \
             mock.patch("antenna.cli._send_email_internal", return_value=email_summary):
            report, rc = cli._run_sync(
                cfg,
                source_id=None,
                strict=False,
                mode=None,
                since_spec=None,
                dry_run=True,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("still in backoff", report["warnings"][0])


class MixedFeedToleranceTests(unittest.TestCase):
    def test_sync_still_delivers_healthy_feed_when_another_feed_fails(self) -> None:
        class FeedHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/healthy.xml":
                    body = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Healthy Feed</title>
    <item>
      <guid>healthy-1</guid>
      <title>Healthy item</title>
      <link>https://example.com/healthy-1</link>
      <description>Everything is fine.</description>
      <pubDate>Tue, 21 Apr 2026 14:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""
                    data = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == "/blocked.xml":
                    data = b"forbidden"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, fmt, *args):  # noqa: A003
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                db_path = root / "antenna.db"
                db.init_db(db_path)
                cfg = SimpleNamespace(
                    database=db_path,
                    outbox=root / "outbox",
                    config_path=root / "antenna.yaml",
                    first_run_entries=3,
                    poll_delay_seconds=0.0,
                    default_mode="per_post",
                    email=SimpleNamespace(
                        from_address="feeds@example.test",
                        from_name_template="{feed_title}",
                        to_address="user@example.test",
                    ),
                    smtp=SimpleNamespace(
                        host="smtp.example.test",
                        port=587,
                        username="",
                        password="",
                        use_tls=True,
                    ),
                    rules=[],
                )

                with db.connect(db_path) as conn:
                    healthy_id = db.upsert_source(conn, f"{base}/healthy.xml", title="Healthy Feed", tags=[])
                    blocked_id = db.upsert_source(conn, f"{base}/blocked.xml", title="Blocked Feed", tags=[])

                report, rc = cli._run_sync(
                    cfg,
                    source_id=None,
                    strict=False,
                    mode="per_post",
                    since_spec=None,
                    dry_run=True,
                )

                self.assertEqual(rc, 0)
                self.assertEqual(report["status"], "degraded")
                self.assertEqual(report["fetch"]["new_posts"], 1)
                self.assertEqual(report["fetch"]["errors"], 1)
                self.assertEqual(report["email"]["emails_sent"], 1)
                self.assertEqual(report["email"]["emails_failed"], 0)
                self.assertTrue(any("feed(s) errored" in warning for warning in report["warnings"]))

                with db.connect(db_path) as conn:
                    healthy = db.get_source(conn, healthy_id)
                    blocked = db.get_source(conn, blocked_id)
                    self.assertIsNone(healthy["last_error"])
                    self.assertIsNotNone(blocked["last_error"])
                    self.assertIn("403", blocked["last_error"])
                    self.assertGreater(
                        len(db.undelivered_posts(conn, channel="email", source_id=healthy_id, limit=10)),
                        0,
                    )
                    deliveries = conn.execute(
                        "SELECT channel, status FROM deliveries ORDER BY id"
                    ).fetchall()
                    self.assertEqual(len(deliveries), 1)
                    self.assertEqual(deliveries[0]["channel"], "dry_run")
                    self.assertEqual(deliveries[0]["status"], "sent")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class McpSourceHealthTests(unittest.TestCase):
    def test_list_sources_surfaces_backoff_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "antenna.db"
            db.init_db(db_path)
            cfg = SimpleNamespace(database=db_path)
            with db.connect(db_path) as conn:
                source_id = db.upsert_source(conn, "https://example.com/feed.xml", title="Example Feed", tags=["news"])
                db.update_source_poll_state(
                    conn,
                    source_id,
                    last_error="HTTP error: 403",
                    consecutive_failures=2,
                    next_poll_after=db.future_iso(3600),
                    touch_last_polled=False,
                )

            with mock.patch("antenna.mcp_server._config", return_value=cfg):
                rows = mcp_server.list_sources()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["poll_status"], "backoff")
            self.assertEqual(rows[0]["consecutive_failures"], 2)
            self.assertIsNotNone(rows[0]["next_poll_after"])


if __name__ == "__main__":
    unittest.main()
