"""SMTP sender + dry-run outbox. Stdlib smtplib only."""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from antenna.config import SmtpConfig
from antenna.renderer import RenderedEmail

log = logging.getLogger(__name__)


@dataclass
class SendResult:
    ok: bool
    detail: str


def _build_message(r: RenderedEmail) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = r.subject
    msg["From"] = formataddr((r.from_name, r.from_address))
    msg["To"] = r.to_address
    msg["Message-ID"] = make_msgid(domain="antennafeed.com")
    msg["X-Mailer"] = "Antenna/0.1"
    # Plain-text fallback from HTML for clients that prefer it.
    text_fallback = _html_to_text(r.html)
    msg.set_content(text_fallback)
    msg.add_alternative(r.html, subtype="html")
    return msg


def send_smtp(smtp: SmtpConfig, r: RenderedEmail) -> SendResult:
    msg = _build_message(r)
    try:
        if smtp.use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=context)
                s.ehlo()
                if smtp.username:
                    s.login(smtp.username, smtp.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as s:
                if smtp.username:
                    s.login(smtp.username, smtp.password)
                s.send_message(msg)
        return SendResult(ok=True, detail=f"sent via {smtp.host}:{smtp.port}")
    except (smtplib.SMTPException, OSError) as e:
        log.warning("SMTP send failed: %s", e)
        return SendResult(ok=False, detail=f"{type(e).__name__}: {e}")
    except Exception as e:
        log.exception("Unexpected SMTP send failure")
        return SendResult(ok=False, detail=f"{type(e).__name__}: {e}")


def write_dry_run(outbox: Path, r: RenderedEmail, tag: str = "") -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _slug(r.subject)
    suffix = f"-{tag}" if tag else ""
    path = outbox / f"{stamp}{suffix}-{slug}.html"
    header = (
        f"<!-- Subject: {r.subject} -->\n"
        f"<!-- From: {r.from_name} <{r.from_address}> -->\n"
        f"<!-- To: {r.to_address} -->\n\n"
    )
    path.write_text(header + r.html, encoding="utf-8")
    return path


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str, max_len: int = 60) -> str:
    slug = _SLUG_RE.sub("-", s.lower()).strip("-")
    return slug[:max_len] or "email"


def _html_to_text(html: str) -> str:
    # Minimal: strip tags, collapse whitespace. Keep line breaks from <br> and </p>.
    txt = re.sub(r"<\s*br\s*/?\s*>", "\n", html, flags=re.I)
    txt = re.sub(r"</\s*p\s*>", "\n\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    # Decode HTML entities
    import html as _html
    txt = _html.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt
