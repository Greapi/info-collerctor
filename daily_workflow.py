#!/usr/bin/env python3
"""Run the daily RSS workflow: collect feeds, generate yesterday's report, email it."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

import daily_report
import settings as settings_module
from config_store import atomic_write
from runlock import LockBusy, RunLock, lock_path
from settings import MailConfig


DEFAULT_LOGICAL_DATE_OFFSET_DAYS = 1
DEFAULT_FETCH_LIMIT = 100


def env_value(name: str, file_values: dict[str, str]) -> str:
    env = os.environ.get(name)
    if env is not None:
        return env.strip()
    return file_values.get(name, "").strip()


def load_mail_config(env_file: Path = daily_report.DEFAULT_ENV_FILE) -> MailConfig:
    if env_file == daily_report.DEFAULT_ENV_FILE:
        env_file = settings_module.PROJECT_ROOT / ".env"
    return settings_module.load_mail_config(env_path=env_file, require=True)


def yesterday() -> str:
    return (dt.datetime.now().date() - dt.timedelta(days=DEFAULT_LOGICAL_DATE_OFFSET_DAYS)).isoformat()


def send_report(mail: MailConfig, report_path: Path, report_date: str, timeout: int) -> None:
    body = report_path.read_text(encoding="utf-8")
    message = EmailMessage()
    message["Subject"] = f"{mail.subject_prefix} {report_date}"
    message["From"] = mail.sender
    message["To"] = ", ".join(mail.recipients)
    message.set_content(body)

    content_type, _ = mimetypes.guess_type(report_path.name)
    maintype, subtype = (content_type or "text/markdown").split("/", 1)
    message.add_attachment(
        body.encode("utf-8"),
        maintype=maintype,
        subtype=subtype,
        filename=report_path.name,
    )

    context = ssl.create_default_context()
    if mail.use_ssl:
        with smtplib.SMTP_SSL(mail.host, mail.port, timeout=timeout, context=context) as smtp:
            login_if_needed(smtp, mail)
            smtp.send_message(message)
        return

    with smtplib.SMTP(mail.host, mail.port, timeout=timeout) as smtp:
        if mail.use_starttls:
            smtp.starttls(context=context)
        login_if_needed(smtp, mail)
        smtp.send_message(message)


def login_if_needed(smtp: smtplib.SMTP, mail: MailConfig) -> None:
    if mail.username or mail.password:
        smtp.login(mail.username, mail.password)


def run_workflow(
    report_date: str,
    input_dir: Path,
    output_dir: Path,
    config_path: Path,
    db_path: Path,
    fetch_limit: int | None,
    timeout: int,
    should_send: bool,
    should_fetch: bool = True,
    summary: dict | None = None,
) -> Path:
    summary = summary if summary is not None else {}
    report_path = daily_report.generate_report(
        report_date=report_date,
        input_dir=input_dir,
        output_dir=output_dir,
        config_path=config_path,
        db_path=db_path,
        should_fetch=should_fetch,
        fetch_limit=fetch_limit,
        timeout=timeout,
        summary=summary,
    )
    if should_send:
        mail = load_mail_config()
        summary["mail"] = {"recipients": mail.recipients, "status": "sending"}
        print(f"Sending {report_path} to {', '.join(mail.recipients)}...")
        send_report(mail, report_path, report_date, timeout=timeout)
        summary["mail"]["status"] = "sent"
        print("Email sent.")
    else:
        summary["mail"] = {"status": "skipped"}
    return report_path


def build_parser(settings_obj: settings_module.Settings | None = None) -> argparse.ArgumentParser:
    settings_obj = settings_obj or settings_module.load_settings()
    parser = argparse.ArgumentParser(description="Collect RSS, generate the daily report, and send it by email.")
    parser.add_argument(
        "--date",
        default=settings_obj.default_report_date(),
        help="Report date in YYYY-MM-DD format. Defaults to today minus run.date_offset.",
    )
    parser.add_argument(
        "--input", type=Path, default=settings_obj.resolve_path("paths.archive_dir"), help="RSS Markdown input directory."
    )
    parser.add_argument(
        "--output", type=Path, default=settings_obj.resolve_path("paths.report_dir"), help="Report output directory."
    )
    parser.add_argument("--config", type=Path, default=settings_obj.feeds_path, help="RSS feed config file.")
    parser.add_argument(
        "--db", type=Path, default=settings_obj.resolve_path("paths.db_path"), help="RSS deduplication SQLite file."
    )
    parser.add_argument(
        "--timeout", type=int, default=int(settings_obj.get("run.timeout")), help="Network timeout in seconds."
    )
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=int(settings_obj.get("run.fetch_limit")),
        help="Maximum entries to fetch per feed. Use 0 for unlimited.",
    )
    parser.add_argument("--no-send", action="store_true", help="Run collect and report generation without sending email.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip RSS fetching and use local Markdown only.")
    parser.add_argument("--summary-json", type=Path, help="Write a structured run summary to this JSON file.")
    return parser


def main(argv: list[str] | None = None) -> int:
    settings_obj = settings_module.load_settings()
    parser = build_parser(settings_obj)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.exit(2, "error: --date must use YYYY-MM-DD format\n")
    summary: dict = {
        "ok": True,
        "mode": "workflow",
        "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }

    try:
        with RunLock(lock_path(settings_obj.base_dir), label="完整工作流", metadata={"mode": "workflow"}):
            path = run_workflow(
                report_date=args.date,
                input_dir=args.input,
                output_dir=args.output,
                config_path=args.config,
                db_path=args.db,
                fetch_limit=args.fetch_limit,
                timeout=args.timeout,
                should_send=not args.no_send,
                should_fetch=not args.no_fetch,
                summary=summary,
            )
        summary["report_path"] = str(path)
    except LockBusy as exc:
        summary.update({"ok": False, "error": str(exc)})
        _write_summary(args.summary_json, summary)
        parser.exit(1, f"error: {exc}\n")
    except (FileNotFoundError, RuntimeError, ValueError, OSError, smtplib.SMTPException) as exc:
        summary.update({"ok": False, "error": str(exc)})
        _write_summary(args.summary_json, summary)
        parser.exit(1, f"error: {exc}\n")
    except KeyboardInterrupt:
        summary.update({"ok": False, "error": "用户中断"})
        _write_summary(args.summary_json, summary)
        return 130
    finally:
        summary["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _write_summary(args.summary_json, summary)
    return 0


def _write_summary(path: Path | None, summary: dict) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
