import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from antenna import cli, db, fetcher
from antenna.agent_eyes_bridge import (
    AgentEyesExtraction,
    extraction_to_post,
    extract_with_agent_eyes,
)
from antenna.config import AgentEyesConfig, AgentEyesSourceConfig


class AgentEyesBridgeTests(unittest.TestCase):
    def test_extract_with_agent_eyes_builds_local_cli_command(self) -> None:
        source = AgentEyesSourceConfig(
            url="https://www.asco.org/annual-meeting",
            title="ASCO Annual Meeting",
            instruction="conference dates and important links",
            schema=["title", "dates", "important_links"],
            cookies_file="~/cookies/asco.txt",
            stealth=True,
            settle_ms=5000,
        )
        settings = AgentEyesConfig(
            binary="/tmp/agent-eyes",
            model="gpt-4o-mini",
            timeout_seconds=30,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"title": "ASCO Annual Meeting", "dates": "May 29 - June 2, 2026"}),
            stderr="[agent-eyes] ok",
        )

        with mock.patch("antenna.agent_eyes_bridge.subprocess.run", return_value=completed) as run:
            result = extract_with_agent_eyes(source, settings, env={"OPENAI_API_KEY": "test"})

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["/tmp/agent-eyes", "extract", source.url, source.instruction])
        self.assertIn("--schema", command)
        self.assertIn("title,dates,important_links", command)
        self.assertIn("--cookies", command)
        self.assertIn(str(Path("~/cookies/asco.txt").expanduser()), command)
        self.assertIn("--stealth", command)
        self.assertEqual(result.payload["title"], "ASCO Annual Meeting")

    def test_extraction_to_post_uses_payload_hash_as_stable_id(self) -> None:
        source = AgentEyesSourceConfig(
            url="https://www.asco.org/annual-meeting",
            title="ASCO Annual Meeting",
            mode="state",
            stable_fields=["title", "dates"],
        )
        extraction = AgentEyesExtraction(
            source=source,
            payload={
                "title": "ASCO Annual Meeting",
                "dates": "May 29 - June 2, 2026",
                "important_links": ["https://example.com/first"],
            },
            stderr="",
        )
        changed_links_only = AgentEyesExtraction(
            source=source,
            payload={
                "title": "ASCO Annual Meeting",
                "dates": "May 29 - June 2, 2026",
                "important_links": ["https://example.com/second"],
            },
            stderr="",
        )

        first = extraction_to_post(12, extraction)
        second = extraction_to_post(12, changed_links_only)

        self.assertEqual(first.stable_id, second.stable_id)
        self.assertEqual(first.source_id, 12)
        self.assertEqual(first.title, "ASCO Annual Meeting")
        self.assertIsNone(first.published_at)
        self.assertIn("Experimental Agent Eyes local extraction", first.body_html or "")

    def test_feed_poller_skips_agent_eyes_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "antenna.db"
            db.init_db(db_path)
            with db.connect(db_path) as conn:
                db.upsert_source(
                    conn,
                    "https://www.asco.org/annual-meeting",
                    title="ASCO Annual Meeting",
                    tags=["agent-eyes"],
                )
                with mock.patch("antenna.fetcher.fetch_feed") as fetch_feed:
                    summary = fetcher.poll_all(conn, poll_delay_seconds=0)

            self.assertEqual(summary["polled"], 0)
            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["per_feed"][0]["skipped_reason"], "agent_eyes_source")
            fetch_feed.assert_not_called()


class AgentEyesCliIntegrationTests(unittest.TestCase):
    def test_fetch_agent_eyes_sources_inserts_snapshot_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "antenna.db"
            db.init_db(db_path)
            source = AgentEyesSourceConfig(
                url="https://www.asco.org/annual-meeting",
                title="ASCO Annual Meeting",
                tags=["oncology", "conference"],
                schema=["title", "dates"],
            )
            cfg = SimpleNamespace(
                database=db_path,
                experimental_agent_eyes=AgentEyesConfig(sources=[source]),
            )
            extraction = AgentEyesExtraction(
                source=source,
                payload={"title": "ASCO Annual Meeting", "dates": "May 29 - June 2, 2026"},
                stderr="",
            )

            with db.connect(db_path) as conn, \
                 mock.patch("antenna.cli.extract_with_agent_eyes", return_value=extraction):
                summary = cli._fetch_agent_eyes_sources(cfg, conn)
                posts = db.recent_posts(conn, limit=5)
                sources = db.list_sources(conn)

            self.assertEqual(summary["polled"], 1)
            self.assertEqual(summary["new_posts"], 1)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["source_title"], "ASCO Annual Meeting")
            self.assertIn("agent-eyes", json.loads(sources[0]["tags_json"]))
