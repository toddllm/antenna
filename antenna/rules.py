"""Rule evaluation — include/exclude keyword filters + alert flag."""

from __future__ import annotations

import fnmatch
import re
import sqlite3
from dataclasses import dataclass, field

from antenna.config import Rule


@dataclass
class RuleDecision:
    include: bool = True
    alert: bool = False
    matched_rules: list[str] = field(default_factory=list)


def _matches_feed(rule: Rule, source_url: str) -> bool:
    if rule.match == "*" or not rule.match:
        return True
    return fnmatch.fnmatch(source_url, rule.match) or rule.match == source_url


def _has_term(haystack: str, term: str) -> bool:
    if not haystack:
        return False
    if term.startswith("/") and term.endswith("/") and len(term) > 2:
        # Regex rule: /pattern/
        try:
            return re.search(term[1:-1], haystack, re.IGNORECASE) is not None
        except re.error:
            return False
    return term.lower() in haystack.lower()


def decide(
    rules: list[Rule],
    source_url: str,
    title: str | None,
    body_text: str | None,
) -> RuleDecision:
    """Evaluate rules in order. Later include/exclude override earlier,
    since they act as filters. Alert is sticky-true."""
    haystack = " ".join(filter(None, [title or "", body_text or ""]))
    include = True
    alert = False
    matched: list[str] = []

    for r in rules:
        if not _matches_feed(r, source_url):
            continue

        # Exclude terms are evaluated first — any match excludes.
        if r.exclude:
            if any(_has_term(haystack, t) for t in r.exclude):
                include = False
                matched.append(f"exclude({r.match}): {r.exclude}")
                continue

        # Include terms: if set, at least one must match for inclusion.
        if r.include:
            if any(_has_term(haystack, t) for t in r.include):
                matched.append(f"include({r.match}): {r.include}")
                if r.alert:
                    alert = True
            else:
                # include is declared but none matched — exclude.
                include = False

    return RuleDecision(include=include, alert=alert, matched_rules=matched)
