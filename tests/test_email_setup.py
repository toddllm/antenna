import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from antenna import cli
from antenna.config import Config
from antenna.email_setup import assess_smtp_config


class SmtpAssessmentTests(unittest.TestCase):
    def test_generic_smtp_without_auth_is_still_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "antenna.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "database: antenna.db",
                        "outbox: outbox",
                        "",
                        "smtp:",
                        "  host: smtp.example.test",
                        "  port: 25",
                        '  username: ""',
                        '  password: ""',
                        "  use_tls: false",
                        "",
                        "email:",
                        "  from_address: relay@example.test",
                        "  to_address: inbox@example.test",
                    ]
                )
            )

            cfg = Config.load(cfg_path)
            assessment = assess_smtp_config(cfg)

        self.assertTrue(assessment.configured)
        self.assertEqual(assessment.auth_mode, "none")
        self.assertEqual(assessment.missing_fields, [])
        self.assertEqual(assessment.placeholder_fields, [])

    def test_example_placeholders_are_flagged_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "antenna.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "database: antenna.db",
                        "outbox: outbox",
                        "",
                        "smtp:",
                        "  host: smtp.gmail.com",
                        "  port: 587",
                        "  username: you@gmail.com",
                        '  password: "YOUR_16_CHAR_APP_PASSWORD"',
                        "  use_tls: true",
                        "",
                        "email:",
                        "  from_address: you@gmail.com",
                        "  to_address: you+antenna@gmail.com",
                    ]
                )
            )

            cfg = Config.load(cfg_path)
            assessment = assess_smtp_config(cfg)

        self.assertFalse(assessment.configured)
        self.assertEqual(
            assessment.placeholder_fields,
            [
                "email.from_address",
                "email.to_address",
                "smtp.username",
                "smtp.password",
            ],
        )


class SetupEmailCommandTests(unittest.TestCase):
    def test_setup_email_gmail_writes_config_and_backup(self) -> None:
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
                        "  host: smtp.gmail.com",
                        "  port: 587",
                        '  username: ""',
                        '  password: ""',
                        "  use_tls: true",
                        "",
                        "email:",
                        '  from_address: ""',
                        '  to_address: ""',
                        '  from_name_template: "{feed_title}"',
                    ]
                )
            )
            args = SimpleNamespace(
                config=str(cfg_path),
                provider="gmail",
                gmail_address="person@gmail.com",
                app_password="abcd efgh ijkl mnop",
                region="us-east-1",
                host=None,
                port="587",
                username=None,
                password=None,
                from_address=None,
                to_address="person+antenna@gmail.com",
                from_name_template="{feed_title}",
                no_tls=False,
                dry_run=False,
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.cmd_setup_email(args)

            self.assertEqual(rc, 0)
            cfg = Config.load(cfg_path)
            self.assertEqual(cfg.smtp.host, "smtp.gmail.com")
            self.assertEqual(cfg.smtp.username, "person@gmail.com")
            self.assertEqual(cfg.smtp.password, "abcd efgh ijkl mnop")
            self.assertEqual(cfg.email.from_address, "person@gmail.com")
            self.assertEqual(cfg.email.to_address, "person+antenna@gmail.com")
            backups = list(root.glob("antenna.yaml.bak.*"))
            self.assertEqual(len(backups), 1)


class TestEmailCommandTests(unittest.TestCase):
    def test_test_email_dry_run_writes_preview(self) -> None:
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
                        '  username: ""',
                        '  password: ""',
                        "  use_tls: true",
                        "",
                        "email:",
                        "  from_address: you@example.test",
                        "  to_address: inbox@example.test",
                    ]
                )
            )
            args = SimpleNamespace(
                config=str(cfg_path),
                to=None,
                subject="Antenna smoke",
                dry_run=True,
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.cmd_test_email(args)

            self.assertEqual(rc, 0)
            outbox = root / "outbox"
            previews = list(outbox.glob("*smtp-test*.html"))
            self.assertEqual(len(previews), 1)

    def test_test_email_live_blocks_placeholder_config(self) -> None:
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
                        "  host: smtp.gmail.com",
                        "  port: 587",
                        "  username: you@gmail.com",
                        '  password: "YOUR_16_CHAR_APP_PASSWORD"',
                        "  use_tls: true",
                        "",
                        "email:",
                        "  from_address: you@gmail.com",
                        "  to_address: you+antenna@gmail.com",
                    ]
                )
            )
            args = SimpleNamespace(
                config=str(cfg_path),
                to=None,
                subject="Antenna smoke",
                dry_run=False,
            )
            stderr = io.StringIO()

            with mock.patch("antenna.cli.send_smtp") as mock_send, \
                 contextlib.redirect_stderr(stderr):
                rc = cli.cmd_test_email(args)

            self.assertEqual(rc, 1)
            self.assertIn("Email route is not ready for live sends", stderr.getvalue())
            mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
