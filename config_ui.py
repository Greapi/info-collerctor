#!/usr/bin/env python3
"""Local, dependency-free configuration UI for the RSS collector.

Run ``python3 config_ui.py`` in the project directory.  The server only binds
to 127.0.0.1, prints a one-time session token URL and opens the browser.  The
configuration files remain the source of truth; the UI is a validated read/write
layer on top of them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import mimetypes
import os
import plistlib
import re
import secrets
import shutil
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import feed_config
import rss_collector
import settings as settings_module
from config_store import (
    ConfigStoreError,
    ConflictError,
    FeedValidationError,
    atomic_write,
    ensure_private_env_file,
    file_revision,
    migrate_feed_rename,
    write_config_file,
    write_env_file,
    write_feeds_file,
    write_feeds_text,
)
from feed_config import Feed, FeedsDocument, validate_feeds
from runlock import LockBusy, RunLock, current_info, lock_path

HOST = "127.0.0.1"
WEB_DIR = Path(__file__).resolve().parent / "web"
STATE_FILENAME = ".config_ui_state.json"
LOG_SUBDIR = Path("logs") / "ui-runs"
SCHEDULER_LABEL = "com.info-collector.daily"
MAX_BODY = 4 * 1024 * 1024
MAX_LOG_CHUNK = 200_000
VERSION = "1.0.0"
RUN_MODES = {
    "collect": "仅采集",
    "report": "仅生成日报",
    "workflow": "完整工作流",
    "site": "仅构建站点",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        self.status = status
        self.message = message
        self.extra = extra
        super().__init__(message)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds")


def parse_date(value: str) -> str | None:
    if not DATE_RE.fullmatch(value or ""):
        return None
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return None
    return value


def looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def tail(value: str, lines: int = 30) -> str:
    return "\n".join(value.splitlines()[-lines:])


def sanitize(value: str, root: Path) -> str:
    try:
        secrets = settings_module.secret_values(root)
    except Exception:
        secrets = []
    return settings_module.mask_text(value, secrets)


class UiState:
    """Small JSON state file (never contains secrets)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": 1, "legacy_confirmed": False, "feeds": {}, "last_run": None}
        if not isinstance(parsed, dict):
            return {"version": 1, "legacy_confirmed": False, "feeds": {}, "last_run": None}
        parsed.setdefault("version", 1)
        parsed.setdefault("legacy_confirmed", False)
        parsed.setdefault("feeds", {})
        parsed.setdefault("last_run", None)
        return parsed

    def save(self) -> None:
        try:
            atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            pass

    def update(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            callback(self.data)
            self.save()

    def record_feed_result(self, url: str, result: dict[str, Any]) -> None:
        key = feed_config.url_key(url)

        def mutate(data: dict[str, Any]) -> None:
            feeds = data.setdefault("feeds", {})
            feeds[key] = result
            # Keep the state file small and bounded.
            if len(feeds) > 200:
                ordered = sorted(feeds.items(), key=lambda item: item[1].get("at", ""), reverse=True)
                data["feeds"] = dict(ordered[:200])

        self.update(mutate)


def _missing_requirement(model: settings_module.ModelConfig, mail: settings_module.MailConfig) -> dict[str, Any]:
    modes: dict[str, Any] = {}
    model_reason = f"缺少模型配置：{', '.join(model.missing)}" if model.missing else ""
    mail_reason = f"缺少邮件配置：{', '.join(mail.missing)}" if mail.missing else ""
    modes["collect"] = {"enabled": True, "reason": ""}
    modes["site"] = {"enabled": True, "reason": ""}
    modes["report"] = {"enabled": not model.missing, "reason": model_reason}
    reasons = [reason for reason in (model_reason, mail_reason) if reason]
    modes["workflow"] = {
        "enabled": not model.missing,
        "reason": "；".join(reasons),
        "send_enabled": not (model.missing or mail.missing),
        "send_reason": "；".join(reasons),
    }
    return modes


@dataclass
class RunRecord:
    id: str
    mode: str
    status: str
    started_at: str
    finished_at: str | None = None
    log_path: Path | None = None
    summary_path: Path | None = None
    returncode: int | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    lock: RunLock | None = None
    log_text: str = ""
    stop_requested: bool = False
    _log_handle: Any = None


class RunManager:
    def __init__(self, context: "UiContext") -> None:
        self.context = context
        self._lock = threading.Lock()
        self.current: RunRecord | None = None
        self.recent: dict[str, RunRecord] = {}

    def start(self, mode: str, options: dict[str, Any]) -> RunRecord:
        if mode not in RUN_MODES:
            raise ApiError(400, f"未知的运行类型：{mode}")
        root = self.context.root
        settings_obj = self.context.load_settings()
        model = settings_module.load_model_config(base_dir=root, settings_obj=settings_obj)
        mail = settings_module.load_mail_config(base_dir=root, settings_obj=settings_obj)
        if mode in {"report", "workflow"} and model.missing:
            raise ApiError(400, f"缺少模型配置：{', '.join(model.missing)}。请先在「集成配置」中补全。")
        if mode == "workflow" and bool(options.get("send", True)) and mail.missing:
            raise ApiError(400, f"缺少邮件配置：{', '.join(mail.missing)}。请先在「集成配置」中补全，或关闭「发送邮件」。")
        if mode == "report" and not options.get("fetch", True):
            pass
        date = str(options.get("date") or "").strip()
        if date and parse_date(date) is None:
            raise ApiError(400, "日期必须使用 YYYY-MM-DD 格式")

        with self._lock:
            if self.current and self.current.status == "running":
                raise ApiError(409, f"已有任务在运行（{RUN_MODES.get(self.current.mode, self.current.mode)}）。请等待结束或先停止。")
            run_id = self._new_id(mode)
            log_dir = root / LOG_SUBDIR
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{run_id}.log"
            summary_path = log_dir / f"{run_id}.json"
            command = self._build_command(mode, options, settings_obj, summary_path)
            lock = RunLock(lock_path(root), label=RUN_MODES[mode], metadata={"mode": mode, "source": "config_ui"})
            try:
                lock.acquire()
            except LockBusy as exc:
                raise ApiError(409, str(exc)) from exc
            record = RunRecord(
                id=run_id,
                mode=mode,
                status="running",
                started_at=now_iso(),
                log_path=log_path,
                summary_path=summary_path,
                options={
                    "date": date,
                    "fetch": bool(options.get("fetch", True)),
                    "send": bool(options.get("send", True)),
                },
                lock=lock,
            )
            try:
                handle = open(log_path, "w", encoding="utf-8", buffering=1)
                record._log_handle = handle
                header = (
                    f"# {RUN_MODES[mode]} · {record.started_at}\n"
                    f"# 项目目录：{root}\n"
                    f"# 命令：{' '.join(command)}\n\n"
                )
                handle.write(header)
                record.log_text = header
                env = dict(os.environ)
                env["INFO_COLLECTOR_LOCK_HELD"] = "1"
                env["PYTHONUNBUFFERED"] = "1"
                record.proc = subprocess.Popen(
                    command,
                    cwd=str(root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            except OSError as exc:
                lock.release()
                if record._log_handle is not None:
                    record._log_handle.close()
                raise ApiError(500, f"无法启动任务：{exc}") from exc
            self.current = record
            self.recent[run_id] = record
            while len(self.recent) > 20:
                oldest = next(iter(self.recent))
                if self.recent[oldest].status == "running":
                    break
                self.recent.pop(oldest, None)
            thread = threading.Thread(target=self._reader, args=(record,), name=f"run-{run_id}", daemon=True)
            thread.start()
            return record

    def _new_id(self, mode: str) -> str:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{mode}-{secrets.token_hex(3)}"

    def _build_command(
        self,
        mode: str,
        options: dict[str, Any],
        settings_obj: settings_module.Settings,
        summary_path: Path,
    ) -> list[str]:
        python = sys.executable
        root = self.context.root
        archive = settings_obj.resolve_path("paths.archive_dir")
        reports = settings_obj.resolve_path("paths.report_dir")
        site = settings_obj.resolve_path("paths.site_dir")
        db_path = settings_obj.resolve_path("paths.db_path")
        timeout = str(settings_obj.get("run.timeout"))
        limit = str(settings_obj.get("run.fetch_limit"))
        feeds = str(settings_obj.feeds_path)
        date = str(options.get("date") or "").strip()
        if mode == "collect":
            return [
                python,
                str(root / "rss_collector.py"),
                "--config", feeds,
                "--output", str(archive),
                "--db", str(db_path),
                "--timeout", timeout,
                "--limit", limit,
                "--summary-json", str(summary_path),
            ]
        if mode == "report":
            command = [
                python,
                str(root / "daily_report.py"),
                "--input", str(archive),
                "--output", str(reports),
                "--config", feeds,
                "--db", str(db_path),
                "--timeout", timeout,
                "--fetch-limit", limit,
                "--summary-json", str(summary_path),
            ]
            if date:
                command.extend(["--date", date])
            if not options.get("fetch", True):
                command.append("--no-fetch")
            return command
        if mode == "workflow":
            command = [
                python,
                str(root / "daily_workflow.py"),
                "--input", str(archive),
                "--output", str(reports),
                "--config", feeds,
                "--db", str(db_path),
                "--timeout", timeout,
                "--fetch-limit", limit,
                "--summary-json", str(summary_path),
            ]
            if date:
                command.extend(["--date", date])
            if not options.get("fetch", True):
                command.append("--no-fetch")
            if not options.get("send", True):
                command.append("--no-send")
            return command
        return [
            python,
            str(root / "build_site.py"),
            "--reports", str(reports),
            "--site", str(site),
            "--summary-json", str(summary_path),
        ]

    def _reader(self, record: RunRecord) -> None:
        secrets = settings_module.secret_values(self.context.root)
        proc = record.proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                line = settings_module.mask_text(line, secrets)
                record.log_text += line
                if len(record.log_text) > 4_000_000:
                    record.log_text = record.log_text[-2_000_000:]
                if record._log_handle is not None:
                    try:
                        record._log_handle.write(line)
                    except OSError:
                        pass
            returncode = proc.wait()
        finally:
            if record._log_handle is not None:
                try:
                    record._log_handle.close()
                except OSError:
                    pass
        record.returncode = returncode
        record.finished_at = now_iso()
        summary = self._read_summary(record)
        if summary is None:
            summary = self._fallback_summary(record, record.log_text, returncode)
            self._write_summary(record, summary)
        record.summary = summary
        if record.stop_requested:
            record.status = "stopped"
        elif returncode == 0 and summary.get("ok", True):
            record.status = "done"
        else:
            record.status = "failed"
            record.error = str(summary.get("error") or f"进程退出码 {returncode}")
        if record.lock is not None:
            record.lock.release()
            record.lock = None
        self._record_feed_results(record)
        self.context.state.update(lambda data: data.__setitem__("last_run", self._record_summary(record)))
        with self._lock:
            if self.current is record:
                self.current = record

    def _read_summary(self, record: RunRecord) -> dict[str, Any] | None:
        if record.summary_path is None or not record.summary_path.exists():
            return None
        try:
            data = json.loads(record.summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_summary(self, record: RunRecord, summary: dict[str, Any]) -> None:
        if record.summary_path is None:
            return
        try:
            atomic_write(record.summary_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            pass

    def _fallback_summary(self, record: RunRecord, output: str, returncode: int) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "ok": returncode == 0,
            "mode": record.mode,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "returncode": returncode,
        }
        if returncode != 0:
            summary["error"] = tail(output, 8).strip() or f"进程退出码 {returncode}"
        if record.mode == "collect":
            match = re.search(r"Done\. Saved (\d+) new item", output)
            if match:
                summary["collect"] = {"saved": int(match.group(1)), "failed_feeds": self._parse_failures(output)}
        elif record.mode in {"report", "workflow"}:
            match = re.search(r"Wrote (.+?)\s*$", output, flags=re.M)
            if match:
                summary["report_path"] = match.group(1).strip()
            failures = self._parse_feed_failures(output)
            if failures:
                summary["collect"] = {"failed_feeds": failures}
            if "Email sent." in output:
                summary["mail"] = {"status": "sent"}
            elif record.options.get("send") is False:
                summary["mail"] = {"status": "skipped"}
        elif record.mode == "site":
            match = re.search(r"Built (\d+) report page", output)
            if match:
                summary["reports"] = int(match.group(1))
        return summary

    def _parse_failures(self, output: str) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        current: str | None = None
        for line in output.splitlines():
            match = re.match(r"Fetching (.+?): (https?://\S+)", line)
            if match:
                current = match.group(1)
                continue
            match = re.match(r"\s+failed: (.+)", line)
            if match and current:
                failures.append({"name": current, "error": match.group(1)})
        return failures

    def _parse_feed_failures(self, output: str) -> list[dict[str, str]]:
        return self._parse_failures(output)

    def _record_feed_results(self, record: RunRecord) -> None:
        if record.mode not in {"collect", "report", "workflow"}:
            return
        if record.mode in {"report", "workflow"} and not record.options.get("fetch", True):
            return
        failures = {}
        summary = record.summary or {}
        collect = summary.get("collect") if isinstance(summary, dict) else None
        if isinstance(collect, dict):
            for failure in collect.get("failed_feeds") or []:
                if isinstance(failure, dict) and failure.get("url"):
                    failures[failure["url"]] = str(failure.get("error") or "失败")
        if not failures:
            for failure in self._parse_failures(record.log_text):
                failures[failure.get("name", "")] = failure.get("error", "失败")
        enabled = []
        try:
            enabled = feed_config.enabled_feeds(self.context.load_settings().feeds_path)
        except (OSError, ValueError):
            enabled = []
        for feed in enabled:
            if feed.url in failures:
                self.context.state.record_feed_result(
                    feed.url,
                    {"ok": False, "at": record.finished_at or now_iso(), "error": failures[feed.url], "source": "run"},
                )
            elif record.status == "done":
                self.context.state.record_feed_result(
                    feed.url,
                    {"ok": True, "at": record.finished_at or now_iso(), "error": "", "source": "run"},
                )

    def _record_summary(self, record: RunRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "mode": record.mode,
            "mode_label": RUN_MODES.get(record.mode, record.mode),
            "status": record.status,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "summary": record.summary,
        }

    def _find(self, run_id: str) -> RunRecord:
        record = self.recent.get(run_id)
        if record is not None:
            return record
        log_dir = self.context.root / LOG_SUBDIR
        log_path = log_dir / f"{run_id}.log"
        if not log_path.exists():
            raise ApiError(404, "找不到这次运行记录")
        summary = None
        summary_path = log_dir / f"{run_id}.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                summary = None
        status = "unknown"
        if isinstance(summary, dict):
            status = "done" if summary.get("ok", True) else "failed"
        parts = run_id.split("-")
        started = ""
        if len(parts) >= 2:
            try:
                started = dt.datetime.strptime(parts[0] + "-" + parts[1], "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
            except ValueError:
                started = ""
        return RunRecord(
            id=run_id,
            mode=parts[2] if len(parts) > 2 else "unknown",
            status=status,
            started_at=started,
            log_path=log_path,
            summary=summary,
        )

    def log_chunk(self, run_id: str, offset: int) -> dict[str, Any]:
        record = self._find(run_id)
        text = ""
        if record.log_path is not None and record.log_path.exists():
            try:
                full_text = record.log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                full_text = ""
            text = full_text[max(0, int(offset)) : max(0, int(offset)) + MAX_LOG_CHUNK]
        done = record.status != "running"
        return {
            "id": run_id,
            "text": text,
            "offset": max(0, int(offset)) + len(text),
            "status": record.status,
            "done": done,
            "summary": record.summary,
            "error": record.error,
        }

    def status(self, run_id: str) -> dict[str, Any]:
        record = self._find(run_id)
        return {
            "id": record.id,
            "mode": record.mode,
            "mode_label": RUN_MODES.get(record.mode, record.mode),
            "status": record.status,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "returncode": record.returncode,
            "summary": record.summary,
            "error": record.error,
            "options": record.options,
        }

    def stop(self, run_id: str) -> dict[str, Any]:
        record = self._find(run_id)
        if record.status != "running" or record.proc is None:
            raise ApiError(400, "任务已经结束，无需停止")
        record.stop_requested = True
        try:
            record.proc.terminate()
        except OSError:
            pass
        try:
            record.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                record.proc.kill()
            except OSError:
                pass
        return {"id": run_id, "status": "stopping"}

    def history(self) -> list[dict[str, Any]]:
        log_dir = self.context.root / LOG_SUBDIR
        entries: list[dict[str, Any]] = []
        if log_dir.exists():
            for log_path in sorted(log_dir.glob("*.log"), reverse=True)[:20]:
                run_id = log_path.stem
                summary = None
                summary_path = log_path.with_suffix(".json")
                if summary_path.exists():
                    try:
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        summary = None
                parts = run_id.split("-")
                status = "unknown"
                ok = summary.get("ok") if isinstance(summary, dict) else None
                if ok is True:
                    status = "done"
                elif ok is False:
                    status = "failed"
                entries.append(
                    {
                        "id": run_id,
                        "mode": parts[2] if len(parts) > 2 else "unknown",
                        "mode_label": RUN_MODES.get(parts[2] if len(parts) > 2 else "", "未知"),
                        "status": status,
                        "started_at": summary.get("started_at") if isinstance(summary, dict) else "",
                        "summary": summary,
                    }
                )
        current = self.current
        if current is not None and current.status == "running":
            replaced = False
            for index, item in enumerate(entries):
                if item["id"] == current.id:
                    entries[index] = self._record_summary(current)
                    replaced = True
                    break
            if not replaced:
                entries.insert(0, self._record_summary(current))
        return entries[:20]


class UiContext:
    def __init__(self, root: Path, token: str, port: int) -> None:
        self.root = root.resolve()
        self.token = token
        self.port = port
        self.state = UiState(self.root / STATE_FILENAME)
        self.runs = RunManager(self)
        # Test-fetch results are kept in memory only: the probe must not write
        # feed archives, the dedupe database, or any configuration file.
        self._test_results: dict[str, dict[str, Any]] = {}
        for env_path in (self.root / ".env", self.root / ".env.rsshub"):
            try:
                ensure_private_env_file(env_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ basics
    def load_settings(self) -> settings_module.Settings:
        return settings_module.load_settings(self.root)

    def load_model(self, settings_obj: settings_module.Settings | None = None) -> settings_module.ModelConfig:
        return settings_module.load_model_config(base_dir=self.root, settings_obj=settings_obj)

    def load_mail(self, settings_obj: settings_module.Settings | None = None) -> settings_module.MailConfig:
        return settings_module.load_mail_config(base_dir=self.root, settings_obj=settings_obj)

    def load_rsshub(self) -> settings_module.RsshubConfig:
        return settings_module.load_rsshub_config(self.root)

    def revisions(self, settings_obj: settings_module.Settings | None = None) -> dict[str, str | None]:
        s = settings_obj or self.load_settings()
        return {
            "feeds": file_revision(s.feeds_path),
            "env": file_revision(s.env_path),
            "rsshub_env": file_revision(s.rsshub_env_path),
            "config": file_revision(s.config_path),
        }

    def legacy_payload(self) -> dict[str, Any]:
        s = self.load_settings()
        detected = not s.config_path.exists() and any(
            path.exists() for path in (s.feeds_path, s.env_path, s.rsshub_env_path)
        )
        confirmed = bool(self.state.data.get("legacy_confirmed"))
        feeds: list[dict[str, Any]] = []
        feed_errors: list[str] = []
        if s.feeds_path.exists():
            try:
                document = FeedsDocument.read(s.feeds_path)
                feeds = [
                    {"name": feed.name, "url": feed.url, "enabled": feed.enabled}
                    for feed in document.feeds[:50]
                ]
                feed_errors = list(document.errors)
            except (OSError, ValueError) as exc:
                feed_errors.append(str(exc))
        model = self.load_model(s)
        mail = self.load_mail(s)
        rsshub = self.load_rsshub()
        return {
            "detected": detected,
            "confirmed": confirmed,
            "preview": {
                "feeds": feeds,
                "feed_errors": feed_errors,
                "feeds_count": len(feeds),
                "model": model.display(),
                "mail": mail.display(),
                "rsshub": rsshub.display(),
                "config_exists": s.config_path.exists(),
                "files": {
                    "feeds": s.feeds_path.exists(),
                    "env": s.env_path.exists(),
                    "rsshub_env": s.rsshub_env_path.exists(),
                    "config": s.config_path.exists(),
                },
            },
        }

    def ensure_legacy_confirmed(self) -> None:
        legacy = self.legacy_payload()
        if legacy["detected"] and not legacy["confirmed"]:
            raise ApiError(
                409,
                "首次启动需要先确认旧布局配置预览，确认后才会写入任何配置文件。",
                code="legacy_unconfirmed",
                legacy=legacy,
            )

    def confirm_legacy(self) -> dict[str, Any]:
        self.state.update(lambda data: data.__setitem__("legacy_confirmed", True))
        return {"ok": True, "legacy": self.legacy_payload()}

    # ----------------------------------------------------------------- overview
    def bootstrap(self) -> dict[str, Any]:
        s = self.load_settings()
        model = self.load_model(s)
        mail = self.load_mail(s)
        rsshub = self.load_rsshub()
        feeds = self.feeds_payload(s)
        db = self._db_stats(s.resolve_path("paths.db_path"))
        scheduler = self.scheduler_payload()
        last_run = self.state.data.get("last_run")
        if not last_run:
            history = self.runs.history()
            if history:
                last_run = history[0]
        scripts_present = {
            "rss_collector": (self.root / "rss_collector.py").exists(),
            "daily_report": (self.root / "daily_report.py").exists(),
            "daily_workflow": (self.root / "daily_workflow.py").exists(),
            "build_site": (self.root / "build_site.py").exists(),
        }
        return {
            "ok": True,
            "version": VERSION,
            "project_root": str(self.root),
            "host": HOST,
            "port": self.port,
            "settings": settings_module.summarize_effective(s),
            "model": model.display(),
            "mail": mail.display(),
            "rsshub": rsshub.display(),
            "feeds": feeds,
            "data": db,
            "scheduler": scheduler,
            "legacy": self.legacy_payload(),
            "requirements": {
                "model": {"ready": model.ready, "missing": model.missing},
                "mail": {"ready": mail.ready, "missing": mail.missing},
                "modes": _missing_requirement(model, mail),
            },
            "revisions": self.revisions(s),
            "last_run": last_run,
            "default_report_date": s.default_report_date(),
            "scripts_present": scripts_present,
            "platform": sys.platform,
            "site_preview_url": "/preview/",
            "crontab": self._crontab(s),
        }

    # -------------------------------------------------------------------- feeds
    def feeds_payload(self, settings_obj: settings_module.Settings | None = None) -> dict[str, Any]:
        s = settings_obj or self.load_settings()
        document = FeedsDocument.read(s.feeds_path) if s.feeds_path.exists() else FeedsDocument()
        db = self._db_stats(s.resolve_path("paths.db_path"))
        by_feed = {item["feed_name"]: item for item in db.get("by_feed", [])}
        rsshub = self.load_rsshub()
        archive_root = s.resolve_path("paths.archive_dir")
        results = self.state.data.get("feeds", {}) if isinstance(self.state.data.get("feeds"), dict) else {}
        if self._test_results:
            results = {**results, **self._test_results}
        feeds = []
        for feed in document.feeds:
            stat = by_feed.get(feed.archive_name, {})
            archive_exists = (archive_root / feed.archive_name).is_dir()
            result = results.get(feed.url_key) if isinstance(results, dict) else None
            last_result = result
            if not last_result and stat.get("last_saved_at"):
                last_result = {
                    "ok": True,
                    "at": stat.get("last_saved_at"),
                    "error": "",
                    "source": "archive",
                }
            feeds.append(
                {
                    "name": feed.name,
                    "url": feed.url,
                    "enabled": feed.enabled,
                    "note": feed.note,
                    "line_no": feed.line_no,
                    "archive_name": feed.archive_name,
                    "archived": int(stat.get("count", 0)),
                    "archive_exists": archive_exists,
                    "last_saved_at": stat.get("last_saved_at"),
                    "last_result": last_result,
                    "source_type": feed_config.source_type(feed.url, rsshub.host, rsshub.port),
                    "valid_url": not bool(feed_config.validate_url(feed.url)),
                }
            )
        validation = validate_feeds(document.feeds, source=str(s.feeds_path)) if document.feeds else []
        return {
            "ok": True,
            "feeds": feeds,
            "revision": file_revision(s.feeds_path),
            "errors": list(document.errors),
            "validation": validation,
            "disabled_archive_names": [feed.archive_name for feed in document.feeds if not feed.enabled],
            "total_archived": int(db.get("total", 0)),
            "total_feeds": len(feeds),
            "enabled_feeds": sum(1 for feed in feeds if feed["enabled"]),
            "path": str(s.feeds_path),
        }

    def _feeds_from_body(self, payload: Any) -> list[Feed]:
        if not isinstance(payload, list):
            raise ApiError(400, "feeds 必须是数组")
        if len(payload) > 500:
            raise ApiError(400, "订阅源数量不能超过 500")
        feeds: list[Feed] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ApiError(400, f"第 {index + 1} 行订阅源格式不正确")
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            note = str(item.get("note") or "").strip()
            enabled = bool(item.get("enabled", True))
            if len(name) > 120:
                raise ApiError(400, f"第 {index + 1} 行名称过长（最多 120 字符）")
            if len(url) > 2000:
                raise ApiError(400, f"第 {index + 1} 行 URL 过长")
            if len(note) > 200:
                raise ApiError(400, f"第 {index + 1} 行备注过长（最多 200 字符）")
            feeds.append(Feed(name=name, url=url, enabled=enabled, note=note))
        return feeds

    def save_feeds(self, body: dict[str, Any]) -> dict[str, Any]:
        self.ensure_legacy_confirmed()
        s = self.load_settings()
        feeds = self._feeds_from_body(body.get("feeds"))
        errors = validate_feeds(feeds, source=str(s.feeds_path))
        if errors:
            raise ApiError(400, "订阅源校验失败，请修正高亮行后再保存。", errors=errors)
        old_doc = FeedsDocument.read(s.feeds_path) if s.feeds_path.exists() else FeedsDocument()
        old_by_name = {feed.name: feed for feed in old_doc.feeds}
        old_by_url = {feed.url_key: feed for feed in old_doc.feeds}
        merged: list[Feed] = []
        for feed in feeds:
            old = old_by_name.get(feed.name) or old_by_url.get(feed.url_key)
            extra = old.extra if old is not None else ()
            merged.append(
                Feed(
                    name=feed.name,
                    url=feed.url,
                    enabled=feed.enabled,
                    note=feed.note,
                    extra=extra,
                )
            )
        rename = body.get("rename")
        rename_result: dict[str, Any] | None = None
        old_text = s.feeds_path.read_text(encoding="utf-8") if s.feeds_path.exists() else ""
        if rename is not None:
            rename_result = self._preflight_rename(rename, old_doc, merged, s)
        revision = write_feeds_file(s.feeds_path, merged, body.get("revision"))
        if rename is not None and rename_result is not None:
            try:
                migration = migrate_feed_rename(
                    rename_result["old_name"],
                    rename_result["new_name"],
                    rename_result["mode"],
                    s.resolve_path("paths.archive_dir"),
                    s.resolve_path("paths.db_path"),
                )
            except (ConfigStoreError, OSError, ValueError) as exc:
                # feeds.txt was already replaced; roll it back so file and history agree.
                try:
                    atomic_write(s.feeds_path, old_text, mode=0o644)
                except OSError:
                    pass
                raise ApiError(500, f"重命名迁移失败，feeds.txt 已回滚：{exc}") from exc
            rename_result.update(migration)
        payload = self.feeds_payload()
        payload["rename"] = rename_result
        payload["saved_revision"] = revision
        return payload

    def _preflight_rename(
        self,
        rename: Any,
        old_doc: FeedsDocument,
        new_feeds: list[Feed],
        settings_obj: settings_module.Settings,
    ) -> dict[str, Any]:
        if not isinstance(rename, dict):
            raise ApiError(400, "rename 参数格式不正确")
        old_name = str(rename.get("old_name") or "").strip()
        new_name = str(rename.get("new_name") or "").strip()
        mode = str(rename.get("mode") or "rekey")
        if mode not in {"display", "migrate", "rekey"}:
            raise ApiError(400, "重命名方式必须是 display / migrate / rekey")
        if not old_name or not new_name:
            raise ApiError(400, "重命名前后名称不能为空")
        if old_name == new_name:
            raise ApiError(400, "重命名前后名称相同")
        old_feed = next((feed for feed in old_doc.feeds if feed.name == old_name), None)
        if old_feed is None:
            raise ApiError(400, f"feeds.txt 中找不到原名称：{old_name}")
        if not any(feed.name == new_name for feed in new_feeds):
            raise ApiError(400, "新名称未出现在保存的订阅源列表中")
        if mode != "display" and old_feed.archive_name != feed_config.safe_name(new_name):
            old_dir = settings_obj.resolve_path("paths.archive_dir") / old_feed.archive_name
            new_dir = settings_obj.resolve_path("paths.archive_dir") / feed_config.safe_name(new_name)
            if old_dir.exists() and new_dir.exists():
                raise ApiError(409, f"目标归档目录已存在：{new_dir}，请先合并或移除。")
            db_path = settings_obj.resolve_path("paths.db_path")
            if db_path.exists() and not os.access(db_path, os.R_OK | os.W_OK):
                raise ApiError(409, f"去重数据库不可读写：{db_path}")
        return {
            "old_name": old_name,
            "new_name": new_name,
            "mode": mode,
            "warnings": [],
        }

    def validate_feeds_body(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            feeds = self._feeds_from_body(body.get("feeds"))
            errors = validate_feeds(feeds, source="feeds.txt")
        except ApiError as exc:
            return {"ok": False, "errors": [{"index": 0, "field": "feeds", "message": exc.message}], "warnings": []}
        warnings = []
        disabled = sum(1 for feed in feeds if not feed.enabled)
        if disabled:
            warnings.append(f"保存后将有 {disabled} 个订阅源在采集与日报环节被跳过。")
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    def import_feeds(self, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("text")
        if not isinstance(text, str):
            raise ApiError(400, "text 必须是字符串")
        if len(text) > 2_000_000:
            raise ApiError(400, "导入内容过大")
        try:
            document = FeedsDocument.parse(text, source="导入内容")
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        errors = validate_feeds(document.feeds, source="导入内容") if document.feeds else []
        if document.errors or errors:
            return {
                "ok": False,
                "errors": errors,
                "document_errors": list(document.errors),
                "preview": [
                    {"name": feed.name, "url": feed.url, "enabled": feed.enabled, "note": feed.note}
                    for feed in document.feeds
                ],
            }
        if body.get("dry_run", True):
            return {
                "ok": True,
                "dry_run": True,
                "feeds": [
                    {"name": feed.name, "url": feed.url, "enabled": feed.enabled, "note": feed.note}
                    for feed in document.feeds
                ],
                "revision": body.get("revision"),
                "warnings": [],
            }
        self.ensure_legacy_confirmed()
        s = self.load_settings()
        revision = write_feeds_text(s.feeds_path, text, body.get("revision"))
        payload = self.feeds_payload()
        payload["saved_revision"] = revision
        return payload

    def export_feeds(self) -> dict[str, Any]:
        s = self.load_settings()
        document = FeedsDocument.read(s.feeds_path) if s.feeds_path.exists() else FeedsDocument()
        return {"ok": True, "text": document.render(), "revision": file_revision(s.feeds_path)}

    def test_feed(self, body: dict[str, Any]) -> dict[str, Any]:
        url = str(body.get("url") or "").strip()
        name = str(body.get("name") or "").strip()
        error = feed_config.validate_url(url)
        if error:
            raise ApiError(400, error)
        try:
            timeout = int(body.get("timeout") or 20)
        except ValueError:
            timeout = 20
        timeout = max(3, min(60, timeout))
        started = time.monotonic()
        try:
            payload = rss_collector.fetch_url(url, timeout=timeout, retries=1)
            if len(payload) > 20_000_000:
                raise ValueError("响应超过 20MB，疑似不是 RSS/Atom")
            feed = Feed(name=name or (urllib.parse.urlsplit(url).hostname or "test"), url=url)
            entries = rss_collector.parse_feed(feed, payload)
            elapsed = int((time.monotonic() - started) * 1000)
            items = [
                {"title": entry.title, "link": entry.link, "published": entry.published}
                for entry in entries[:5]
            ]
            result = {
                "ok": True,
                "url": url,
                "count": len(entries),
                "items": items,
                "elapsed_ms": elapsed,
                "error": "",
                "at": now_iso(),
            }
            self._test_results[feed_config.url_key(url)] = {
                "ok": True,
                "at": result["at"],
                "count": len(entries),
                "source": "test",
                "error": "",
            }
            return result
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            reason = rss_collector.describe_fetch_error(exc)
            self._test_results[feed_config.url_key(url)] = {
                "ok": False,
                "at": now_iso(),
                "error": reason,
                "source": "test",
            }
            return {
                "ok": False,
                "url": url,
                "count": 0,
                "items": [],
                "elapsed_ms": elapsed,
                "error": reason,
                "error_type": type(exc).__name__,
                "at": now_iso(),
            }

    # -------------------------------------------------------------- integrations
    def integrations_payload(self) -> dict[str, Any]:
        s = self.load_settings()
        model = self.load_model(s)
        mail = self.load_mail(s)
        rsshub = self.load_rsshub()
        return {
            "ok": True,
            "model": model.display(),
            "mail": mail.display(),
            "rsshub": rsshub.display(),
            "revisions": self.revisions(s),
            "env_exists": s.env_path.exists(),
            "rsshub_env_exists": s.rsshub_env_path.exists(),
            "presets": [
                {
                    "id": "deepseek",
                    "label": "DeepSeek",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                },
                {
                    "id": "openai",
                    "label": "OpenAI",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                },
                {
                    "id": "custom",
                    "label": "自定义（任意 OpenAI 兼容接口）",
                    "base_url": "",
                    "model": "",
                },
            ],
            "settings": settings_module.summarize_effective(s),
        }

    def save_integrations(self, body: dict[str, Any]) -> dict[str, Any]:
        self.ensure_legacy_confirmed()
        s = self.load_settings()
        current_model = self.load_model(s)
        current_mail = self.load_mail(s)
        updates: dict[str, str] = {}
        warnings: list[str] = []
        errors: list[dict[str, str]] = []

        if "model" in body:
            section = body.get("model")
            if not isinstance(section, dict):
                raise ApiError(400, "model 必须是对象")
            base_url = str(section.get("base_url", current_model.base_url) or "").strip().rstrip("/")
            model_name = str(section.get("model", current_model.model) or "").strip()
            url_error = self._validate_http_url(base_url)
            if url_error:
                errors.append({"field": "model.base_url", "message": url_error})
            if not model_name:
                errors.append({"field": "model.model", "message": "模型名不能为空"})
            if len(model_name) > 200:
                errors.append({"field": "model.model", "message": "模型名过长"})
            api_key = self._secret_update(section.get("api_key"), current_model.api_key)
            updates["OPENAI_BASE_URL"] = base_url
            updates["OPENAI_MODEL"] = model_name
            if api_key is not None:
                updates["OPENAI_API_KEY"] = api_key

        if "mail" in body:
            section = body.get("mail")
            if not isinstance(section, dict):
                raise ApiError(400, "mail 必须是对象")
            values = self._mail_from_section(section, current_mail)
            errors.extend(values["errors"])
            warnings.extend(values["warnings"])
            updates.update(values["updates"])

        if errors:
            raise ApiError(400, "集成配置校验失败，请修正后重试。", errors=errors)
        if not updates:
            raise ApiError(400, "没有需要保存的改动")
        try:
            revision = write_env_file(
                s.env_path,
                updates,
                body.get("env_revision"),
                mode=0o600,
            )
            ensure_private_env_file(s.env_path)
        except ConflictError as exc:
            raise ApiError(409, str(exc)) from exc
        except (ConfigStoreError, OSError) as exc:
            raise ApiError(500, f"保存 .env 失败：{exc}") from exc
        payload = self.integrations_payload()
        payload["saved_revision"] = revision
        payload["warnings"] = warnings
        return payload

    def _mail_from_section(
        self,
        section: dict[str, Any],
        current: settings_module.MailConfig,
    ) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        warnings: list[str] = []

        def text_value(key: str, fallback: str) -> str:
            if key in section and section[key] is not None:
                return str(section[key]).strip()
            return fallback

        host = text_value("host", current.host)
        username = text_value("username", current.username)
        sender = text_value("sender", current.sender) or username
        recipients_value = section.get("recipients", current.recipients)
        if isinstance(recipients_value, list):
            recipients_text = ", ".join(str(item).strip() for item in recipients_value)
        else:
            recipients_text = str(recipients_value or "")
        recipients = settings_module.split_recipients(recipients_text)
        subject_prefix = text_value("subject_prefix", current.subject_prefix) or settings_module.DEFAULT_SUBJECT_PREFIX
        password_update = self._secret_update(section.get("password"), current.password)
        effective_password = password_update if password_update is not None else current.password
        try:
            port = int(section.get("port", current.port) or 0)
        except (TypeError, ValueError):
            port = 0
        use_ssl = bool(section.get("use_ssl", current.use_ssl))
        use_starttls = bool(section.get("use_starttls", current.use_starttls))

        if not 1 <= port <= 65535:
            errors.append({"field": "mail.port", "message": "SMTP 端口必须在 1-65535 之间"})
        if host and any(char.isspace() for char in host):
            errors.append({"field": "mail.host", "message": "SMTP 主机不能包含空格"})
        if host and not sender:
            errors.append({"field": "mail.sender", "message": "填写 SMTP 主机时必须填写发件人"})
        if sender and not self._valid_sender(sender):
            errors.append({"field": "mail.sender", "message": f"发件人邮箱格式不正确：{sender}"})
        if host and not recipients:
            errors.append({"field": "mail.recipients", "message": "填写 SMTP 主机时必须至少填写一个收件人"})
        for recipient in recipients:
            if not looks_like_email(recipient):
                errors.append({"field": "mail.recipients", "message": f"收件人邮箱格式不正确：{recipient}"})
        if use_ssl and use_starttls:
            warnings.append("SSL 与 STARTTLS 同时开启：多数服务器只支持其中一种，请确认端口与服务端配置。")
        if port == 587 and use_ssl:
            warnings.append("端口 587 通常使用 STARTTLS 而不是 SSL。")
        if port == 465 and use_starttls:
            warnings.append("端口 465 通常使用 SSL 而不是 STARTTLS。")
        updates = {
            "SMTP_HOST": host,
            "SMTP_PORT": str(port),
            "SMTP_USERNAME": username,
            "SMTP_USE_SSL": "true" if use_ssl else "false",
            "SMTP_USE_STARTTLS": "true" if use_starttls else "false",
            "MAIL_FROM": sender,
            "MAIL_TO": ", ".join(recipients),
            "MAIL_SUBJECT_PREFIX": subject_prefix,
        }
        if password_update is not None:
            updates["SMTP_PASSWORD"] = password_update
        return {"updates": updates, "password": effective_password, "errors": errors, "warnings": warnings}

    def _secret_update(self, value: Any, current: str) -> str | None:
        """Return the new secret, or None when the value should be kept."""
        if value is None:
            return None
        text = str(value)
        if text == "":
            return None
        if text.startswith("****"):
            return None
        return text

    @staticmethod
    def _valid_sender(value: str) -> bool:
        match = re.fullmatch(r".*<([^<>]+)>", value.strip())
        email = match.group(1).strip() if match else value.strip()
        return looks_like_email(email)

    @staticmethod
    def _validate_http_url(value: str) -> str:
        if not value:
            return "Base URL 不能为空"
        if any(char.isspace() for char in value):
            return "Base URL 不能包含空格"
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError as exc:
            return f"Base URL 无法解析：{exc}"
        if parsed.scheme.lower() not in {"http", "https"}:
            return "Base URL 必须以 http:// 或 https:// 开头"
        if not parsed.netloc:
            return "Base URL 缺少主机名"
        return ""

    def test_model(self, body: dict[str, Any]) -> dict[str, Any]:
        s = self.load_settings()
        current = self.load_model(s)
        base_url = str(body.get("base_url") or current.base_url).strip().rstrip("/")
        model_name = str(body.get("model") or current.model).strip()
        key = str(body.get("api_key") or "").strip()
        if not key or key.startswith("****"):
            key = current.api_key
        error = self._validate_http_url(base_url)
        if error:
            raise ApiError(400, error)
        if not model_name:
            raise ApiError(400, "请先填写模型名")
        if not key:
            raise ApiError(400, "请先填写 API Key")
        try:
            timeout = max(5, min(120, int(body.get("timeout") or 30)))
        except (TypeError, ValueError):
            timeout = 30
        endpoint = base_url + ("" if base_url.endswith("/chat/completions") else "/chat/completions")
        payload = json.dumps(
            {
                "model": model_name,
                "messages": [{"role": "user", "content": "请回复 pong"}],
                "temperature": 0,
                "max_tokens": 16,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "info-collector-config-ui/1.0",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read(200_000).decode("utf-8", errors="replace")
            latency = int((time.monotonic() - started) * 1000)
            try:
                data = json.loads(raw)
                reply = str(data["choices"][0]["message"]["content"]).strip()
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                reply = ""
                return {
                    "ok": False,
                    "status": status,
                    "latency_ms": latency,
                    "error": "接口返回了 HTTP 200，但不是预期的 chat completions 结构",
                    "raw": sanitize(raw[:800], self.root),
                    "hint": "请确认 Base URL 指向 OpenAI 兼容的 /v1 接口。",
                }
            return {
                "ok": True,
                "status": status,
                "latency_ms": latency,
                "reply": reply[:500],
                "model": model_name,
                "endpoint": endpoint,
                "error": "",
            }
        except urllib.error.HTTPError as exc:
            latency = int((time.monotonic() - started) * 1000)
            try:
                raw = exc.read().decode("utf-8", errors="replace")[:1000]
            except OSError:
                raw = ""
            hint = {
                401: "API Key 无效或没有权限。",
                403: "请求被拒绝，请检查 API Key 权限或服务端白名单。",
                404: "接口路径不存在，请检查 Base URL（通常以 /v1 结尾）。",
                429: "请求被限流，请稍后重试或检查账户额度。",
            }.get(exc.code, "请检查 Base URL、模型名和账户状态。")
            return {
                "ok": False,
                "status": exc.code,
                "latency_ms": latency,
                "error": f"HTTP {exc.code} {exc.reason}",
                "raw": sanitize(raw, self.root),
                "hint": hint,
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            latency = int((time.monotonic() - started) * 1000)
            reason = getattr(exc, "reason", exc)
            return {
                "ok": False,
                "status": None,
                "latency_ms": latency,
                "error": f"网络错误：{reason}",
                "hint": "检查 Base URL、网络连接或代理设置。",
            }

    def test_mail(self, body: dict[str, Any]) -> dict[str, Any]:
        s = self.load_settings()
        current = self.load_mail(s)
        section = {}
        for key in ("host", "port", "username", "password", "sender", "recipients", "use_ssl", "use_starttls", "subject_prefix"):
            if key in body:
                section[key] = body[key]
        values = self._mail_from_section(section, current)
        if values["errors"]:
            raise ApiError(400, "邮件配置不完整，无法测试。", errors=values["errors"])
        updates = values["updates"]
        mail = settings_module.MailConfig(
            host=updates["SMTP_HOST"],
            port=int(updates["SMTP_PORT"]),
            username=updates["SMTP_USERNAME"],
            password=values["password"],
            sender=updates["MAIL_FROM"],
            recipients=settings_module.split_recipients(updates["MAIL_TO"]),
            use_ssl=updates["SMTP_USE_SSL"] == "true",
            use_starttls=updates["SMTP_USE_STARTTLS"] == "true",
            subject_prefix=updates["MAIL_SUBJECT_PREFIX"],
        )
        if mail.missing:
            raise ApiError(400, f"缺少邮件配置：{', '.join(mail.missing)}")
        try:
            timeout = max(5, min(60, int(body.get("timeout") or 15)))
        except (TypeError, ValueError):
            timeout = 15
        started = time.monotonic()
        stage = "connect"
        smtp = None
        try:
            context = ssl.create_default_context()
            if mail.use_ssl:
                smtp = smtplib.SMTP_SSL(mail.host, mail.port, timeout=timeout, context=context)
            else:
                smtp = smtplib.SMTP(mail.host, mail.port, timeout=timeout)
                smtp.ehlo_or_helo_if_needed()
            stage = "starttls"
            if mail.use_starttls and not mail.use_ssl:
                smtp.starttls(context=context)
                smtp.ehlo()
            stage = "login"
            if mail.username or mail.password:
                smtp.login(mail.username, mail.password)
            stage = "send"
            message = EmailMessage()
            message["Subject"] = f"{mail.subject_prefix} 测试邮件 {dt.datetime.now():%Y-%m-%d %H:%M}"
            message["From"] = mail.sender
            message["To"] = ", ".join(mail.recipients)
            message.set_content(
                "这是一封来自 info-collector 配置界面的测试邮件。\n\n"
                "收到它说明 SMTP 主机、端口、加密方式、账号和收件人都可用。\n"
            )
            smtp.send_message(message)
            stage = "done"
            smtp.quit()
            smtp = None
            latency = int((time.monotonic() - started) * 1000)
            return {
                "ok": True,
                "stage": stage,
                "stage_label": "发送成功",
                "latency_ms": latency,
                "recipients": mail.recipients,
                "warnings": values["warnings"],
                "error": "",
            }
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            latency = int((time.monotonic() - started) * 1000)
            labels = {
                "connect": "连接 / SSL 握手失败",
                "starttls": "STARTTLS 失败",
                "login": "SMTP 认证失败",
                "send": "发送邮件失败",
                "done": "发送后断开连接失败",
            }
            hint = {
                "connect": "检查 SMTP 主机、端口、网络，以及端口与 SSL 选项是否匹配。",
                "starttls": "检查服务器是否支持 STARTTLS，或改用 SSL 端口 465。",
                "login": "检查用户名与授权码；多数邮箱使用「授权码」而不是登录密码。",
                "send": "检查发件人地址是否属于该 SMTP 账户、收件人是否被拒。",
            }.get(stage, "请检查 SMTP 配置。")
            return {
                "ok": False,
                "stage": stage,
                "stage_label": labels.get(stage, stage),
                "latency_ms": latency,
                "error": sanitize(str(exc), self.root),
                "hint": hint,
                "warnings": values["warnings"],
            }
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except Exception:
                    pass

    def _smtp_login_probe(self, timeout: int = 8) -> dict[str, Any]:
        current = self.load_mail()
        if current.missing:
            return {"ok": False, "error": f"缺少邮件配置：{', '.join(current.missing)}"}
        stage = "connect"
        smtp = None
        try:
            context = ssl.create_default_context()
            if current.use_ssl:
                smtp = smtplib.SMTP_SSL(current.host, current.port, timeout=timeout, context=context)
            else:
                smtp = smtplib.SMTP(current.host, current.port, timeout=timeout)
            stage = "starttls"
            if current.use_starttls and not current.use_ssl:
                smtp.starttls(context=context)
            stage = "login"
            if current.username or current.password:
                smtp.login(current.username, current.password)
            smtp.quit()
            smtp = None
            return {"ok": True, "error": ""}
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            return {"ok": False, "error": f"{stage}: {sanitize(str(exc), self.root)}"}
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ settings
    def settings_payload(self) -> dict[str, Any]:
        s = self.load_settings()
        return {
            "ok": True,
            "settings": settings_module.summarize_effective(s),
            "revisions": self.revisions(s),
            "default_report_date": s.default_report_date(),
            "platform": sys.platform,
            "crontab": self._crontab(s),
            "lock": current_info(lock_path(self.root)),
        }

    def save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        self.ensure_legacy_confirmed()
        s = self.load_settings()
        errors: list[dict[str, str]] = []
        config_data: dict[str, Any] = {}

        paths = body.get("paths")
        if paths is not None:
            if not isinstance(paths, dict):
                raise ApiError(400, "paths 必须是对象")
            allowed = {"archive_dir", "report_dir", "db_path", "site_dir"}
            clean: dict[str, str] = {}
            for key in allowed:
                if key not in paths:
                    continue
                value = str(paths[key] or "").strip()
                if not value:
                    errors.append({"field": f"paths.{key}", "message": "路径不能为空"})
                    continue
                clean[key] = value
                resolved = settings_module.resolve_path(value, self.root)
                status = settings_module.path_status(resolved, is_dir=(key != "db_path"))
                if not status["writable"]:
                    errors.append(
                        {
                            "field": f"paths.{key}",
                            "message": f"目录不可写：{resolved}（{status['reason']}）",
                        }
                    )
                if status["exists"] and key != "db_path" and not status["is_dir"]:
                    errors.append({"field": f"paths.{key}", "message": f"不是目录：{resolved}"})
            config_data["paths"] = clean

        run = body.get("run")
        if run is not None:
            if not isinstance(run, dict):
                raise ApiError(400, "run 必须是对象")
            clean_run: dict[str, int] = {}
            for key, low, high, label in (
                ("timeout", 1, 600, "网络超时"),
                ("fetch_limit", 0, 10000, "每源抓取条数"),
                ("date_offset", 0, 7, "日报日期偏移"),
            ):
                if key not in run:
                    continue
                try:
                    value = int(run[key])
                except (TypeError, ValueError):
                    errors.append({"field": f"run.{key}", "message": f"{label}必须是整数"})
                    continue
                if not low <= value <= high:
                    errors.append({"field": f"run.{key}", "message": f"{label}必须在 {low}-{high} 之间"})
                    continue
                clean_run[key] = value
            config_data["run"] = clean_run

        advanced = body.get("advanced")
        if advanced is not None:
            if not isinstance(advanced, dict):
                raise ApiError(400, "advanced 必须是对象")
            clean_model: dict[str, Any] = {}
            try:
                if "temperature" in advanced:
                    temperature = float(advanced["temperature"])
                    if not 0 <= temperature <= 2:
                        raise ValueError("temperature 必须在 0-2 之间")
                    clean_model["temperature"] = temperature
                for key, low, high, label in (
                    ("max_chars_per_article", 500, 200_000, "单篇截断字符数"),
                    ("max_chars_per_batch", 1000, 1_000_000, "单批字符上限"),
                ):
                    if key in advanced:
                        value = int(advanced[key])
                        if not low <= value <= high:
                            raise ValueError(f"{label}必须在 {low}-{high} 之间")
                        clean_model[key] = value
                if (
                    "max_chars_per_article" in clean_model
                    and "max_chars_per_batch" in clean_model
                    and clean_model["max_chars_per_article"] > clean_model["max_chars_per_batch"]
                ):
                    errors.append(
                        {
                            "field": "advanced.max_chars_per_article",
                            "message": "单篇截断字符数不能大于单批字符上限",
                        }
                    )
            except (TypeError, ValueError) as exc:
                errors.append({"field": "advanced", "message": str(exc)})
            config_data["model"] = clean_model

        schedule = body.get("schedule")
        if schedule is not None:
            if not isinstance(schedule, dict):
                raise ApiError(400, "schedule 必须是对象")
            clean_schedule: dict[str, int] = {}
            for key, low, high in (("hour", 0, 23), ("minute", 0, 59)):
                if key not in schedule:
                    continue
                try:
                    value = int(schedule[key])
                except (TypeError, ValueError):
                    errors.append({"field": f"schedule.{key}", "message": "必须是整数"})
                    continue
                if not low <= value <= high:
                    errors.append({"field": f"schedule.{key}", "message": f"必须在 {low}-{high} 之间"})
                    continue
                clean_schedule[key] = value
            config_data["schedule"] = clean_schedule

        if errors:
            raise ApiError(400, "配置校验失败，请修正后重试。", errors=errors)
        if not config_data:
            raise ApiError(400, "没有需要保存的改动")
        try:
            revision = write_config_file(s.config_path, config_data, body.get("config_revision"))
        except ConflictError as exc:
            raise ApiError(409, str(exc)) from exc
        except (ConfigStoreError, OSError) as exc:
            raise ApiError(500, f"保存 config.json 失败：{exc}") from exc
        payload = self.settings_payload()
        payload["saved_revision"] = revision
        return payload

    # -------------------------------------------------------------------- health
    def health(self, connectivity: bool = False) -> dict[str, Any]:
        s = self.load_settings()
        model = self.load_model(s)
        mail = self.load_mail(s)
        rsshub = self.load_rsshub()
        checks: list[dict[str, Any]] = []

        def add(identifier: str, label: str, status: str, detail: str, fix: str = "") -> None:
            checks.append({"id": identifier, "label": label, "status": status, "detail": detail, "fix": fix})

        if s.config_path.exists():
            add("config", "config.json", "ok", f"已读取：{s.config_path}")
        else:
            add(
                "config",
                "config.json",
                "warn",
                "尚未生成 config.json，当前使用默认值；保存任意配置后会创建。",
                "在「系统」页保存路径与运行参数。",
            )
        for error in s.errors:
            add("config_error", "配置解析", "error", error, "修正 config.json 或环境变量后刷新。")

        if not s.feeds_path.exists():
            add("feeds", "订阅源文件", "error", f"找不到 {s.feeds_path}", "创建 feeds.txt 并添加至少一个源。")
        else:
            try:
                document = FeedsDocument.read(s.feeds_path)
                validation = validate_feeds(document.feeds, source=str(s.feeds_path))
                duplicate_or_invalid = [item for item in validation]
                if document.errors:
                    add("feeds_parse", "订阅源格式", "error", "；".join(document.errors[:3]), "按「名称 | URL」修正 feeds.txt。")
                elif duplicate_or_invalid:
                    add(
                        "feeds_validate",
                        "订阅源校验",
                        "error",
                        "；".join(str(item["message"]) for item in duplicate_or_invalid[:3]),
                        "修正重复 URL 或非法地址。",
                    )
                else:
                    enabled = sum(1 for feed in document.feeds if feed.enabled)
                    add("feeds", "订阅源", "ok", f"{len(document.feeds)} 个源，启用 {enabled} 个。")
                if not document.feeds:
                    add("feeds_empty", "订阅源数量", "warn", "feeds.txt 中没有有效订阅源。", "在「订阅源」页添加。")
            except (OSError, ValueError) as exc:
                add("feeds_read", "订阅源读取", "error", str(exc), "检查 feeds.txt 编码与权限。")

        if model.ready:
            add("model", "模型配置", "ok", f"{model.model} @ {model.base_url}")
        else:
            add(
                "model",
                "模型配置",
                "error",
                f"缺少 {', '.join(model.missing)}",
                "在「集成配置」中填写 Base URL、API Key 和模型名。",
            )
        if mail.ready:
            add("mail", "邮件配置", "ok", f"{mail.host}:{mail.port}，收件人 {len(mail.recipients)} 个")
            for warning in mail.warnings:
                add("mail_warning", "邮件加密", "warn", warning, "确认端口与 SSL/STARTTLS 组合。")
        else:
            add(
                "mail",
                "邮件配置",
                "error",
                f"缺少 {', '.join(mail.missing)}",
                "在「集成配置」中补全 SMTP 与收发件人。",
            )

        for key, is_dir, label in (
            ("paths.archive_dir", True, "归档目录"),
            ("paths.report_dir", True, "日报目录"),
            ("paths.site_dir", True, "站点目录"),
            ("paths.db_path", False, "去重数据库"),
        ):
            path = s.resolve_path(key)
            status = settings_module.path_status(path, is_dir=is_dir)
            if not status["writable"]:
                add(f"path_{key}", label, "error", f"{path}：{status['reason']}", "修改路径或修正权限。")
            elif not status["exists"]:
                add(f"path_{key}", label, "warn", f"{path} 尚不存在（{status['reason']}）。", "首次运行时会创建。")
            else:
                add(f"path_{key}", label, "ok", str(path))

        db_stats = self._db_stats(s.resolve_path("paths.db_path"))
        if db_stats.get("error"):
            add("db", "去重数据库", "error", db_stats["error"], "检查数据库路径与文件权限。")
        elif db_stats.get("exists"):
            add("db", "去重数据库", "ok", f"{db_stats['total']} 条记录，{db_stats['size']} 字节")
        else:
            add("db", "去重数据库", "warn", "数据库尚未创建。", "首次采集后会创建。")

        scheduler = self.scheduler_payload()
        if scheduler.get("path_valid") is False:
            add("scheduler_path", "定时任务路径", "error", "已安装的 plist 指向不存在的路径。", "在「系统」页重新安装定时任务。")
        elif scheduler.get("loaded"):
            add("scheduler", "定时任务", "ok", f"已加载，下次执行：{scheduler.get('next_run') or '未知'}")
        elif scheduler.get("installed"):
            add("scheduler", "定时任务", "warn", "已安装但未加载。", "在「系统」页启用，或检查 launchctl。")
        else:
            add("scheduler", "定时任务", "warn", "尚未安装定时任务。", "在「系统」页安装，或按 crontab 示例配置。")

        if (self.root / "docker-compose.rsshub.yml").exists():
            add("compose", "RSSHub 编排", "ok", "找到 docker-compose.rsshub.yml")
        else:
            add("compose", "RSSHub 编排", "warn", "缺少 docker-compose.rsshub.yml", "从仓库恢复该文件，或忽略 RSSHub。")

        if connectivity:
            try:
                probe = self.test_model({})
                add(
                    "model_connectivity",
                    "模型连通性",
                    "ok" if probe.get("ok") else "error",
                    (
                        f"HTTP {probe.get('status')} · {probe.get('latency_ms')}ms"
                        if probe.get("ok")
                        else f"{probe.get('error')} {probe.get('hint', '')}".strip()
                    ),
                    "在「集成配置」中点击「测试连接」查看详情。",
                )
            except ApiError as exc:
                add("model_connectivity", "模型连通性", "error", exc.message, "先补全模型配置。")
            probe = self._smtp_login_probe()
            add(
                "mail_connectivity",
                "SMTP 连通性",
                "ok" if probe.get("ok") else "error",
                probe.get("error") or "连接与登录成功",
                "在「集成配置」中点击「发送测试邮件」查看阶段错误。",
            )
            probe = self._rsshub_health(rsshub)
            add(
                "rsshub_connectivity",
                "RSSHub 健康检查",
                "ok" if probe.get("ok") else "warn",
                (
                    f"{rsshub.host}:{rsshub.port} 延迟 {probe.get('latency_ms')}ms"
                    if probe.get("ok")
                    else f"{probe.get('error')}（若未启用 RSSHub 可忽略）"
                ),
                "确认 RSSHub 容器已启动、端口正确。",
            )

        errors = sum(1 for check in checks if check["status"] == "error")
        warnings = sum(1 for check in checks if check["status"] == "warn")
        return {
            "ok": errors == 0,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
            "connectivity": connectivity,
        }

    # ----------------------------------------------------------------- scheduler
    def _launchctl(self, *arguments: str) -> tuple[int, str]:
        try:
            result = subprocess.run(
                ["launchctl", *arguments],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output.strip()

    def _launch_agent_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{SCHEDULER_LABEL}.plist"

    def _python_path(self) -> str:
        candidate = shutil.which("python3") or sys.executable
        return str(Path(candidate).resolve())

    def _crontab(self, settings_obj: settings_module.Settings | None = None) -> str:
        s = settings_obj or self.load_settings()
        hour = int(s.get("schedule.hour"))
        minute = int(s.get("schedule.minute"))
        python = self._python_path()
        return (
            f"{minute} {hour} * * * cd {self.root} && {python} daily_workflow.py "
            f">> logs/daily_workflow.log 2>&1"
        )

    def scheduler_payload(self) -> dict[str, Any]:
        s = self.load_settings()
        plist_path = self._launch_agent_path()
        is_macos = sys.platform == "darwin"
        installed = is_macos and plist_path.exists()
        loaded = False
        launch_output = ""
        plist_data: dict[str, Any] = {}
        plist_error = ""
        if installed:
            try:
                plist_data = plistlib.loads(plist_path.read_bytes())
            except (OSError, plistlib.InvalidFileException) as exc:
                plist_error = str(exc)
            if is_macos and not plist_error:
                rc, launch_output = self._launchctl("print", f"gui/{os.getuid()}/{SCHEDULER_LABEL}")
                loaded = rc == 0
        schedule = {
            "hour": int(s.get("schedule.hour")),
            "minute": int(s.get("schedule.minute")),
        }
        if isinstance(plist_data.get("StartCalendarInterval"), dict):
            interval = plist_data["StartCalendarInterval"]
            schedule["hour"] = int(interval.get("Hour", schedule["hour"]))
            schedule["minute"] = int(interval.get("Minute", schedule["minute"]))
        program = ""
        arguments = plist_data.get("ProgramArguments")
        if isinstance(arguments, list) and arguments:
            program = str(arguments[-1])
        working_dir = str(plist_data.get("WorkingDirectory") or "")
        now = dt.datetime.now()
        next_run = ""
        if installed and loaded:
            candidate = now.replace(hour=schedule["hour"], minute=schedule["minute"], second=0, microsecond=0)
            if candidate <= now:
                candidate += dt.timedelta(days=1)
            next_run = candidate.strftime("%Y-%m-%d %H:%M")
        log_paths = [self.root / "logs" / "launchd.err.log", self.root / "logs" / "launchd.out.log"]
        existing_logs = [path for path in log_paths if path.exists()]
        last_log = ""
        last_log_path = ""
        if existing_logs:
            newest = max(existing_logs, key=lambda path: path.stat().st_mtime)
            last_log_path = str(newest)
            try:
                last_log = tail(newest.read_text(encoding="utf-8", errors="replace"), 20)
            except OSError as exc:
                last_log = f"读取日志失败：{exc}"
        return {
            "ok": True,
            "platform": sys.platform,
            "is_macos": is_macos,
            "label": SCHEDULER_LABEL,
            "plist_path": str(plist_path),
            "installed": installed,
            "loaded": loaded,
            "path_valid": (
                (not installed)
                or (
                    bool(program)
                    and Path(program).exists()
                    and bool(working_dir)
                    and Path(working_dir).exists()
                )
            ),
            "program": program,
            "working_directory": working_dir,
            "schedule": schedule,
            "next_run": next_run,
            "last_log": last_log,
            "last_log_path": last_log_path,
            "launchctl": sanitize(launch_output, self.root),
            "plist_error": plist_error,
            "crontab": self._crontab(s),
            "python": self._python_path(),
        }

    def scheduler_plist(self, hour: int, minute: int) -> str:
        """Render the launchd plist without touching ~/Library/LaunchAgents."""
        logs = self.root / "logs"
        data = {
            "Label": SCHEDULER_LABEL,
            "ProgramArguments": [self._python_path(), str(self.root / "daily_workflow.py")],
            "WorkingDirectory": str(self.root),
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "StandardOutPath": str(logs / "launchd.out.log"),
            "StandardErrorPath": str(logs / "launchd.err.log"),
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "PYTHONUNBUFFERED": "1",
            },
            "ThrottleInterval": 60,
        }
        return plistlib.dumps(data).decode("utf-8")

    def scheduler_action(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "")
        if action not in {"install", "update", "uninstall", "disable", "enable"}:
            raise ApiError(400, "未知的定时任务操作")
        if not body.get("confirm"):
            raise ApiError(400, "该操作会修改 launchd 配置，请先确认。")
        s = self.load_settings()
        if sys.platform != "darwin":
            return {
                "ok": True,
                "skipped": True,
                "message": "当前不是 macOS，不会写入系统定时配置。请使用下面的 crontab 示例手动安装。",
                "crontab": self._crontab(s),
                "scheduler": self.scheduler_payload(),
            }
        try:
            hour = int(body.get("hour", s.get("schedule.hour")))
            minute = int(body.get("minute", s.get("schedule.minute")))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "执行时间必须是整数") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ApiError(400, "执行时间不合法")
        plist_path = self._launch_agent_path()
        script = self.root / "daily_workflow.py"
        if action in {"install", "update"} and not script.exists():
            raise ApiError(400, f"找不到 {script}，无法安装定时任务")
        output_parts: list[str] = []
        if action in {"install", "update"}:
            (self.root / "logs").mkdir(parents=True, exist_ok=True)
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(plist_path, self.scheduler_plist(hour, minute), mode=0o644)
            rc, out = self._launchctl("bootout", f"gui/{os.getuid()}/{SCHEDULER_LABEL}")
            output_parts.append(f"bootout({rc}): {out}")
            rc, out = self._launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
            output_parts.append(f"bootstrap({rc}): {out}")
            if rc != 0:
                rc, out = self._launchctl("load", "-w", str(plist_path))
                output_parts.append(f"load({rc}): {out}")
            if rc != 0:
                raise ApiError(500, "launchctl 加载失败：" + sanitize("\n".join(output_parts), self.root))
        elif action == "uninstall":
            rc, out = self._launchctl("bootout", f"gui/{os.getuid()}/{SCHEDULER_LABEL}")
            output_parts.append(f"bootout({rc}): {out}")
            try:
                plist_path.unlink()
            except FileNotFoundError:
                pass
        elif action == "disable":
            rc, out = self._launchctl("bootout", f"gui/{os.getuid()}/{SCHEDULER_LABEL}")
            output_parts.append(f"bootout({rc}): {out}")
        elif action == "enable":
            if not plist_path.exists():
                raise ApiError(400, "尚未安装定时任务，请先安装")
            rc, out = self._launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
            output_parts.append(f"bootstrap({rc}): {out}")
            if rc != 0:
                raise ApiError(500, "launchctl 加载失败：" + sanitize("\n".join(output_parts), self.root))
        scheduler = self.scheduler_payload()
        scheduler["action"] = action
        scheduler["launchctl_output"] = sanitize("\n".join(output_parts), self.root)
        return scheduler

    # ------------------------------------------------------------------- rsshub
    def _rsshub_health(self, config: settings_module.RsshubConfig | None = None) -> dict[str, Any]:
        config = config or self.load_rsshub()
        host = config.host or "127.0.0.1"
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        url = f"http://{host}:{config.port}/healthz"
        started = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                body = response.read(200).decode("utf-8", errors="replace")
                latency = int((time.monotonic() - started) * 1000)
                return {"ok": True, "url": url, "status": getattr(response, "status", 200), "latency_ms": latency, "body": body}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            latency = int((time.monotonic() - started) * 1000)
            return {"ok": False, "url": url, "latency_ms": latency, "error": str(exc)}

    def _docker_compose_command(self, *arguments: str) -> list[str]:
        s = self.load_settings()
        return [
            "docker",
            "compose",
            "--env-file",
            str(s.rsshub_env_path),
            "-f",
            str(self.root / "docker-compose.rsshub.yml"),
            *arguments,
        ]

    def rsshub_payload(self) -> dict[str, Any]:
        s = self.load_settings()
        config = self.load_rsshub()
        docker_available = shutil.which("docker") is not None
        compose_available = (self.root / "docker-compose.rsshub.yml").exists()
        containers = ""
        containers_ok = False
        containers_error = ""
        if docker_available and compose_available:
            try:
                result = subprocess.run(
                    self._docker_compose_command("ps"),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd=str(self.root),
                )
                containers = tail(sanitize((result.stdout or "") + (result.stderr or ""), self.root), 20)
                containers_ok = result.returncode == 0
                if not containers_ok:
                    containers_error = containers
            except (OSError, subprocess.TimeoutExpired) as exc:
                containers_error = str(exc)
        return {
            "ok": True,
            "config": config.display(),
            "revision": file_revision(s.rsshub_env_path),
            "env_exists": s.rsshub_env_path.exists(),
            "compose_available": compose_available,
            "docker_available": docker_available,
            "health": self._rsshub_health(config),
            "containers": containers,
            "containers_ok": containers_ok,
            "containers_error": sanitize(containers_error, self.root),
            "crontab": "",
        }

    def save_rsshub(self, body: dict[str, Any]) -> dict[str, Any]:
        self.ensure_legacy_confirmed()
        s = self.load_settings()
        current = self.load_rsshub()
        errors: list[dict[str, str]] = []

        def text_value(key: str, fallback: str) -> str:
            return str(body[key]).strip() if key in body and body[key] is not None else fallback

        host = text_value("host", current.host)
        try:
            port = int(body.get("port", current.port))
        except (TypeError, ValueError):
            port = 0
        image = text_value("image", current.image)
        browserless_image = text_value("browserless_image", current.browserless_image)
        redis_image = text_value("redis_image", current.redis_image)
        proxy = text_value("proxy", current.proxy)
        token = self._secret_update(body.get("token"), current.token)
        if not host:
            errors.append({"field": "rsshub.host", "message": "主机不能为空"})
        elif any(char.isspace() for char in host) or "://" in host:
            errors.append({"field": "rsshub.host", "message": "主机只填写地址，不要带协议或空格"})
        if not 1 <= port <= 65535:
            errors.append({"field": "rsshub.port", "message": "端口必须在 1-65535 之间"})
        for key, value in (("image", image), ("browserless_image", browserless_image), ("redis_image", redis_image)):
            if not value:
                errors.append({"field": f"rsshub.{key}", "message": "镜像名不能为空"})
            elif any(char.isspace() for char in value):
                errors.append({"field": f"rsshub.{key}", "message": "镜像名不能包含空格"})
        if proxy:
            proxy_error = self._validate_http_url(proxy)
            if proxy_error:
                errors.append({"field": "rsshub.proxy", "message": "代理地址必须是 http:// 或 https:// URL"})
        if errors:
            raise ApiError(400, "RSSHub 配置校验失败。", errors=errors)
        updates = {
            "RSSHUB_HOST": host,
            "RSSHUB_PORT": str(port),
            "RSSHUB_IMAGE": image,
            "BROWSERLESS_IMAGE": browserless_image,
            "REDIS_IMAGE": redis_image,
            "RSSHUB_PROXY": proxy,
        }
        if token is not None:
            updates["GITHUB_ACCESS_TOKEN"] = token
        try:
            revision = write_env_file(s.rsshub_env_path, updates, body.get("revision"), mode=0o600)
        except ConflictError as exc:
            raise ApiError(409, str(exc)) from exc
        except (ConfigStoreError, OSError) as exc:
            raise ApiError(500, f"保存 .env.rsshub 失败：{exc}") from exc
        payload = self.rsshub_payload()
        payload["saved_revision"] = revision
        return payload

    def rsshub_action(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "")
        if action not in {"up", "down", "restart"}:
            raise ApiError(400, "未知的 RSSHub 操作")
        if not body.get("confirm"):
            raise ApiError(400, "该操作会启停 Docker 容器，请先确认。")
        if shutil.which("docker") is None:
            raise ApiError(400, "未找到 docker 命令，无法管理 RSSHub 容器")
        if not (self.root / "docker-compose.rsshub.yml").exists():
            raise ApiError(400, "缺少 docker-compose.rsshub.yml")
        arguments = {
            "up": ["up", "-d"],
            "down": ["down"],
            "restart": ["restart"],
        }[action]
        try:
            result = subprocess.run(
                self._docker_compose_command(*arguments),
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(self.root),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(500, f"执行 docker compose 失败：{exc}") from exc
        output = tail(sanitize((result.stdout or "") + (result.stderr or ""), self.root), 60)
        if result.returncode != 0:
            return {"ok": False, "action": action, "returncode": result.returncode, "output": output}
        return {"ok": True, "action": action, "returncode": result.returncode, "output": output}

    # --------------------------------------------------------------------- data
    def _db_stats(self, db_path: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": str(db_path),
            "exists": db_path.exists(),
            "size": 0,
            "total": 0,
            "by_feed": [],
            "last_saved_at": "",
            "error": "",
        }
        if not db_path.exists():
            return result
        try:
            result["size"] = db_path.stat().st_size
            uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            try:
                tables = {
                    row[0]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "entries" not in tables:
                    return result
                result["total"] = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
                rows = connection.execute(
                    "SELECT feed_name, COUNT(*), MAX(saved_at) FROM entries GROUP BY feed_name ORDER BY COUNT(*) DESC"
                ).fetchall()
                result["by_feed"] = [
                    {"feed_name": row[0], "count": int(row[1]), "last_saved_at": row[2] or ""}
                    for row in rows
                ]
                latest = connection.execute("SELECT MAX(saved_at) FROM entries").fetchone()
                result["last_saved_at"] = latest[0] if latest and latest[0] else ""
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as exc:
            result["error"] = f"无法读取去重数据库：{exc}"
        return result

    def data_payload(self) -> dict[str, Any]:
        s = self.load_settings()
        db = self._db_stats(s.resolve_path("paths.db_path"))
        human_names = {}
        if s.feeds_path.exists():
            try:
                human_names = {
                    feed.archive_name: feed.name for feed in FeedsDocument.read(s.feeds_path).feeds
                }
            except (OSError, ValueError):
                human_names = {}
        for item in db["by_feed"]:
            item["display_name"] = human_names.get(item["feed_name"], item["feed_name"])
        archive = self.archive_overview()
        return {
            "ok": True,
            "db": db,
            "archive": archive,
            "archive_dir": str(s.resolve_path("paths.archive_dir")),
        }

    def reset_feed(self, body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("confirm"):
            raise ApiError(400, "重置去重记录前需要二次确认")
        feed_name = str(body.get("feed") or "").strip()
        if not feed_name:
            raise ApiError(400, "缺少 feed 参数")
        s = self.load_settings()
        db_path = s.resolve_path("paths.db_path")
        if not db_path.exists():
            raise ApiError(400, "去重数据库不存在")
        try:
            connection = sqlite3.connect(db_path)
            try:
                with connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM entries WHERE feed_name = ?", (feed_name,)
                    ).fetchone()[0]
                    connection.execute("DELETE FROM entries WHERE feed_name = ?", (feed_name,))
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ApiError(500, f"重置去重记录失败：{exc}") from exc
        if s.feeds_path.exists():
            try:
                for feed in FeedsDocument.read(s.feeds_path).feeds:
                    if feed.archive_name == feed_name:
                        self.state.update(lambda data: data.get("feeds", {}).pop(feed.url_key, None))
                        break
            except (OSError, ValueError):
                pass
        payload = self.data_payload()
        payload["reset"] = {"feed": feed_name, "deleted_rows": int(count)}
        return payload

    def archive_overview(self) -> dict[str, Any]:
        s = self.load_settings()
        archive_root = s.resolve_path("paths.archive_dir")
        dates: dict[str, dict[str, Any]] = {}
        feeds: dict[str, dict[str, Any]] = {}
        human_names: dict[str, str] = {}
        if s.feeds_path.exists():
            try:
                human_names = {feed.archive_name: feed.name for feed in FeedsDocument.read(s.feeds_path).feeds}
            except (OSError, ValueError):
                human_names = {}
        if not archive_root.exists():
            return {"dir": str(archive_root), "dates": [], "feeds": [], "total_files": 0, "exists": False}
        try:
            for feed_dir in sorted(archive_root.iterdir()):
                if not feed_dir.is_dir():
                    continue
                feed_count = 0
                for date_dir in sorted(feed_dir.iterdir(), reverse=True):
                    if not date_dir.is_dir() or not DATE_RE.fullmatch(date_dir.name):
                        continue
                    count = sum(1 for item in date_dir.glob("*.md") if item.is_file())
                    if not count:
                        continue
                    feed_count += count
                    entry = dates.setdefault(date_dir.name, {"date": date_dir.name, "count": 0, "feeds": set()})
                    entry["count"] += count
                    entry["feeds"].add(human_names.get(feed_dir.name, feed_dir.name))
                if feed_count:
                    feeds[feed_dir.name] = {
                        "archive_name": feed_dir.name,
                        "display_name": human_names.get(feed_dir.name, feed_dir.name),
                        "count": feed_count,
                    }
        except OSError as exc:
            return {"dir": str(archive_root), "dates": [], "feeds": [], "total_files": 0, "exists": True, "error": str(exc)}
        date_list = [
            {"date": item["date"], "count": item["count"], "feeds": sorted(item["feeds"])}
            for item in sorted(dates.values(), key=lambda item: item["date"], reverse=True)
        ]
        return {
            "dir": str(archive_root),
            "exists": True,
            "dates": date_list,
            "feeds": sorted(feeds.values(), key=lambda item: item["display_name"]),
            "total_files": sum(item["count"] for item in date_list),
        }

    def archive_files(self, date: str) -> dict[str, Any]:
        if parse_date(date) is None:
            raise ApiError(400, "日期必须使用 YYYY-MM-DD 格式")
        s = self.load_settings()
        archive_root = s.resolve_path("paths.archive_dir")
        human_names: dict[str, str] = {}
        if s.feeds_path.exists():
            try:
                human_names = {feed.archive_name: feed.name for feed in FeedsDocument.read(s.feeds_path).feeds}
            except (OSError, ValueError):
                human_names = {}
        files = []
        if archive_root.exists():
            for feed_dir in sorted(archive_root.iterdir()):
                date_dir = feed_dir / date
                if not feed_dir.is_dir() or not date_dir.is_dir():
                    continue
                for path in sorted(date_dir.glob("*.md")):
                    try:
                        stat = path.stat()
                        title = ""
                        head = path.read_text(encoding="utf-8", errors="replace")[:8192]
                        for line in head.splitlines():
                            if line.startswith("# "):
                                title = line[2:].strip()
                                break
                    except OSError:
                        continue
                    files.append(
                        {
                            "path": str(path.relative_to(archive_root)),
                            "name": path.name,
                            "feed": human_names.get(feed_dir.name, feed_dir.name),
                            "archive_name": feed_dir.name,
                            "size": stat.st_size,
                            "mtime": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                            "title": title or path.stem,
                        }
                    )
        return {"ok": True, "date": date, "files": files, "total": len(files), "dir": str(archive_root)}

    def archive_file(self, relative: str) -> dict[str, Any]:
        if not relative:
            raise ApiError(400, "缺少 path 参数")
        s = self.load_settings()
        archive_root = s.resolve_path("paths.archive_dir").resolve()
        target = (archive_root / relative).resolve()
        if not target.is_relative_to(archive_root):
            raise ApiError(403, "只能读取归档目录内的文件")
        if not target.is_file():
            raise ApiError(404, "文件不存在")
        if target.stat().st_size > 5_000_000:
            raise ApiError(400, "文件过大，无法在页面中预览")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ApiError(500, f"读取文件失败：{exc}") from exc
        try:
            relative_path = str(target.relative_to(archive_root))
        except ValueError:
            relative_path = target.name
        return {"ok": True, "path": relative_path, "content": content}

    # ------------------------------------------------------------------- publish
    def publish(self, body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("confirm"):
            raise ApiError(400, "发布前需要确认")
        s = self.load_settings()
        report_dir = s.resolve_path("paths.report_dir")
        site_dir = s.resolve_path("paths.site_dir")
        add_paths: list[str] = []
        for path in (report_dir, site_dir):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                raise ApiError(400, f"目录不在项目内，无法发布：{path}")
            if not path.exists():
                raise ApiError(400, f"目录不存在：{path}")
            add_paths.append(str(relative))
        git = shutil.which("git")
        if git is None:
            raise ApiError(400, "未找到 git 命令")
        commands: list[list[str]] = [
            ["git", "add", "--", *add_paths],
            ["git", "diff", "--cached", "--name-only"],
            ["git", "status", "--short"],
        ]
        outputs: list[str] = []
        staged = ""
        for command in commands:
            result = subprocess.run(command, cwd=str(self.root), capture_output=True, text=True, timeout=60)
            outputs.append(f"$ {' '.join(command)}\n{result.stdout}{result.stderr}".strip())
            if command[1] == "diff":
                staged = result.stdout
        relevant = [
            line
            for line in staged.splitlines()
            if any(line == path or line.startswith(path.rstrip("/") + "/") for path in add_paths)
        ]
        forbidden = re.compile(r"(^|/)(\.env(\.rsshub)?|\.env|\.sqlite3|rss-downloads|logs)(/|$)")
        bad = [line for line in relevant if forbidden.search(line) or line.endswith(".sqlite3")]
        if bad:
            subprocess.run(["git", "reset", "-q", "HEAD", "--", *bad], cwd=str(self.root), capture_output=True, text=True)
            raise ApiError(400, "发布被拦截：暂存区包含敏感或不该提交的文件。", files=bad)
        if not relevant:
            return {"ok": True, "skipped": True, "message": "reports/ 与 docs/ 没有需要发布的改动。", "output": "\n\n".join(outputs)}
        message = str(body.get("message") or "").strip()
        if not message:
            dates = sorted(report_dir.glob("*.md")) if report_dir.exists() else []
            message = f"Add daily report {dates[-1].stem}" if dates else "Update reports and site"
        commit = subprocess.run(
            ["git", "commit", "-m", message, "--", *add_paths],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        outputs.append(f"$ git commit -m {message!r}\n{commit.stdout}{commit.stderr}".strip())
        if commit.returncode != 0:
            raise ApiError(500, "git commit 失败：" + sanitize(tail(commit.stdout + commit.stderr, 10), self.root))
        push = subprocess.run(["git", "push"], cwd=str(self.root), capture_output=True, text=True, timeout=180)
        outputs.append(f"$ git push\n{push.stdout}{push.stderr}".strip())
        if push.returncode != 0:
            fallback = subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=180,
            )
            outputs.append(f"$ git push -u origin HEAD\n{fallback.stdout}{fallback.stderr}".strip())
            if fallback.returncode != 0:
                raise ApiError(500, "git push 失败：" + sanitize(tail(fallback.stdout + fallback.stderr, 10), self.root))
        return {
            "ok": True,
            "skipped": False,
            "message": message,
            "output": sanitize("\n\n".join(outputs), self.root),
            "site_preview_url": "/preview/",
        }


# --------------------------------------------------------------------- HTTP server
class ConfigUiHandler(BaseHTTPRequestHandler):
    server_version = f"ConfigUI/{VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def context(self) -> UiContext:
        return self.server.context  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        message = re.sub(r"token=[^&\s]+", "token=****", message)
        sys.stderr.write(f"[config-ui] {self.address_string()} {message}\n")

    # ------------------------------------------------------------------ plumbing
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.context.port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True
        port = self.context.port
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-Config-Token") or ""
        return bool(supplied) and hmac.compare_digest(supplied, self.context.token)

    def _security_headers(self, content_type: str) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers("application/json")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._security_headers(content_type)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 不合法")
        if length > MAX_BODY:
            raise ApiError(413, "请求体过大")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"JSON 解析失败：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return parsed

    def _static_file(self, relative: str) -> Path | None:
        target = (WEB_DIR / relative).resolve()
        if not target.is_relative_to(WEB_DIR.resolve()):
            return None
        if not target.is_file():
            return None
        return target

    def _serve_web(self, relative: str = "index.html") -> None:
        target = self._static_file(relative)
        if target is None:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._send_bytes(200, body, content_type)

    def _serve_preview(self, relative: str) -> None:
        s = self.context.load_settings()
        site_root = s.resolve_path("paths.site_dir").resolve()
        target = (site_root / relative).resolve()
        if not target.is_relative_to(site_root):
            self.send_error(403)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            body = (
                "<!doctype html><meta charset='utf-8'><title>没有可预览的站点</title>"
                "<body style='font:16px/1.7 -apple-system,sans-serif;padding:40px'>"
                "<h1>没有可预览的站点</h1>"
                f"<p>先运行「仅构建站点」，输出目录：<code>{site_root}</code></p>"
                "</body>"
            ).encode("utf-8")
            self._send_bytes(404, body, "text/html; charset=utf-8")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --------------------------------------------------------------- HTTP verbs
    def do_GET(self) -> None:
        if not self._host_ok():
            self.send_error(403, "Host not allowed")
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in {"/", "/index.html"}:
            self._serve_web("index.html")
            return
        if path in {"/app.js", "/styles.css", "/favicon.svg"}:
            self._serve_web(path.lstrip("/"))
            return
        if path == "/preview":
            self._redirect("/preview/")
            return
        if path.startswith("/preview/"):
            self._serve_preview(path[len("/preview/") :])
            return
        if path.startswith("/api/"):
            self._handle_api("GET", path, query, {})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        self._handle_unsafe("POST")

    def do_PUT(self) -> None:
        self._handle_unsafe("PUT")

    def do_DELETE(self) -> None:
        self._handle_unsafe("DELETE")

    def _discard_request_body(self) -> None:
        """Drain the body so an early error cannot poison a keep-alive socket."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            try:
                self.rfile.read(min(length, MAX_BODY))
            except OSError:
                pass

    def _handle_unsafe(self, method: str) -> None:
        if not self._host_ok():
            self._discard_request_body()
            self.send_error(403, "Host not allowed")
            return
        if not self._origin_ok():
            self._discard_request_body()
            self._send_json(403, {"ok": False, "error": "跨站请求被拒绝"})
            return
        if not self._token_ok():
            self._discard_request_body()
            self._send_json(
                401,
                {
                    "ok": False,
                    "error": "缺少或无效的写入令牌。请使用启动时终端打印的带 token 链接重新打开页面。",
                    "code": "token_required",
                },
            )
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            body = self._read_json()
        except ApiError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message, **exc.extra})
            return
        self._handle_api(method, parsed.path, query, body)

    def _handle_api(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any]) -> None:
        context = self.context
        try:
            result = self._dispatch(context, method, path, query, body)
        except ApiError as exc:
            payload = {"ok": False, "error": exc.message, **exc.extra}
            self._send_json(exc.status, payload)
            return
        except FeedValidationError as exc:
            self._send_json(400, {"ok": False, "error": str(exc), "errors": exc.errors})
            return
        except ConflictError as exc:
            self._send_json(409, {"ok": False, "error": str(exc)})
            return
        except (ConfigStoreError, settings_module.SettingsError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": sanitize(str(exc), context.root)})
            return
        except LockBusy as exc:
            self._send_json(409, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive
            import traceback

            sys.stderr.write(sanitize(traceback.format_exc(), context.root))
            self._send_json(500, {"ok": False, "error": sanitize(f"服务器内部错误：{exc}", context.root)})
            return
        if result is None:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(200, result)

    def _dispatch(
        self,
        context: UiContext,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized = path.rstrip("/") or "/"
        if method == "GET":
            if normalized == "/api/bootstrap":
                return context.bootstrap()
            if normalized == "/api/ping":
                return {"ok": True, "version": VERSION}
            if normalized == "/api/feeds":
                return context.feeds_payload()
            if normalized == "/api/feeds/export":
                return context.export_feeds()
            if normalized == "/api/integrations":
                return context.integrations_payload()
            if normalized == "/api/settings":
                return context.settings_payload()
            if normalized == "/api/scheduler":
                return context.scheduler_payload()
            if normalized == "/api/rsshub":
                return context.rsshub_payload()
            if normalized == "/api/data":
                return context.data_payload()
            if normalized == "/api/health":
                connectivity = (query.get("connectivity") or [""])[0].lower() in {"1", "true", "yes"}
                return context.health(connectivity=connectivity)
            if normalized == "/api/runs":
                return {"ok": True, "runs": context.runs.history()}
            if normalized == "/api/archive":
                date = (query.get("date") or [""])[0]
                if date:
                    return context.archive_files(date)
                return {"ok": True, **context.archive_overview()}
            if normalized == "/api/archive/file":
                return context.archive_file((query.get("path") or [""])[0])
            match = re.fullmatch(r"/api/run/([A-Za-z0-9._-]+)", normalized)
            if match:
                return {"ok": True, **context.runs.status(match.group(1))}
            match = re.fullmatch(r"/api/run/([A-Za-z0-9._-]+)/log", normalized)
            if match:
                try:
                    offset = int((query.get("offset") or ["0"])[0])
                except ValueError:
                    offset = 0
                return {"ok": True, **context.runs.log_chunk(match.group(1), offset)}
        if method in {"POST", "PUT", "DELETE"}:
            if normalized == "/api/legacy/confirm":
                return context.confirm_legacy()
            if normalized == "/api/feeds/validate":
                return context.validate_feeds_body(body)
            if normalized == "/api/feeds/test":
                return context.test_feed(body)
            if normalized == "/api/feeds/import":
                return context.import_feeds(body)
            if normalized == "/api/feeds":
                if method != "PUT":
                    raise ApiError(405, "请使用 PUT 保存订阅源")
                return context.save_feeds(body)
            if normalized == "/api/integrations":
                if method != "PUT":
                    raise ApiError(405, "请使用 PUT 保存集成配置")
                return context.save_integrations(body)
            if normalized == "/api/model/test":
                return context.test_model(body)
            if normalized == "/api/mail/test":
                return context.test_mail(body)
            if normalized == "/api/settings":
                if method != "PUT":
                    raise ApiError(405, "请使用 PUT 保存设置")
                return context.save_settings(body)
            if normalized == "/api/scheduler":
                if method != "PUT":
                    raise ApiError(405, "请使用 PUT 操作定时任务")
                return context.scheduler_action(body)
            if normalized == "/api/rsshub":
                if method != "PUT":
                    raise ApiError(405, "请使用 PUT 保存 RSSHub 配置")
                return context.save_rsshub(body)
            if normalized == "/api/rsshub/action":
                return context.rsshub_action(body)
            if normalized == "/api/data/reset":
                return context.reset_feed(body)
            if normalized == "/api/run":
                record = context.runs.start(str(body.get("mode") or ""), body)
                return {"ok": True, **context.runs.status(record.id)}
            match = re.fullmatch(r"/api/run/([A-Za-z0-9._-]+)/stop", normalized)
            if match:
                return {"ok": True, **context.runs.stop(match.group(1))}
            if normalized == "/api/publish":
                return context.publish(body)
        raise ApiError(404, f"未知接口：{method} {path}")


class ConfigUiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: UiContext) -> None:
        self.context = context
        super().__init__(address, ConfigUiHandler)


def create_server(
    project_dir: Path | str,
    token: str | None = None,
    port: int = 0,
    host: str = HOST,
) -> ConfigUiServer:
    root = Path(project_dir).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"项目目录不存在：{root}")
    token = token or secrets.token_urlsafe(24)
    server = ConfigUiServer((host, port), UiContext(root, token, 0))  # type: ignore[arg-type]
    actual_port = server.server_address[1]
    server.context.port = actual_port
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 RSS 采集器配置界面（仅监听 127.0.0.1）")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="项目目录，默认为当前目录。")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认读取 config.json 的 ui.port（8765）。")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器。")
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.project_dir.expanduser().resolve()
    if not root.exists():
        parser.exit(2, f"error: 项目目录不存在：{root}\n")
    try:
        settings_obj = settings_module.load_settings(root)
        port = args.port if args.port is not None else int(settings_obj.get("ui.port"))
    except (ValueError, settings_module.SettingsError) as exc:
        parser.exit(2, f"error: 无法读取端口设置：{exc}\n")
    try:
        server = create_server(root, token=args.token, port=port)
    except OSError as exc:
        parser.exit(2, f"error: 无法监听 127.0.0.1:{port}（{exc}）。请用 --port 指定其他端口。\n")
    url = f"http://{HOST}:{server.context.port}/?token={server.context.token}"
    print("info-collector 配置界面已启动")
    print(f"  项目目录：{root}")
    print(f"  地址：{url}")
    print("  仅监听 127.0.0.1；写入接口需要本次启动生成的 token。按 Ctrl+C 退出。")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在退出配置界面...")
    finally:
        current = server.context.runs.current
        if current is not None and current.status == "running":
            try:
                server.context.runs.stop(current.id)
            except ApiError:
                pass
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
