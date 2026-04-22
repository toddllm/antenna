"""Helpers for turning live waitlist rows into an early-access cohort.

This module is intentionally stdlib-only so it can run anywhere the operator
already has the AWS CLI configured.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse


TEST_UTM_SOURCES = {
    "codex-r2",
    "live-site-test",
    "precheck",
    "preflight",
    "r3",
    "r3-evidence",
    "r4",
    "r5",
    "smoketest",
}

TEST_EMAIL_FRAGMENTS = (
    "example.com",
    "launchcheck",
    "tdeshane",
)


@dataclass(frozen=True)
class WaitlistEntry:
    email: str
    created_at: str
    utm_source: str
    utm_medium: str
    utm_campaign: str
    referrer: str
    user_agent: str
    source_ip: str

    @property
    def created_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.created_at)
        except ValueError:
            return None


def _get_string(attr: dict[str, str] | None) -> str:
    if not attr:
        return ""
    return attr.get("S", "")


def parse_dynamodb_scan(scan_doc: dict[str, Any]) -> list[WaitlistEntry]:
    rows: list[WaitlistEntry] = []
    for item in scan_doc.get("Items", []):
        rows.append(
            WaitlistEntry(
                email=_get_string(item.get("email")).strip().lower(),
                created_at=_get_string(item.get("created_at")).strip(),
                utm_source=_get_string(item.get("utm_source")).strip(),
                utm_medium=_get_string(item.get("utm_medium")).strip(),
                utm_campaign=_get_string(item.get("utm_campaign")).strip(),
                referrer=_get_string(item.get("referrer")).strip(),
                user_agent=_get_string(item.get("user_agent")).strip(),
                source_ip=_get_string(item.get("source_ip")).strip(),
            )
        )
    return sorted(rows, key=lambda row: row.created_at)


def is_internal_test(entry: WaitlistEntry) -> bool:
    source = entry.utm_source.lower()
    email = entry.email.lower()
    ua = entry.user_agent.lower()
    if source in TEST_UTM_SOURCES:
        return True
    if any(fragment in email for fragment in TEST_EMAIL_FRAGMENTS):
        return True
    if ua.startswith("curl/"):
        return True
    return False


def likely_human(entry: WaitlistEntry) -> bool:
    ua = entry.user_agent.lower()
    return "mozilla/" in ua and not is_internal_test(entry)


def inferred_channel(entry: WaitlistEntry) -> str:
    if entry.utm_source and entry.utm_source.lower() not in TEST_UTM_SOURCES:
        return entry.utm_source.lower()
    if not entry.referrer:
        return "direct"
    host = urlparse(entry.referrer).netloc.lower()
    if not host:
        return "direct"
    if host == "news.ycombinator.com":
        return "hn"
    if host.endswith("inoreader.com"):
        return "inoreader"
    if host.endswith("antennafeed.com"):
        return "site"
    return host


def cohort_score(entry: WaitlistEntry, now: datetime) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if is_internal_test(entry):
        return -999, ["internal or synthetic test traffic"]

    if likely_human(entry):
        score += 3
        reasons.append("browser signup")

    channel = inferred_channel(entry)
    if channel == "hn":
        score += 3
        reasons.append("Hacker News referrer")
    elif channel == "inoreader":
        score += 4
        reasons.append("existing reader migration signal")
    elif channel == "site":
        score += 2
        reasons.append("direct site visit")
    elif channel == "direct":
        score += 1
        reasons.append("direct / unattributed visit")
    else:
        score += 1
        reasons.append(f"channel={channel}")

    created_dt = entry.created_dt
    if created_dt is not None:
        if created_dt >= now - timedelta(hours=6):
            score += 2
            reasons.append("fresh signup")
        elif created_dt >= now - timedelta(hours=24):
            score += 1
            reasons.append("same-day signup")

    return score, reasons


def build_report(
    entries: list[WaitlistEntry],
    *,
    now: datetime | None = None,
    cohort_size: int = 5,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    total = len(entries)
    real_entries = [entry for entry in entries if not is_internal_test(entry)]
    candidate_rows = []
    for entry in real_entries:
        score, reasons = cohort_score(entry, now)
        candidate_rows.append(
            {
                "email": entry.email,
                "created_at": entry.created_at,
                "channel": inferred_channel(entry),
                "likely_human": likely_human(entry),
                "score": score,
                "reason": ", ".join(reasons),
                "referrer": entry.referrer or "(none)",
            }
        )
    candidate_rows.sort(key=lambda row: (-row["score"], row["created_at"], row["email"]))

    def count_since(window_hours: int) -> int:
        cutoff = now - timedelta(hours=window_hours)
        return sum(1 for entry in real_entries if entry.created_dt and entry.created_dt >= cutoff)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "totals": {
            "all_rows": total,
            "real_signups": len(real_entries),
            "internal_or_synthetic": total - len(real_entries),
            "real_last_24h": count_since(24),
            "real_last_6h": count_since(6),
            "real_last_1h": count_since(1),
            "likely_human": sum(1 for entry in real_entries if likely_human(entry)),
        },
        "channel_counts": Counter(inferred_channel(entry) for entry in real_entries).most_common(),
        "referrer_counts": Counter((entry.referrer or "(none)") for entry in real_entries).most_common(),
        "recommended_first_cohort": candidate_rows[: max(1, cohort_size)],
        "recent_real_signups": [
            {
                "email": entry.email,
                "created_at": entry.created_at,
                "channel": inferred_channel(entry),
                "referrer": entry.referrer or "(none)",
            }
            for entry in real_entries[-8:]
        ],
    }
