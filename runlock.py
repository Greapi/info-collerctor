#!/usr/bin/env python3
"""Cross-process run lock shared by the CLI scripts and the configuration UI."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only; the project targets macOS/Linux.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

SKIP_ENV = "INFO_COLLECTOR_LOCK_HELD"
LOCK_FILENAME = ".run.lock"


class LockBusy(RuntimeError):
    def __init__(self, path: Path, info: dict[str, object] | None = None) -> None:
        self.path = path
        self.info = info or {}
        who = self.info.get("label") or self.info.get("mode") or "其他任务"
        started = self.info.get("started_at") or "未知时间"
        pid = self.info.get("pid") or "?"
        super().__init__(f"已有任务在运行（{who}，PID {pid}，开始于 {started}）。请等待结束后重试。")


def lock_path(base_dir: Path) -> Path:
    return Path(base_dir) / "logs" / LOCK_FILENAME


class RunLock:
    """An exclusive, non-blocking advisory lock stored in ``logs/.run.lock``."""

    def __init__(self, path: Path, label: str = "任务", metadata: dict[str, object] | None = None) -> None:
        self.path = Path(path)
        self.label = label
        self.metadata = dict(metadata or {})
        self._handle = None
        self._held = False

    def acquire(self) -> None:
        if os.environ.get(SKIP_ENV) == "1":
            # The configuration UI already owns the lock for this child run.
            return
        if fcntl is None:  # pragma: no cover - Windows fallback
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            info = _read_info(handle)
            handle.close()
            raise LockBusy(self.path, info) from None
        payload = {
            "pid": os.getpid(),
            "label": self.label,
            "started_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            **self.metadata,
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
        self._handle = handle
        self._held = True

    def release(self) -> None:
        if not self._held or self._handle is None:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._held = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    @property
    def held(self) -> bool:
        return self._held


def current_info(path: Path) -> dict[str, object] | None:
    """Best-effort description of a busy lock, used for UI messages."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return _read_info(handle)
    except OSError:
        return None


def _read_info(handle) -> dict[str, object]:
    try:
        handle.seek(0)
        text = handle.read().strip()
        if not text:
            return {}
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
