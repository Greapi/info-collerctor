#!/usr/bin/env python3
"""Fetch RSS/Atom feeds and save entries as Markdown files."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import re
import sqlite3
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_CONFIG = Path("feeds.txt")
DEFAULT_OUTPUT = Path("rss-downloads")
DEFAULT_DB = Path(".rss_collector.sqlite3")
USER_AGENT = "info-collector-rss/0.1 (+local markdown archiver)"


@dataclass(frozen=True)
class Feed:
    name: str
    url: str


@dataclass(frozen=True)
class Entry:
    feed_name: str
    feed_url: str
    title: str
    link: str
    entry_id: str
    published: str
    author: str
    content: str


class MarkdownHTMLParser(HTMLParser):
    """Small HTML-to-Markdown converter for common RSS summary markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href_stack: list[str | None] = []
        self._list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"p", "div", "section", "article", "header", "footer"}:
            self._block()
        elif tag in {"br"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3"}:
            self._block()
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "pre":
            self._block()
            self.parts.append("```\n")
        elif tag in {"ul", "ol"}:
            self._list_depth += 1
            self._block()
        elif tag == "li":
            self._block()
            self.parts.append("  " * max(self._list_depth - 1, 0) + "- ")
        elif tag == "blockquote":
            self._block()
            self.parts.append("> ")
        elif tag == "a":
            self._href_stack.append(attrs_dict.get("href"))
            self.parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "section", "article", "header", "footer", "li", "blockquote"}:
            self._block()
        elif tag in {"h1", "h2", "h3"}:
            self._block()
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag in {"ul", "ol"}:
            self._list_depth = max(0, self._list_depth - 1)
            self._block()
        elif tag == "a":
            href = self._href_stack.pop() if self._href_stack else None
            if href:
                self.parts.append(f"]({href})")
            else:
                self.parts.append("]")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _block(self) -> None:
        if self.parts and not "".join(self.parts[-2:]).endswith("\n\n"):
            self.parts.append("\n\n")


def html_to_markdown(value: str) -> str:
    if not value:
        return ""
    parser = MarkdownHTMLParser()
    parser.feed(value)
    parser.close()
    markdown = parser.markdown()
    if markdown:
        return markdown
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def read_feeds(path: Path) -> list[Feed]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    feeds: list[Feed] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, url = [part.strip() for part in line.split("|", 1)]
        else:
            url = line
            parsed = urllib.parse.urlparse(url)
            name = parsed.netloc or f"feed-{line_no}"
        if not name or not url:
            raise ValueError(f"Invalid feed config at {path}:{line_no}")
        feeds.append(Feed(name=safe_name(name), url=url))
    return feeds


def fetch_url(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_feed(feed: Feed, payload: bytes) -> list[Entry]:
    root = ET.fromstring(payload)
    if strip_namespace(root.tag) == "feed":
        return parse_atom(feed, root)
    channel = root.find("./channel")
    if channel is None:
        channel = find_child(root, "channel")
    if channel is None:
        raise ValueError("Unsupported feed format")
    return parse_rss(feed, channel)


def parse_rss(feed: Feed, channel: ET.Element) -> list[Entry]:
    entries: list[Entry] = []
    for item in children_named(channel, "item"):
        title = child_text(item, "title") or "(untitled)"
        link = child_text(item, "link")
        guid = child_text(item, "guid")
        published = normalize_date(child_text(item, "pubDate") or child_text(item, "date"))
        author = child_text(item, "author") or child_text(item, "creator")
        content = child_text(item, "encoded") or child_text(item, "description")
        entry_id = guid or link or stable_hash(f"{feed.url}:{title}:{published}")
        entries.append(
            Entry(
                feed_name=feed.name,
                feed_url=feed.url,
                title=title.strip(),
                link=(link or "").strip(),
                entry_id=entry_id.strip(),
                published=published,
                author=(author or "").strip(),
                content=content or "",
            )
        )
    return entries


def parse_atom(feed: Feed, root: ET.Element) -> list[Entry]:
    entries: list[Entry] = []
    for item in children_named(root, "entry"):
        title = child_text(item, "title") or "(untitled)"
        link = atom_link(item)
        entry_id = child_text(item, "id") or link or stable_hash(f"{feed.url}:{title}")
        published = normalize_date(child_text(item, "published") or child_text(item, "updated"))
        author = atom_author(item)
        content = child_text(item, "content") or child_text(item, "summary")
        entries.append(
            Entry(
                feed_name=feed.name,
                feed_url=feed.url,
                title=title.strip(),
                link=link.strip(),
                entry_id=entry_id.strip(),
                published=published,
                author=author.strip(),
                content=content or "",
            )
        )
    return entries


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            feed_name TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            link TEXT NOT NULL,
            title TEXT NOT NULL,
            file_path TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (feed_name, entry_id)
        )
        """
    )
    return conn


def already_saved(conn: sqlite3.Connection, entry: Entry) -> bool:
    row = conn.execute(
        "SELECT 1 FROM entries WHERE feed_name = ? AND entry_id = ?",
        (entry.feed_name, entry.entry_id),
    ).fetchone()
    return row is not None


def mark_saved(conn: sqlite3.Connection, entry: Entry, file_path: Path) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO entries
            (feed_name, entry_id, link, title, file_path, saved_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            entry.feed_name,
            entry.entry_id,
            entry.link,
            entry.title,
            str(file_path),
            dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        ),
    )


def save_entry(entry: Entry, output_dir: Path) -> Path:
    date_part = date_for_path(entry.published)
    feed_dir = output_dir / entry.feed_name / date_part
    feed_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slugify(entry.title) or stable_hash(entry.entry_id)[:12]}.md"
    path = unique_path(feed_dir / filename)
    path.write_text(render_markdown(entry), encoding="utf-8")
    return path


def render_markdown(entry: Entry) -> str:
    body = html_to_markdown(entry.content)
    if not body:
        body = "_No content was included in this RSS item._"

    metadata = [
        "---",
        f'title: "{yaml_escape(entry.title)}"',
        f"feed: {entry.feed_name}",
        f'feed_url: "{yaml_escape(entry.feed_url)}"',
        f'link: "{yaml_escape(entry.link)}"',
        f'published: "{yaml_escape(entry.published)}"',
    ]
    if entry.author:
        metadata.append(f'author: "{yaml_escape(entry.author)}"')
    metadata.extend(["---", ""])

    title = f"# {entry.title}\n"
    source = f"\n\nSource: [{entry.link}]({entry.link})\n" if entry.link else ""
    return "\n".join(metadata) + title + "\n" + body.strip() + source


def collect(config: Path, output: Path, db_path: Path, timeout: int, limit: int | None) -> int:
    feeds = read_feeds(config)
    output.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    saved_count = 0

    with conn:
        for feed in feeds:
            print(f"Fetching {feed.name}: {feed.url}")
            try:
                entries = parse_feed(feed, fetch_url(feed.url, timeout=timeout))
            except (ET.ParseError, ValueError, urllib.error.URLError, TimeoutError) as exc:
                print(f"  failed: {exc}", file=sys.stderr)
                continue

            if limit is not None:
                entries = entries[:limit]
            for entry in entries:
                if already_saved(conn, entry):
                    continue
                file_path = save_entry(entry, output)
                mark_saved(conn, entry, file_path)
                saved_count += 1
                print(f"  saved: {file_path}")

    print(f"Done. Saved {saved_count} new item(s).")
    return saved_count


def child_text(element: ET.Element, name: str) -> str:
    child = find_child(element, name)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def find_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if strip_namespace(child.tag) == name:
            return child
    return None


def children_named(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if strip_namespace(child.tag) == name]


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def atom_link(item: ET.Element) -> str:
    fallback = ""
    for child in item:
        if strip_namespace(child.tag) != "link":
            continue
        href = child.attrib.get("href", "")
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate" and href:
            return href
        fallback = fallback or href
    return fallback


def atom_author(item: ET.Element) -> str:
    author = find_child(item, "author")
    if author is None:
        return ""
    return child_text(author, "name") or "".join(author.itertext()).strip()


def normalize_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.isoformat()
    except ValueError:
        pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    return value


def date_for_path(value: str) -> str:
    if not value:
        return "undated"
    try:
        return dt.datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        match = re.search(r"\d{4}-\d{2}-\d{2}", value)
        return match.group(0) if match else "undated"


def slugify(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value)
    value = value.strip(".-")
    return value[:90]


def safe_name(value: str) -> str:
    return slugify(value) or stable_hash(value)[:12]


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a unique filename for {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch RSS/Atom feeds and save new entries as Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            feeds.txt format:
              # comments are allowed
              Feed Name | https://example.com/rss.xml
              https://example.org/feed.atom
            """
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Feed list file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Markdown output folder.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite file used for deduplication.")
    parser.add_argument("--timeout", type=int, default=20, help="Network timeout in seconds.")
    parser.add_argument("--limit", type=int, help="Maximum entries to save per feed.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        collect(
            config=args.config,
            output=args.output,
            db_path=args.db,
            timeout=args.timeout,
            limit=args.limit,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
