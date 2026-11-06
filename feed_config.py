#!/usr/bin/env python3
"""Read, validate and render the ``feeds.txt`` feed list.

The historical format is ``名称 | URL`` with one feed per line.  The
configuration UI adds two optional, backward compatible columns:

    Name | https://example.com/feed.xml
    Another | https://example.com/other.xml | enabled=false | note=暂停观察

Unknown ``key=value`` columns are preserved so older/newer tooling can add
metadata without losing it.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path


class FeedConfigError(ValueError):
    """Raised for an unreadable or invalid feeds file."""


@dataclass(frozen=True)
class Feed:
    """One configured feed.

    ``name`` is the human-readable name from ``feeds.txt``.  Archive folders
    and database rows use :func:`safe_name` of this value.
    """

    name: str
    url: str
    enabled: bool = True
    note: str = ""
    extra: tuple[str, ...] = ()
    line_no: int = 0

    @property
    def archive_name(self) -> str:
        return safe_name(self.name)

    @property
    def url_key(self) -> str:
        return url_key(self.url)


@dataclass
class RawLine:
    kind: str  # "blank" | "comment" | "feed" | "invalid"
    raw: str
    line_no: int
    feed: Feed | None = None
    error: str | None = None


@dataclass
class FeedsDocument:
    lines: list[RawLine] = field(default_factory=list)
    original_text: str = ""
    source: str = "feeds.txt"
    errors: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str, source: str = "feeds.txt") -> "FeedsDocument":
        document = cls(original_text=text, source=source)
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            raw = raw_line.rstrip("\r")
            stripped = raw.strip()
            if not stripped:
                document.lines.append(RawLine("blank", raw, line_no))
                continue
            if stripped.startswith("#"):
                document.lines.append(RawLine("comment", raw, line_no))
                continue
            try:
                feed = parse_feed_line(stripped, line_no)
            except FeedConfigError as exc:
                message = f"{source}:{line_no} {exc}"
                document.errors.append(message)
                document.lines.append(RawLine("invalid", raw, line_no, error=message))
                continue
            document.lines.append(RawLine("feed", raw, line_no, feed=feed))
        return document

    @classmethod
    def read(cls, path: Path) -> "FeedsDocument":
        if not path.exists():
            raise FeedConfigError(f"找不到订阅源配置文件：{path}")
        return cls.parse(path.read_text(encoding="utf-8"), source=str(path))

    @property
    def feeds(self) -> list[Feed]:
        return [line.feed for line in self.lines if line.kind == "feed" and line.feed]

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()

    def render(self, feeds: list[Feed] | None = None) -> str:
        """Render the document, replacing feed lines but keeping comments.

        Comments and blank lines stay in their original positions.  The feed
        slots are filled in the given order; additional feeds are appended
        after the last original feed slot, and removed feeds leave their slot
        empty.  This keeps the file readable after reordering without silently
        deleting the human's comments.
        """
        if feeds is None:
            feeds = self.feeds
        rendered: list[str] = []
        cursor = 0
        trailing_extra: list[str] = []
        for index, line in enumerate(self.lines):
            if line.kind == "feed":
                if cursor < len(feeds):
                    rendered.append(render_feed(feeds[cursor]))
                    cursor += 1
                continue
            if line.kind == "invalid":
                # Keep the invalid line visible so it can be fixed by hand.
                rendered.append(line.raw)
                continue
            rendered.append(line.raw)
        while cursor < len(feeds):
            trailing_extra.append(render_feed(feeds[cursor]))
            cursor += 1
        if trailing_extra:
            # Append after the last non-trailing content line.
            insert_at = len(rendered)
            while insert_at > 0 and not rendered[insert_at - 1].strip():
                insert_at -= 1
            rendered[insert_at:insert_at] = trailing_extra
        text = "\n".join(rendered)
        if text and not text.endswith("\n"):
            text += "\n"
        return text


def parse_feed_line(line: str, line_no: int) -> Feed:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) == 1:
        name = ""
        url = parts[0]
        extras: list[str] = []
    else:
        name, url = parts[0], parts[1]
        extras = parts[2:]
    if not url:
        raise FeedConfigError("URL 为空")
    parsed = urllib.parse.urlsplit(url)
    if not name:
        name = parsed.netloc or f"feed-{line_no}"
    if not name:
        raise FeedConfigError("订阅源名称不能为空")
    if any(not part for part in extras):
        raise FeedConfigError("扩展列不能为空")
    enabled = True
    note = ""
    kept_extras: list[str] = []
    for part in extras:
        if part.lower() in {"enabled", "true", "yes", "on"}:
            enabled = True
            continue
        if part.lower() in {"disabled", "false", "no", "off"}:
            enabled = False
            continue
        if "=" not in part:
            raise FeedConfigError(f"无法识别的扩展列：{part}")
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "enabled":
            lowered = value.lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                enabled = True
            elif lowered in {"0", "false", "no", "n", "off"}:
                enabled = False
            else:
                raise FeedConfigError(f"enabled 取值不合法：{value}")
        elif key == "note":
            note = value
        else:
            kept_extras.append(part)
    return Feed(
        name=name,
        url=url,
        enabled=enabled,
        note=note,
        extra=tuple(kept_extras),
        line_no=line_no,
    )


def render_feed(feed: Feed) -> str:
    line = f"{feed.name.strip()} | {feed.url.strip()}"
    if not feed.enabled:
        line += " | enabled=false"
    note = feed.note.strip().replace("|", "｜")
    if note:
        line += f" | note={note}"
    for extra in feed.extra:
        line += f" | {extra}"
    return line


def read_feeds(path: Path) -> list[Feed]:
    """Read feeds, raising on a missing file or invalid lines."""
    document = FeedsDocument.read(path)
    if document.errors:
        raise FeedConfigError("；".join(document.errors))
    return document.feeds


def enabled_feeds(path: Path) -> list[Feed]:
    return [feed for feed in read_feeds(path) if feed.enabled]


def disabled_archive_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    document = FeedsDocument.read(path)
    return {feed.archive_name for feed in document.feeds if not feed.enabled}


def validate_feeds(feeds: list[Feed], source: str = "feeds.txt") -> list[dict[str, object]]:
    """Return a list of validation errors with stable fields."""
    errors: list[dict[str, object]] = []
    seen_urls: dict[str, int] = {}
    seen_names: dict[str, int] = {}
    for index, feed in enumerate(feeds):
        name = (feed.name or "").strip()
        url = (feed.url or "").strip()
        if not name:
            errors.append(_error(index, "name", "订阅源名称不能为空"))
        elif "|" in name:
            errors.append(_error(index, "name", "名称不能包含 |"))
        archive_name = safe_name(name) if name else ""
        if name:
            if archive_name in seen_names:
                errors.append(
                    _error(
                        index,
                        "name",
                        f"名称与第 {seen_names[archive_name] + 1} 行归档目录相同（{archive_name}）",
                    )
                )
            else:
                seen_names[archive_name] = index
        url_error = validate_url(url)
        if url_error:
            errors.append(_error(index, "url", url_error))
        elif url:
            key = url_key(url)
            if key in seen_urls:
                errors.append(
                    _error(
                        index,
                        "url",
                        f"URL 与第 {seen_urls[key] + 1} 行重复（忽略末尾 / 与域名大小写）",
                    )
                )
            else:
                seen_urls[key] = index
    return errors


def _error(index: int, field: str, message: str) -> dict[str, object]:
    return {"index": index, "field": field, "message": message}


def validate_url(url: str) -> str:
    """Return an empty string when valid, else a human-readable reason."""
    if not url:
        return "URL 不能为空"
    if any(char.isspace() for char in url):
        return "URL 不能包含空格"
    if "|" in url:
        return "URL 不能包含 |"
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        return f"URL 无法解析：{exc}"
    if parsed.scheme.lower() not in {"http", "https"}:
        return "仅支持 http:// 或 https:// 地址"
    if not parsed.netloc:
        return "URL 缺少主机名"
    try:
        port = parsed.port
    except ValueError:
        return "URL 端口不合法"
    if port is not None and not (1 <= port <= 65535):
        return "URL 端口必须在 1-65535 之间"
    if parsed.hostname is None or not parsed.hostname.strip():
        return "URL 缺少主机名"
    return ""


def normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection."""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port in {80, 443}:
        port = None
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    path = re.sub(r"/+$", "", parsed.path)
    query = parsed.query
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def url_key(url: str) -> str:
    try:
        return normalize_url(url)
    except ValueError:
        return url.strip().lower()


def source_type(url: str, rsshub_host: str = "127.0.0.1", rsshub_port: int = 1200) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "未知"
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in {rsshub_host.lower(), "localhost"} and port == int(rsshub_port):
        return "RSSHub"
    if host in {"127.0.0.1", "localhost"}:
        return "本地服务"
    if "anyfeeder.com" in host:
        return "第三方代理"
    return "RSS/Atom"


def slugify(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value)
    return value.strip(".-")[:90]


def safe_name(value: str) -> str:
    return slugify(value) or stable_hash(value)[:12]


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

