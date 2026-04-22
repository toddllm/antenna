"""OPML import — reads Blogtrottr/feedly/Inoreader-style exports."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OpmlEntry:
    url: str
    title: str | None
    category: str | None


def parse_opml(path: str | Path) -> list[OpmlEntry]:
    tree = ET.parse(str(path))
    root = tree.getroot()
    entries: list[OpmlEntry] = []
    _walk(root, category=None, out=entries)
    # Dedupe by URL, preserve first seen.
    seen: set[str] = set()
    deduped: list[OpmlEntry] = []
    for e in entries:
        if e.url in seen:
            continue
        seen.add(e.url)
        deduped.append(e)
    return deduped


def _walk(node: ET.Element, category: str | None, out: list[OpmlEntry]) -> None:
    for child in node:
        tag = child.tag.lower().split("}")[-1]  # strip namespace
        if tag != "outline":
            _walk(child, category, out)
            continue
        xml_url = child.get("xmlUrl") or child.get("xmlurl")
        if xml_url:
            title = child.get("title") or child.get("text")
            out.append(OpmlEntry(url=xml_url, title=title, category=category))
        else:
            # Folder node; recurse with its title as category.
            sub_cat = child.get("title") or child.get("text") or category
            _walk(child, sub_cat, out)
