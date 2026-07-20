"""Minimal, dependency-free RSS 2.0 / Atom feed parsing (stdlib ``xml.etree``).

Kept deliberately small: the news adapters only need id/title/summary/link/date
per entry, over fixtures we control. Malformed XML raises ``FeedParseError`` which
the adapters treat as a (fail-open) empty fetch rather than a cycle-aborting crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class FeedParseError(Exception):
    """Raised when a feed body cannot be parsed as RSS or Atom."""


@dataclass(frozen=True)
class FeedEntry:
    id: str
    title: str
    summary: str
    link: str | None
    published_at: datetime


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_date(raw: str) -> datetime:
    """Parse RFC-822 (RSS ``pubDate``) or ISO-8601/RFC-3339 (Atom) dates.

    Always returns a timezone-aware UTC datetime; unparseable dates fall back to
    the epoch so a single bad entry never breaks the whole feed.
    """
    raw = raw.strip()
    if not raw:
        return datetime(1970, 1, 1, tzinfo=UTC)
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime(1970, 1, 1, tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_rss(root: ET.Element) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    for item in root.iter("item"):
        title = _text(item.find("title"))
        link = _text(item.find("link")) or None
        guid = _text(item.find("guid")) or link or title
        summary = _text(item.find("description"))
        published = _text(item.find("pubDate")) or _text(item.find("date"))
        entries.append(
            FeedEntry(
                id=guid,
                title=title,
                summary=summary,
                link=link,
                published_at=_parse_date(published),
            )
        )
    return entries


def _atom_link(entry: ET.Element) -> str | None:
    for link in entry.findall(f"{_ATOM_NS}link"):
        rel = link.get("rel", "alternate")
        if rel == "alternate" and link.get("href"):
            return link.get("href")
    first = entry.find(f"{_ATOM_NS}link")
    return first.get("href") if first is not None else None


def _parse_atom(root: ET.Element) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    for entry in root.iter(f"{_ATOM_NS}entry"):
        title = _text(entry.find(f"{_ATOM_NS}title"))
        entry_id = _text(entry.find(f"{_ATOM_NS}id")) or title
        summary = _text(entry.find(f"{_ATOM_NS}summary")) or _text(
            entry.find(f"{_ATOM_NS}content")
        )
        published = _text(entry.find(f"{_ATOM_NS}published")) or _text(
            entry.find(f"{_ATOM_NS}updated")
        )
        entries.append(
            FeedEntry(
                id=entry_id,
                title=title,
                summary=summary,
                link=_atom_link(entry),
                published_at=_parse_date(published),
            )
        )
    return entries


def parse_feed(body: str) -> list[FeedEntry]:
    """Parse an RSS-2.0 or Atom document into ``FeedEntry`` records."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedParseError(str(exc)) from exc
    tag = root.tag.lower()
    if tag.endswith("rss") or tag.endswith("rdf"):
        return _parse_rss(root)
    if tag.endswith("feed"):
        return _parse_atom(root)
    # Some feeds wrap items without a recognizable root; try both.
    entries = _parse_rss(root)
    return entries or _parse_atom(root)
