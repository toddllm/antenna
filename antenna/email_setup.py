"""Helpers for configuring and validating Antenna email delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from antenna.config import Config

PROVIDERS = ("gmail", "ses", "generic")


@dataclass(frozen=True)
class SmtpAssessment:
    configured: bool
    auth_mode: str
    missing_fields: list[str]
    placeholder_fields: list[str]


_PLACEHOLDER_EMAILS = {
    "you@gmail.com",
    "you+antenna@gmail.com",
    "you@example.com",
    "you@yourdomain.com",
    "feeds@yourdomain.com",
}

_PLACEHOLDER_VALUES = {
    "your_16_char_app_password",
    "your_smtp_username",
    "your_smtp_password",
}


def _is_placeholder(field: str, value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized in _PLACEHOLDER_EMAILS or normalized in _PLACEHOLDER_VALUES:
        return True
    if "yourdomain.com" in normalized:
        return True
    if normalized.startswith("your_"):
        return True
    if field == "smtp.password" and "app_password" in normalized:
        return True
    return False


def assess_smtp_config(cfg: Config) -> SmtpAssessment:
    missing: list[str] = []
    placeholders: list[str] = []
    if not cfg.smtp.host:
        missing.append("smtp.host")
    elif _is_placeholder("smtp.host", cfg.smtp.host):
        placeholders.append("smtp.host")
    if not cfg.email.from_address:
        missing.append("email.from_address")
    elif _is_placeholder("email.from_address", cfg.email.from_address):
        placeholders.append("email.from_address")
    if not cfg.email.to_address:
        missing.append("email.to_address")
    elif _is_placeholder("email.to_address", cfg.email.to_address):
        placeholders.append("email.to_address")
    if cfg.smtp.username and not cfg.smtp.password:
        missing.append("smtp.password")
    if cfg.smtp.password and not cfg.smtp.username:
        missing.append("smtp.username")
    if cfg.smtp.username and _is_placeholder("smtp.username", cfg.smtp.username):
        placeholders.append("smtp.username")
    if cfg.smtp.password and _is_placeholder("smtp.password", cfg.smtp.password):
        placeholders.append("smtp.password")
    return SmtpAssessment(
        configured=not missing and not placeholders,
        auth_mode="login" if cfg.smtp.username else "none",
        missing_fields=missing,
        placeholder_fields=placeholders,
    )


def build_provider_settings(
    *,
    provider: str,
    from_name_template: str,
    gmail_address: str | None = None,
    app_password: str | None = None,
    region: str = "us-east-1",
    host: str | None = None,
    port: int = 587,
    username: str | None = None,
    password: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    use_tls: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown email provider preset: {provider}")

    if provider == "gmail":
        if not gmail_address:
            raise ValueError("--gmail-address is required for --provider gmail")
        if not app_password:
            raise ValueError("--app-password is required for --provider gmail")
        smtp = {
            "host": "smtp.gmail.com",
            "port": 587,
            "username": gmail_address,
            "password": app_password,
            "use_tls": True,
        }
        email = {
            "from_address": from_address or gmail_address,
            "to_address": to_address or gmail_address,
            "from_name_template": from_name_template,
        }
        return smtp, email

    if provider == "ses":
        if not username:
            raise ValueError("--username is required for --provider ses")
        if not password:
            raise ValueError("--password is required for --provider ses")
        if not from_address:
            raise ValueError("--from-address is required for --provider ses")
        smtp = {
            "host": f"email-smtp.{region}.amazonaws.com",
            "port": int(port),
            "username": username,
            "password": password,
            "use_tls": bool(use_tls),
        }
        email = {
            "from_address": from_address,
            "to_address": to_address or from_address,
            "from_name_template": from_name_template,
        }
        return smtp, email

    if not host:
        raise ValueError("--host is required for --provider generic")
    if not from_address:
        raise ValueError("--from-address is required for --provider generic")
    smtp = {
        "host": host,
        "port": int(port),
        "username": username or "",
        "password": password or "",
        "use_tls": bool(use_tls),
    }
    email = {
        "from_address": from_address,
        "to_address": to_address or from_address,
        "from_name_template": from_name_template,
    }
    return smtp, email


def rewrite_email_config(
    config_path: str | Path,
    *,
    smtp: dict[str, Any],
    email: dict[str, Any],
) -> tuple[Path, Path]:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy antenna.example.yaml there first."
        )

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file did not parse to a mapping: {path}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    backup.write_text(path.read_text())

    data["smtp"] = smtp
    data["email"] = email

    rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(rendered)
    tmp.replace(path)
    return path, backup
