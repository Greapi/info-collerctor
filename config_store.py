#!/usr/bin/env python3
"""Safe configuration file writes: backups, atomic replacement, conflict checks."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from env_file import EnvDocument, file_revision, revision_of
from feed_config import (
    Feed,
    FeedsDocument,
    safe_name,
    validate_feeds,
)

BACKUP_KEEP = 5


class ConfigStoreError(ValueError):
    """A configuration file could not be written safely."""


class ConflictError(ConfigStoreError):
    """The file changed on disk after the UI loaded it."""


class FeedValidationError(ConfigStoreError):
    def __init__(self, errors: list[dict[str, object]]) -> None:
        self.errors = errors
        message = "；".join(str(item.get("message", "")) for item in errors[:4])
        super().__init__(message or "订阅源校验失败")


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write text to a temporary file in the same directory, then replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    effective_mode = mode if mode is not None else existing_mode
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if effective_mode is not None:
            os.chmod(temp_path, effective_mode)
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def backup_file(path: Path, keep: int = BACKUP_KEEP, mode: int | None = None) -> list[str]:
    """Keep ``path.bak`` plus up to ``keep`` timestamped backups."""
    path = Path(path)
    if not path.exists():
        return []
    payload = path.read_bytes()
    backups: list[Path] = []
    latest = path.with_name(path.name + ".bak")
    _write_bytes(latest, payload, mode=mode)
    backups.append(latest)

    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    timestamped = path.with_name(f"{path.name}.{stamp}.bak")
    _write_bytes(timestamped, payload, mode=mode)
    backups.append(timestamped)

    existing = sorted(
        path.parent.glob(f"{path.name}.*.bak"),
        key=lambda item: item.name,
        reverse=True,
    )
    for old in existing[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return [str(item) for item in backups]


def _write_bytes(path: Path, payload: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def check_revision(path: Path, expected_revision: str | None) -> None:
    current = file_revision(path)
    if current == expected_revision:
        return
    if current is None:
        raise ConflictError("文件已被删除，请刷新页面后重试。")
    if expected_revision is None:
        raise ConflictError("文件在页面加载后被创建，请刷新页面后重试。")
    raise ConflictError("文件已被外部修改，请刷新页面后重新编辑，以免覆盖其他改动。")


def write_env_file(
    path: Path,
    updates: Mapping[str, str],
    expected_revision: str | None,
    keep: int = BACKUP_KEEP,
    mode: int = 0o600,
) -> str:
    path = Path(path)
    check_revision(path, expected_revision)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    document = EnvDocument.parse(text, source=str(path))
    for key, value in updates.items():
        document.set(key, str(value))
    backup_file(path, keep=keep, mode=mode)
    rendered = document.render()
    atomic_write(path, rendered, mode=mode)
    return revision_of(rendered)


def write_feeds_file(
    path: Path,
    feeds: list[Feed],
    expected_revision: str | None,
    keep: int = BACKUP_KEEP,
) -> str:
    path = Path(path)
    errors = validate_feeds(feeds, source=str(path))
    if errors:
        raise FeedValidationError(errors)
    check_revision(path, expected_revision)
    if path.exists():
        document = FeedsDocument.read(path)
    else:
        document = FeedsDocument()
    if document.errors:
        raise ConfigStoreError("现有 feeds.txt 含有无法解析的行，请先修复：" + "；".join(document.errors[:3]))
    rendered = document.render(feeds)
    backup_file(path, keep=keep)
    atomic_write(path, rendered, mode=0o644)
    return revision_of(rendered)


def write_config_file(
    path: Path,
    data: Mapping[str, Any],
    expected_revision: str | None,
    keep: int = BACKUP_KEEP,
) -> str:
    path = Path(path)
    check_revision(path, expected_revision)
    existing: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigStoreError(f"现有 config.json 无法解析，请先修复：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigStoreError("现有 config.json 顶层必须是对象")
        existing = parsed
    merged = _deep_merge(existing, data)
    rendered = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    backup_file(path, keep=keep)
    atomic_write(path, rendered, mode=0o644)
    return revision_of(rendered)


def _deep_merge(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def migrate_feed_rename(
    old_name: str,
    new_name: str,
    mode: str,
    archive_dir: Path,
    db_path: Path,
) -> dict[str, Any]:
    """Apply one of the three rename modes.

    ``display`` only changes feeds.txt, ``migrate`` moves archived files and
    fixes stored paths, ``rekey`` additionally rewrites the dedupe primary key.
    """
    if mode not in {"display", "migrate", "rekey"}:
        raise ConfigStoreError(f"未知的重命名方式：{mode}")
    archive_dir = Path(archive_dir)
    db_path = Path(db_path)
    old_slug = safe_name(old_name)
    new_slug = safe_name(new_name)
    result: dict[str, Any] = {
        "mode": mode,
        "old_slug": old_slug,
        "new_slug": new_slug,
        "moved_files": 0,
        "updated_rows": 0,
        "warnings": [],
    }
    if mode == "display":
        result["warnings"].append(
            "仅修改显示名：历史目录与去重表仍使用旧归档名，下次抓取会把已有条目当作新条目重新归档。"
        )
        return result
    if not old_slug or not new_slug:
        raise ConfigStoreError("重命名前后名称都必须能生成归档目录名")
    if old_slug == new_slug:
        return result

    old_dir = archive_dir / old_slug
    new_dir = archive_dir / new_slug
    will_move = old_dir.exists() and old_dir.is_dir()
    if will_move and new_dir.exists():
        raise ConfigStoreError(
            f"目标归档目录已存在：{new_dir}。请先合并或移除该目录，避免覆盖历史文件。"
        )

    connection: sqlite3.Connection | None = None
    moved = False
    row_count = 0
    try:
        if db_path.exists():
            connection = sqlite3.connect(db_path)
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "entries" in tables:
                if mode == "rekey":
                    cursor = connection.execute(
                        "UPDATE entries SET feed_name = ? WHERE feed_name = ?",
                        (new_slug, old_slug),
                    )
                    row_count += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                rows = connection.execute(
                    "SELECT rowid, file_path FROM entries WHERE feed_name = ?",
                    (new_slug if mode == "rekey" else old_slug,),
                ).fetchall()
                for rowid, file_path in rows:
                    updated = _rewrite_path_component(str(file_path), old_slug, new_slug)
                    if updated != file_path:
                        connection.execute(
                            "UPDATE entries SET file_path = ? WHERE rowid = ?",
                            (updated, rowid),
                        )
                        row_count += 1
        if will_move:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            result["moved_files"] = sum(1 for item in old_dir.rglob("*") if item.is_file())
            old_dir.rename(new_dir)
            moved = True
        if connection is not None:
            connection.commit()
        result["updated_rows"] = row_count
    except BaseException:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        if moved and new_dir.exists() and not old_dir.exists():
            try:
                new_dir.rename(old_dir)
            except OSError:
                pass
        raise
    finally:
        if connection is not None:
            connection.close()

    if mode == "migrate":
        result["warnings"].append(
            "已迁移历史目录并修正文件路径，但去重表主键仍是旧名称；下次抓取会重新归档已有条目。"
            "如需彻底避免重复，请再次选择「同步重写去重表」。"
        )
    return result


def _rewrite_path_component(value: str, old_slug: str, new_slug: str) -> str:
    if not value:
        return value
    path = Path(value)
    parts = list(path.parts)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == old_slug:
            parts[index] = new_slug
            return str(Path(*parts))
    return value


def ensure_private_env_file(path: Path) -> None:
    """Tighten an existing `.env` to 0600 when possible."""
    try:
        current = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return
    if current != 0o600:
        os.chmod(path, 0o600)


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


__all__ = [
    "ConfigStoreError",
    "ConflictError",
    "FeedValidationError",
    "atomic_write",
    "backup_file",
    "check_revision",
    "write_env_file",
    "write_feeds_file",
    "write_config_file",
    "migrate_feed_rename",
    "ensure_private_env_file",
]


def write_feeds_text(
    path: Path,
    text: str,
    expected_revision: str | None,
    keep: int = BACKUP_KEEP,
) -> str:
    """Import a complete feeds.txt document after validating every feed line."""
    path = Path(path)
    document = FeedsDocument.parse(text, source=str(path))
    if document.errors:
        raise ConfigStoreError("导入内容存在无法解析的行：" + "；".join(document.errors[:4]))
    errors = validate_feeds(document.feeds, source=str(path))
    if errors:
        raise FeedValidationError(errors)
    check_revision(path, expected_revision)
    rendered = document.render()
    backup_file(path, keep=keep)
    atomic_write(path, rendered, mode=0o644)
    return revision_of(rendered)
