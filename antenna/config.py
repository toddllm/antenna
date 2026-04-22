"""Config loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True


@dataclass
class EmailConfig:
    from_address: str
    to_address: str
    from_name_template: str = "{feed_title}"


@dataclass
class FeedConfig:
    url: str
    title: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Rule:
    match: str  # feed URL glob, or "*" for all
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    alert: bool = False


@dataclass
class Config:
    database: Path
    outbox: Path
    smtp: SmtpConfig
    email: EmailConfig
    feeds: list[FeedConfig]
    rules: list[Rule]
    default_mode: str = "per_post"
    first_run_entries: int = 3
    poll_delay_seconds: float = 0.5
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        base = path.parent

        def resolve(p: str) -> Path:
            pp = Path(p).expanduser()
            return pp if pp.is_absolute() else (base / pp).resolve()

        smtp_d = data.get("smtp", {})
        email_d = data.get("email", {})

        return cls(
            database=resolve(data.get("database", "antenna.db")),
            outbox=resolve(data.get("outbox", "outbox")),
            smtp=SmtpConfig(
                host=smtp_d.get("host", "smtp.gmail.com"),
                port=int(smtp_d.get("port", 587)),
                username=smtp_d.get("username", ""),
                password=smtp_d.get("password", ""),
                use_tls=bool(smtp_d.get("use_tls", True)),
            ),
            email=EmailConfig(
                from_address=email_d.get("from_address", ""),
                to_address=email_d.get("to_address", ""),
                from_name_template=email_d.get("from_name_template", "{feed_title}"),
            ),
            feeds=[
                FeedConfig(
                    url=f["url"],
                    title=f.get("title"),
                    tags=list(f.get("tags", [])),
                )
                for f in data.get("feeds", [])
            ],
            rules=[
                Rule(
                    match=r.get("match", "*"),
                    include=list(r.get("include", [])),
                    exclude=list(r.get("exclude", [])),
                    alert=bool(r.get("alert", False)),
                )
                for r in data.get("rules", [])
            ],
            default_mode=data.get("default_mode", "per_post"),
            first_run_entries=int(data.get("first_run_entries", 3)),
            poll_delay_seconds=float(data.get("poll_delay_seconds", 0.5)),
            config_path=path,
        )


def default_config_path() -> Path:
    """Resolve the default config path. Checks $ANTENNA_CONFIG, then ./antenna.yaml,
    then ~/.config/antenna/antenna.yaml."""
    import os

    env = os.environ.get("ANTENNA_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    local = Path("antenna.yaml").resolve()
    if local.exists():
        return local
    return (Path.home() / ".config" / "antenna" / "antenna.yaml").resolve()
