#!/usr/bin/env python3
"""Small, dependency-free `.env` reader/writer that preserves comments.

The existing collector treats `.env` as a line oriented ``KEY=VALUE`` file and
lets real environment variables override it.  This module keeps the same
semantics but records every original line so the configuration UI can update a
few keys without destroying comments, ordering or unknown keys.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


class EnvFileError(ValueError):
    """Raised when an `.env` file cannot be parsed or rendered."""


@dataclass
class EnvLine:
    """One physical line of an `.env` file."""

    kind: str  # "blank" | "comment" | "entry" | "invalid"
    raw: str
    line_no: int
    key: str | None = None
    value: str | None = None
    export: bool = False
    error: str | None = None
    dirty: bool = False


@dataclass
class EnvDocument:
    """Parsed `.env` document preserving comments and unknown keys."""

    lines: list[EnvLine] = field(default_factory=list)
    original_text: str = ""
    source: str = ".env"
    errors: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str, source: str = ".env") -> "EnvDocument":
        document = cls(original_text=text, source=source)
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            raw = line_no_line(raw_line)
            stripped = raw.strip()
            if not stripped:
                document.lines.append(EnvLine("blank", raw, line_no))
                continue
            if stripped.startswith("#"):
                document.lines.append(EnvLine("comment", raw, line_no))
                continue
            body = stripped
            export = False
            if body.startswith("export "):
                export = True
                body = body[len("export ") :].strip()
            if "=" not in body:
                message = f"{source}:{line_no} 不是 KEY=VALUE 格式"
                document.errors.append(message)
                document.lines.append(EnvLine("invalid", raw, line_no, error=message))
                continue
            key, raw_value = body.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or ""):
                message = f"{source}:{line_no} 变量名不合法：{key or '(空)'}"
                document.errors.append(message)
                document.lines.append(EnvLine("invalid", raw, line_no, error=message))
                continue
            value = unquote_value(raw_value.strip())
            document.lines.append(
                EnvLine(
                    "entry",
                    raw,
                    line_no,
                    key=key,
                    value=value,
                    export=export,
                )
            )
        return document

    @classmethod
    def read(cls, path: Path) -> "EnvDocument":
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(original_text="", source=str(path))
        return cls.parse(text, source=str(path))

    @property
    def values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.lines:
            if line.kind == "entry" and line.key is not None:
                values[line.key] = line.value or ""
        return values

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set(self, key: str, value: str) -> bool:
        """Set a key on every existing line. Return True if a line was added."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise EnvFileError(f"变量名不合法：{key}")
        found = False
        for line in self.lines:
            if line.kind == "entry" and line.key == key:
                line.value = str(value)
                line.dirty = True
                found = True
        if found:
            return False
        new_line = EnvLine(
            "entry",
            "",
            0,
            key=key,
            value=str(value),
            dirty=True,
        )
        self.lines.append(new_line)
        return True

    def delete(self, key: str) -> int:
        """Remove every line that defines ``key`` and return the count."""
        kept: list[EnvLine] = []
        removed = 0
        for line in self.lines:
            if line.kind == "entry" and line.key == key:
                removed += 1
                continue
            kept.append(line)
        self.lines = kept
        return removed

    def render(self) -> str:
        rendered: list[str] = []
        for line in self.lines:
            if line.kind == "entry" and line.dirty:
                rendered.append(render_entry(line.key or "", line.value or "", line.export))
            else:
                rendered.append(line.raw)
        text = "\n".join(rendered)
        if text and not text.endswith("\n"):
            text += "\n"
        return text

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()


def line_no_line(raw_line: str) -> str:
    """Normalize line endings without stripping meaningful whitespace."""
    return raw_line.rstrip("\r\n")


def unquote_value(value: str) -> str:
    """Remove one pair of surrounding quotes and unescape double-quoted values."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        inner = value[1:-1]
        if quote == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def render_entry(key: str, value: str, export: bool = False) -> str:
    prefix = "export " if export else ""
    return f"{prefix}{key}={quote_value(value)}"


def quote_value(value: str) -> str:
    """Quote a value only when the line-oriented format needs it."""
    if value == "":
        return ""
    safe = re.fullmatch(r"[A-Za-z0-9_@%+=:,./$~^!*()\[\]{}<>?|&-]+", value)
    if safe and not value.startswith((" ", "\t")) and not value.endswith((" ", "\t")):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def mask_secret(value: str, keep: int = 4) -> str:
    """Mask a secret for display, keeping at most the last ``keep`` characters."""
    if not value:
        return ""
    if len(value) <= keep:
        return "****"
    return "****" + value[-keep:]


def revision_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_revision(path: Path) -> str | None:
    """Return a content hash for optimistic concurrency checks."""
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(payload).hexdigest()
