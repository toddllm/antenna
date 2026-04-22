import json
import unittest
from datetime import datetime, timezone

from antenna import waitlist_ops


class WaitlistOpsTests(unittest.TestCase):
    def test_parse_scan_and_filter_internal_rows(self) -> None:
        scan = {
            "Items": [
                {
                    "email": {"S": "todd.deshane+r4@gmail.com"},
                    "created_at": {"S": "2026-04-21T12:00:00+00:00"},
                    "utm_source": {"S": "r4"},
                    "utm_medium": {"S": ""},
                    "utm_campaign": {"S": ""},
                    "referrer": {"S": ""},
                    "user_agent": {"S": "curl/8.7.1"},
                    "source_ip": {"S": "127.0.0.1"},
                },
                {
                    "email": {"S": "me@example.org"},
                    "created_at": {"S": "2026-04-21T13:00:00+00:00"},
                    "utm_source": {"S": ""},
                    "utm_medium": {"S": ""},
                    "utm_campaign": {"S": ""},
                    "referrer": {"S": "https://news.ycombinator.com/"},
                    "user_agent": {
                        "S": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36"
                    },
                    "source_ip": {"S": "127.0.0.2"},
                },
            ]
        }

        entries = waitlist_ops.parse_dynamodb_scan(scan)
        self.assertEqual(len(entries), 2)
        self.assertTrue(waitlist_ops.is_internal_test(entries[0]))
        self.assertFalse(waitlist_ops.is_internal_test(entries[1]))
        self.assertEqual(waitlist_ops.inferred_channel(entries[1]), "hn")

    def test_build_report_prefers_real_hn_and_inoreader_rows(self) -> None:
        scan = {
            "Items": [
                {
                    "email": {"S": "me@timefractal.com"},
                    "created_at": {"S": "2026-04-21T13:08:57+00:00"},
                    "utm_source": {"S": ""},
                    "utm_medium": {"S": ""},
                    "utm_campaign": {"S": ""},
                    "referrer": {"S": "https://news.ycombinator.com/"},
                    "user_agent": {
                        "S": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36"
                    },
                    "source_ip": {"S": "1.1.1.1"},
                },
                {
                    "email": {"S": "reader@example.org"},
                    "created_at": {"S": "2026-04-21T14:52:22+00:00"},
                    "utm_source": {"S": ""},
                    "utm_medium": {"S": ""},
                    "utm_campaign": {"S": ""},
                    "referrer": {"S": "https://www.inoreader.com/"},
                    "user_agent": {
                        "S": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36"
                    },
                    "source_ip": {"S": "2.2.2.2"},
                },
                {
                    "email": {"S": "tdeshane+antenna-preflight@gmail.com"},
                    "created_at": {"S": "2026-04-21T11:32:30+00:00"},
                    "utm_source": {"S": "preflight"},
                    "utm_medium": {"S": "ops"},
                    "utm_campaign": {"S": "launch-day"},
                    "referrer": {"S": "https://antennafeed.com/"},
                    "user_agent": {"S": "curl/8.7.1"},
                    "source_ip": {"S": "3.3.3.3"},
                },
            ]
        }
        report = waitlist_ops.build_report(
            waitlist_ops.parse_dynamodb_scan(scan),
            now=datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc),
            cohort_size=2,
        )

        self.assertEqual(report["totals"]["all_rows"], 3)
        self.assertEqual(report["totals"]["real_signups"], 2)
        self.assertEqual(report["totals"]["internal_or_synthetic"], 1)
        self.assertEqual(report["channel_counts"][0][0], "hn")
        self.assertEqual(len(report["recommended_first_cohort"]), 2)
        channels = {row["channel"] for row in report["recommended_first_cohort"]}
        self.assertEqual(channels, {"hn", "inoreader"})


if __name__ == "__main__":
    unittest.main()
