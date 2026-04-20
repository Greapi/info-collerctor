#!/usr/bin/env python3
"""Run the daily RSS workflow: collect feeds, generate yesterday's report, email it."""

from __future__ import annotations

import argparse
import datetime as dt
import mimetypes
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import daily_report
import rss_collector


DEFAULT_LOGICAL_DATE_OFFSET_DAYS = 1
DEFAULT_FETCH_LIMIT = 100


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: list[str]
    use_ssl: bool
    use_starttls: bool
    subject_prefix: str


def env_value(name: str, file_values: dict[str, str]) -> str:
    env = os.environ.get(name)
    if env is not None:
        return env.strip()
    return file_values.get(name, "").strip()


def parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def split_recipients(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


def load_mail_config(env_file: Path = daily_report.DEFAULT_ENV_FILE) -> MailConfig:
    file_values = daily_report.load_env_file(env_file)
    missing = []

    host = env_value("SMTP_HOST", file_values)
    if not host:
        missing.append("SMTP_HOST")

    raw_port = env_value("SMTP_PORT", file_values) or "465"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be an integer") from exc

    username = env_value("SMTP_USERNAME", file_values)
    password = env_value("SMTP_PASSWORD", file_values)
    sender = env_value("MAIL_FROM", file_values) or username
    recipients = split_recipients(env_value("MAIL_TO", file_values))
    if not sender:
        missing.append("MAIL_FROM")
    if not recipients:
        missing.append("MAIL_TO")

    if missing:
        raise RuntimeError(f"Missing required mail config: {', '.join(missing)}")

    use_ssl = parse_bool(env_value("SMTP_USE_SSL", file_values), default=(port == 465))
    use_starttls = parse_bool(env_value("SMTP_USE_STARTTLS", file_values), default=(port == 587))
    subject_prefix = env_value("MAIL_SUBJECT_PREFIX", file_values) or "RSS 日报"

    return MailConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        use_ssl=use_ssl,
        use_starttls=use_starttls,
        subject_prefix=subject_prefix,
    )


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
) -> Path:
    report_path = daily_report.generate_report(
        report_date=report_date,
        input_dir=input_dir,
        output_dir=output_dir,
        config_path=config_path,
        db_path=db_path,
        should_fetch=True,
        fetch_limit=fetch_limit,
        timeout=timeout,
    )
    if should_send:
        mail = load_mail_config()
        print(f"Sending {report_path} to {', '.join(mail.recipients)}...")
        send_report(mail, report_path, report_date, timeout=timeout)
        print("Email sent.")
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect RSS, generate yesterday's report, and send it by email.")
    parser.add_argument("--date", default=yesterday(), help="Report date in YYYY-MM-DD format. Defaults to yesterday.")
    parser.add_argument("--input", type=Path, default=daily_report.DEFAULT_INPUT, help="RSS Markdown input directory.")
    parser.add_argument("--output", type=Path, default=daily_report.DEFAULT_OUTPUT, help="Report output directory.")
    parser.add_argument("--config", type=Path, default=rss_collector.DEFAULT_CONFIG, help="RSS feed config file.")
    parser.add_argument("--db", type=Path, default=rss_collector.DEFAULT_DB, help="RSS deduplication SQLite file.")
    parser.add_argument("--timeout", type=int, default=120, help="Network timeout in seconds.")
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=DEFAULT_FETCH_LIMIT,
        help="Maximum entries to fetch per feed. Defaults to 100 for daily runs.",
    )
    parser.add_argument("--no-send", action="store_true", help="Run collect and report generation without sending email.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.exit(2, "error: --date must use YYYY-MM-DD format\n")

    try:
        run_workflow(
            report_date=args.date,
            input_dir=args.input,
            output_dir=args.output,
            config_path=args.config,
            db_path=args.db,
            fetch_limit=args.fetch_limit,
            timeout=args.timeout,
            should_send=not args.no_send,
        )
    except (FileNotFoundError, RuntimeError, ValueError, OSError, smtplib.SMTPException) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
