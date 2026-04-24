"""Experimental local bridge to the unreleased agent-eyes CLI.

This module is deliberately small and subprocess-based so Antenna does not take
on browser or LLM dependencies in its stable install path.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from antenna import db
from antenna.config import AgentEyesConfig, AgentEyesSourceConfig


class AgentEyesError(RuntimeError):
    """Raised when the local agent-eyes binary cannot produce valid JSON."""


@dataclass
class AgentEyesExtraction:
    source: AgentEyesSourceConfig
    payload: dict[str, Any]
    stderr: str


def extract_with_agent_eyes(
    source: AgentEyesSourceConfig,
    settings: AgentEyesConfig,
    *,
    env: dict[str, str] | None = None,
) -> AgentEyesExtraction:
    """Run the local agent-eyes binary and parse its JSON stdout."""
    cmd = [
        settings.binary,
        "extract",
        source.url,
        source.instruction,
        "--model",
        settings.model,
    ]
    if source.schema:
        cmd.extend(["--schema", ",".join(source.schema)])
    if source.settle_ms:
        cmd.extend(["--settle", str(source.settle_ms)])
    if source.cookies_file:
        cmd.extend(["--cookies", str(Path(source.cookies_file).expanduser())])
    if source.stealth:
        cmd.append("--stealth")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            env=env or os.environ.copy(),
            text=True,
            timeout=max(5, int(settings.timeout_seconds)),
        )
    except FileNotFoundError as exc:
        raise AgentEyesError(
            f"agent-eyes binary not found: {settings.binary!r}. "
            "Install Agent Eyes locally or set experimental_agent_eyes.binary."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentEyesError(
            f"agent-eyes timed out after {settings.timeout_seconds}s for {source.url}"
        ) from exc

    if proc.returncode != 0:
        detail = _trim(proc.stderr or proc.stdout or "unknown error")
        raise AgentEyesError(f"agent-eyes failed for {source.url}: {detail}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AgentEyesError(
            f"agent-eyes returned non-JSON output for {source.url}: {_trim(proc.stdout)}"
        ) from exc
    if not isinstance(payload, dict):
        raise AgentEyesError(f"agent-eyes returned {type(payload).__name__}, expected object")

    return AgentEyesExtraction(source=source, payload=payload, stderr=proc.stderr)


def extraction_to_post(source_id: int, extraction: AgentEyesExtraction) -> db.NewPost:
    """Represent an Agent Eyes state snapshot as one Antenna post."""
    source = extraction.source
    payload = extraction.payload
    title = _first_text(payload, "title", "headline", "name") or source.title or source.url
    identity_payload = _identity_payload(source, payload)
    stable_id = "agent-eyes:" + hashlib.sha1(
        json.dumps(
            {"url": source.url, "mode": source.mode, "payload": identity_payload},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    body_text = _payload_text(source, payload)
    body_html = _payload_html(source, payload)
    return db.NewPost(
        source_id=source_id,
        stable_id=stable_id,
        url=source.url,
        title=title,
        author="Agent Eyes",
        published_at=None,
        body_html=body_html,
        body_text=body_text,
    )


def _identity_payload(source: AgentEyesSourceConfig, payload: dict[str, Any]) -> dict[str, Any]:
    if not source.stable_fields:
        return payload
    return {field: payload.get(field) for field in source.stable_fields}


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _payload_html(source: AgentEyesSourceConfig, payload: dict[str, Any]) -> str:
    rows = []
    for key, value in payload.items():
        rows.append(
            "<tr>"
            f"<th>{html.escape(str(key))}</th>"
            f"<td>{html.escape(_stringify(value))}</td>"
            "</tr>"
        )
    title = html.escape(source.title or source.url)
    url = html.escape(source.url, quote=True)
    return (
        f"<h2>{title}</h2>"
        f'<p><a href="{url}">{url}</a></p>'
        "<p><em>Experimental Agent Eyes local extraction.</em></p>"
        "<table>"
        + "".join(rows)
        + "</table>"
    )


def _payload_text(source: AgentEyesSourceConfig, payload: dict[str, Any]) -> str:
    """Put human-readable scalar fields before bulky arrays in digest excerpts."""
    seen: set[str] = set()
    ordered_keys: list[str] = []
    priority = [
        "title",
        "headline",
        "name",
        "dates",
        "date",
        "location",
        "venue",
        "page_type",
        "status",
        *source.schema,
    ]
    for key in [*priority, *payload.keys()]:
        if key in payload and key not in seen:
            ordered_keys.append(key)
            seen.add(key)

    scalar_lines: list[str] = []
    bulky_lines: list[str] = []
    for key in ordered_keys:
        value = payload.get(key)
        line = f"{key}: {_summary_value(value)}"
        if isinstance(value, (dict, list)):
            bulky_lines.append(line)
        else:
            scalar_lines.append(line)
    return "\n".join([*scalar_lines, *bulky_lines])


def _summary_value(value: Any) -> str:
    if isinstance(value, list):
        items = [_stringify(item).replace("\n", " ") for item in value[:3]]
        suffix = f" (+{len(value) - 3} more)" if len(value) > 3 else ""
        return ", ".join(items) + suffix
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return "" if value is None else str(value)


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    return "" if value is None else str(value)


def _trim(value: str, limit: int = 1000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "...(truncated)"
