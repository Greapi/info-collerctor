#!/usr/bin/env python3
"""Unified, dependency-free settings resolution for the info collector.

Priority (highest first): real environment variables, ``.env`` / ``.env.rsshub``
files, ``config.json`` and finally the constants in this module.  The command
line still wins over everything because the argparse defaults are filled from
this module.

``settings`` deliberately does not import the collector scripts so the
configuration UI can describe and validate the configuration without fetching
feeds or calling a model.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from env_file import EnvDocument, mask_secret

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_FILENAME = "config.json"
ENV_FILENAME = ".env"
RSSHUB_ENV_FILENAME = ".env.rsshub"
FEEDS_FILENAME = "feeds.txt"

SOURCE_ENVIRONMENT = "environment"
SOURCE_ENVFILE = "envfile"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"

SOURCE_LABELS = {
    SOURCE_ENVIRONMENT: "环境变量",
    SOURCE_ENVFILE: ".env 文件",
    SOURCE_CONFIG: "config.json",
    SOURCE_DEFAULT: "默认值",
}

FIELDS: dict[str, tuple[Any, str]] = {
    "paths.archive_dir": ("rss-downloads", "str"),
    "paths.report_dir": ("reports", "str"),
    "paths.db_path": (".rss_collector.sqlite3", "str"),
    "paths.site_dir": ("docs", "str"),
    "run.timeout": (60, "int"),
    "run.fetch_limit": (100, "int"),
    "run.date_offset": (1, "int"),
    "model.temperature": (0.2, "float"),
    "model.max_chars_per_article": (8000, "int"),
    "model.max_chars_per_batch": (24000, "int"),
    "schedule.hour": (4, "int"),
    "schedule.minute": (0, "int"),
    "ui.port": (8765, "int"),
}

FIELD_LABELS = {
    "paths.archive_dir": "归档目录",
    "paths.report_dir": "日报目录",
    "paths.db_path": "去重数据库",
    "paths.site_dir": "站点目录",
    "run.timeout": "网络超时",
    "run.fetch_limit": "每源抓取条数",
    "run.date_offset": "默认日报日期偏移",
    "model.temperature": "模型 temperature",
    "model.max_chars_per_article": "单篇截断字符数",
    "model.max_chars_per_batch": "单批字符上限",
    "schedule.hour": "定时小时",
    "schedule.minute": "定时分钟",
    "ui.port": "配置界面端口",
}

ENV_OVERRIDES = {
    "paths.archive_dir": "INFO_COLLECTOR_ARCHIVE_DIR",
    "paths.report_dir": "INFO_COLLECTOR_REPORT_DIR",
    "paths.db_path": "INFO_COLLECTOR_DB",
    "paths.site_dir": "INFO_COLLECTOR_SITE_DIR",
    "run.timeout": "INFO_COLLECTOR_TIMEOUT",
    "run.fetch_limit": "INFO_COLLECTOR_FETCH_LIMIT",
    "run.date_offset": "INFO_COLLECTOR_DATE_OFFSET",
    "model.temperature": "INFO_COLLECTOR_TEMPERATURE",
    "model.max_chars_per_article": "INFO_COLLECTOR_MAX_CHARS_PER_ARTICLE",
    "model.max_chars_per_batch": "INFO_COLLECTOR_MAX_CHARS_PER_BATCH",
    "ui.port": "INFO_COLLECTOR_UI_PORT",
}

MODEL_ENV_KEYS = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")
MAIL_ENV_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_USE_SSL",
    "SMTP_USE_STARTTLS",
    "MAIL_FROM",
    "MAIL_TO",
    "MAIL_SUBJECT_PREFIX",
)
RSSHUB_ENV_KEYS = (
    "RSSHUB_HOST",
    "RSSHUB_PORT",
    "RSSHUB_IMAGE",
    "BROWSERLESS_IMAGE",
    "REDIS_IMAGE",
    "RSSHUB_PROXY",
    "GITHUB_ACCESS_TOKEN",
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_SMTP_PORT = 465
DEFAULT_SUBJECT_PREFIX = "RSS 日报"


class SettingsError(ValueError):
    """Raised when a configuration file cannot be interpreted."""


@dataclass(frozen=True)
class Setting:
    key: str
    value: Any
    source: str
    env_name: str | None = None

    def display(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": FIELD_LABELS.get(self.key, self.key),
            "value": self.value,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "env_name": self.env_name,
        }


@dataclass
class Settings:
    base_dir: Path
    config_path: Path
    env_path: Path
    rsshub_env_path: Path
    feeds_path: Path
    values: dict[str, Setting] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    config_unknown_keys: list[str] = field(default_factory=list)

    def get(self, key: str) -> Any:
        return self.values[key].value

    def setting(self, key: str) -> Setting:
        return self.values[key]

    def resolve_path(self, key: str) -> Path:
        return resolve_path(str(self.get(key)), self.base_dir)

    def default_report_date(self) -> str:
        offset = int(self.get("run.date_offset"))
        return (dt.date.today() - dt.timedelta(days=offset)).isoformat()

    def display_items(self) -> list[dict[str, Any]]:
        items = []
        for key in FIELDS:
            item = self.values[key].display()
            if key.startswith("paths."):
                path = self.resolve_path(key)
                item["absolute"] = str(path)
                item["path_status"] = path_status(path, is_dir=not key.endswith("db_path"))
            items.append(item)
        return items


def _coerce(key: str, raw: Any, kind: str, source: str) -> Any:
    if kind == "int":
        if isinstance(raw, bool):
            raise SettingsError(f"{FIELD_LABELS.get(key, key)} 必须是整数")
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsError(
                f"{FIELD_LABELS.get(key, key)} 必须是整数（来源：{SOURCE_LABELS.get(source, source)}，值：{raw!r}）"
            ) from exc
    if kind == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsError(
                f"{FIELD_LABELS.get(key, key)} 必须是数字（来源：{SOURCE_LABELS.get(source, source)}，值：{raw!r}）"
            ) from exc
    return str(raw)


def load_settings(
    base_dir: Path | str | None = None,
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    config = Path(config_path).expanduser().resolve() if config_path else base / CONFIG_FILENAME
    environ = os.environ if environ is None else environ
    settings = Settings(
        base_dir=base,
        config_path=config,
        env_path=base / ENV_FILENAME,
        rsshub_env_path=base / RSSHUB_ENV_FILENAME,
        feeds_path=base / FEEDS_FILENAME,
    )

    raw_config: dict[str, Any] = {}
    if config.exists():
        try:
            parsed = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            settings.errors.append(f"config.json 无法解析：{exc}")
            parsed = {}
        if isinstance(parsed, dict):
            raw_config = parsed
        else:
            settings.errors.append("config.json 顶层必须是 JSON 对象")
    flat_config: dict[str, Any] = {}
    for key in FIELDS:
        section, name = key.split(".", 1)
        value = raw_config.get(section)
        if isinstance(value, dict) and name in value:
            flat_config[key] = value[name]
    settings.config_unknown_keys = _unknown_config_keys(raw_config)

    for key, (default, kind) in FIELDS.items():
        env_name = ENV_OVERRIDES.get(key)
        raw: Any = default
        selected_source = SOURCE_DEFAULT
        if key in flat_config:
            try:
                raw = _coerce(key, flat_config[key], kind, SOURCE_CONFIG)
                selected_source = SOURCE_CONFIG
            except SettingsError as exc:
                settings.errors.append(str(exc))
                raw = default
        if env_name and env_name in environ:
            value = environ[env_name]
            try:
                raw = _coerce(key, value, kind, SOURCE_ENVIRONMENT)
                selected_source = SOURCE_ENVIRONMENT
            except SettingsError as exc:
                settings.errors.append(str(exc))
        settings.values[key] = Setting(key, raw, selected_source, env_name)
    return settings


def _unknown_config_keys(data: dict[str, Any]) -> list[str]:
    known_sections = {key.split(".", 1)[0] for key in FIELDS}
    unknown: list[str] = []
    for section, value in data.items():
        if section not in known_sections:
            unknown.append(section)
            continue
        if isinstance(value, dict):
            for name in value:
                if f"{section}.{name}" not in FIELDS:
                    unknown.append(f"{section}.{name}")
    return unknown


def resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def path_status(path: Path, is_dir: bool = True) -> dict[str, Any]:
    """Describe whether a configured path exists and can be written."""
    exists = path.exists()
    writable = False
    reason = ""
    if exists:
        if is_dir and not path.is_dir():
            reason = "存在但不是目录"
        elif not is_dir and path.is_dir():
            reason = "存在但是目录"
        elif os.access(path, os.W_OK):
            writable = True
        else:
            reason = "当前用户没有写权限"
    else:
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if parent.exists() and parent.is_dir() and os.access(parent, os.W_OK):
            writable = True
            reason = "目录不存在，保存时会自动创建"
        else:
            reason = f"上级目录不存在或不可写：{parent}"
    return {
        "absolute": str(path),
        "exists": exists,
        "is_dir": path.is_dir(),
        "writable": writable,
        "reason": reason,
    }


def split_recipients(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value or "") if item.strip()]


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    model: str
    base_url: str
    temperature: float = 0.2
    max_chars_per_article: int = 8000
    max_chars_per_batch: int = 24000
    sources: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.api_key:
            missing.append("OPENAI_API_KEY")
        if not self.model:
            missing.append("OPENAI_MODEL")
        return missing

    @property
    def ready(self) -> bool:
        return not self.missing

    def display(self, masked: bool = True) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": mask_secret(self.api_key) if masked else self.api_key,
            "api_key_saved": bool(self.api_key),
            "temperature": self.temperature,
            "max_chars_per_article": self.max_chars_per_article,
            "max_chars_per_batch": self.max_chars_per_batch,
            "sources": dict(self.sources),
            "source_labels": {key: SOURCE_LABELS.get(value, value) for key, value in self.sources.items()},
            "missing": self.missing,
            "ready": self.ready,
            "errors": list(self.errors),
        }


def load_model_config(
    base_dir: Path | str | None = None,
    env_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    settings_obj: Settings | None = None,
) -> ModelConfig:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    env_file = Path(env_path).expanduser().resolve() if env_path else base / ENV_FILENAME
    environ = os.environ if environ is None else environ
    settings = settings_obj or load_settings(base, environ=environ)
    document = EnvDocument.read(env_file)
    values = document.values
    sources: dict[str, str] = {}

    def pick(name: str, default: str = "") -> str:
        if name in environ:
            sources[name] = SOURCE_ENVIRONMENT
            return str(environ[name]).strip()
        if name in values:
            sources[name] = SOURCE_ENVFILE
            return values[name].strip()
        sources[name] = SOURCE_DEFAULT
        return default

    api_key = pick("OPENAI_API_KEY")
    model = pick("OPENAI_MODEL")
    base_url = (pick("OPENAI_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    return ModelConfig(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=float(settings.get("model.temperature")),
        max_chars_per_article=int(settings.get("model.max_chars_per_article")),
        max_chars_per_batch=int(settings.get("model.max_chars_per_batch")),
        sources=sources,
        errors=tuple(document.errors),
    )


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
    sources: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.host:
            missing.append("SMTP_HOST")
        if not self.sender:
            missing.append("MAIL_FROM")
        if not self.recipients:
            missing.append("MAIL_TO")
        return missing

    @property
    def ready(self) -> bool:
        return not self.missing

    def display(self, masked: bool = True) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": mask_secret(self.password) if masked else self.password,
            "password_saved": bool(self.password),
            "sender": self.sender,
            "recipients": list(self.recipients),
            "recipients_text": ", ".join(self.recipients),
            "use_ssl": self.use_ssl,
            "use_starttls": self.use_starttls,
            "subject_prefix": self.subject_prefix,
            "sources": dict(self.sources),
            "source_labels": {key: SOURCE_LABELS.get(value, value) for key, value in self.sources.items()},
            "missing": self.missing,
            "ready": self.ready,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def load_mail_config(
    base_dir: Path | str | None = None,
    env_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    settings_obj: Settings | None = None,
    require: bool = False,
) -> MailConfig:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    env_file = Path(env_path).expanduser().resolve() if env_path else base / ENV_FILENAME
    environ = os.environ if environ is None else environ
    settings_obj = settings_obj or load_settings(base, environ=environ)
    document = EnvDocument.read(env_file)
    values = document.values
    sources: dict[str, str] = {}

    def pick(name: str, default: str = "") -> str:
        if name in environ:
            sources[name] = SOURCE_ENVIRONMENT
            return str(environ[name]).strip()
        if name in values:
            sources[name] = SOURCE_ENVFILE
            return values[name].strip()
        sources[name] = SOURCE_DEFAULT
        return default

    host = pick("SMTP_HOST")
    raw_port = pick("SMTP_PORT", str(DEFAULT_SMTP_PORT)) or str(DEFAULT_SMTP_PORT)
    errors: list[str] = list(document.errors)
    try:
        port = int(raw_port)
    except ValueError:
        errors.append(f"SMTP_PORT 必须是整数，当前值：{raw_port!r}")
        port = DEFAULT_SMTP_PORT
    username = pick("SMTP_USERNAME")
    password = pick("SMTP_PASSWORD")
    sender = pick("MAIL_FROM") or username
    recipients = split_recipients(pick("MAIL_TO"))
    use_ssl = parse_bool(pick("SMTP_USE_SSL", ""), default=(port == 465))
    use_starttls = parse_bool(pick("SMTP_USE_STARTTLS", ""), default=(port == 587))
    subject_prefix = pick("MAIL_SUBJECT_PREFIX", DEFAULT_SUBJECT_PREFIX) or DEFAULT_SUBJECT_PREFIX
    warnings: list[str] = []
    if use_ssl and use_starttls:
        warnings.append("SSL 与 STARTTLS 同时开启：多数服务器只支持其中一种，请确认端口与服务端配置。")
    if port == 587 and use_ssl:
        warnings.append("端口 587 通常使用 STARTTLS 而不是 SSL。")
    if port == 465 and use_starttls:
        warnings.append("端口 465 通常使用 SSL 而不是 STARTTLS。")
    mail = MailConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        use_ssl=use_ssl,
        use_starttls=use_starttls,
        subject_prefix=subject_prefix,
        sources=sources,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    if require and mail.missing:
        raise RuntimeError(f"Missing required mail config: {', '.join(mail.missing)}")
    return mail


@dataclass(frozen=True)
class RsshubConfig:
    host: str
    port: int
    image: str
    browserless_image: str
    redis_image: str
    proxy: str
    token: str
    sources: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def display(self, masked: bool = True) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "image": self.image,
            "browserless_image": self.browserless_image,
            "redis_image": self.redis_image,
            "proxy": self.proxy,
            "token": mask_secret(self.token) if masked else self.token,
            "token_saved": bool(self.token),
            "sources": dict(self.sources),
            "source_labels": {key: SOURCE_LABELS.get(value, value) for key, value in self.sources.items()},
            "errors": list(self.errors),
        }


def load_rsshub_config(
    base_dir: Path | str | None = None,
    env_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RsshubConfig:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    env_file = Path(env_path).expanduser().resolve() if env_path else base / RSSHUB_ENV_FILENAME
    environ = os.environ if environ is None else environ
    document = EnvDocument.read(env_file)
    values = document.values
    sources: dict[str, str] = {}

    def pick(name: str, default: str = "") -> str:
        if name in environ:
            sources[name] = SOURCE_ENVIRONMENT
            return str(environ[name]).strip()
        if name in values:
            sources[name] = SOURCE_ENVFILE
            return values[name].strip()
        sources[name] = SOURCE_DEFAULT
        return default

    defaults = {
        "RSSHUB_HOST": "127.0.0.1",
        "RSSHUB_PORT": "1200",
        "RSSHUB_IMAGE": "diygod/rsshub:latest",
        "BROWSERLESS_IMAGE": "browserless/chrome:latest",
        "REDIS_IMAGE": "redis:7-alpine",
        "RSSHUB_PROXY": "",
        "GITHUB_ACCESS_TOKEN": "",
    }
    raw = {name: pick(name, default) for name, default in defaults.items()}
    errors = list(document.errors)
    try:
        port = int(raw["RSSHUB_PORT"])
    except ValueError:
        errors.append(f"RSSHUB_PORT 必须是整数，当前值：{raw['RSSHUB_PORT']!r}")
        port = 1200
    return RsshubConfig(
        host=raw["RSSHUB_HOST"],
        port=port,
        image=raw["RSSHUB_IMAGE"],
        browserless_image=raw["BROWSERLESS_IMAGE"],
        redis_image=raw["REDIS_IMAGE"],
        proxy=raw["RSSHUB_PROXY"],
        token=raw["GITHUB_ACCESS_TOKEN"],
        sources=sources,
        errors=tuple(errors),
    )


def secret_values(base_dir: Path | str | None = None, environ: Mapping[str, str] | None = None) -> list[str]:
    """Collect secret-looking values so logs can mask them."""
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    environ = os.environ if environ is None else environ
    values: set[str] = set()
    for name in (
        "OPENAI_API_KEY",
        "SMTP_PASSWORD",
        "GITHUB_ACCESS_TOKEN",
        "YOUTUBE_KEY",
    ):
        value = str(environ.get(name, "")).strip()
        if len(value) >= 4:
            values.add(value)
    for path in (base / ENV_FILENAME, base / RSSHUB_ENV_FILENAME):
        document = EnvDocument.read(path)
        for key, value in document.values.items():
            if re.search(r"(KEY|PASSWORD|TOKEN|SECRET|COOKIE)", key, flags=re.I) and len(value.strip()) >= 4:
                values.add(value.strip())
    return sorted(values, key=len, reverse=True)


def mask_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "****")
    return text


def summarize_effective(settings_obj: Settings) -> dict[str, Any]:
    return {
        "base_dir": str(settings_obj.base_dir),
        "config_path": str(settings_obj.config_path),
        "env_path": str(settings_obj.env_path),
        "rsshub_env_path": str(settings_obj.rsshub_env_path),
        "feeds_path": str(settings_obj.feeds_path),
        "items": settings_obj.display_items(),
        "errors": list(settings_obj.errors),
        "unknown_config_keys": list(settings_obj.config_unknown_keys),
    }
