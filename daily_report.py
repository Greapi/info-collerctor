#!/usr/bin/env python3
"""Generate a Chinese daily report from locally archived RSS Markdown files."""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request

from dataclasses import dataclass
from pathlib import Path

import rss_collector
import settings as settings_module
from config_store import atomic_write
from env_file import EnvDocument
from feed_config import disabled_archive_names
from runlock import LockBusy, RunLock, lock_path


DEFAULT_INPUT = Path("rss-downloads")
DEFAULT_OUTPUT = Path("reports")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ENV_FILE = Path(".env")
MAX_CHARS_PER_BATCH = 24_000
MAX_CHARS_PER_ARTICLE = 8_000


@dataclass(frozen=True)
class Article:
    title: str
    feed: str
    link: str
    published: str
    body: str
    path: Path


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str
    base_url: str
    temperature: float = 0.2
    max_chars_per_article: int = MAX_CHARS_PER_ARTICLE
    max_chars_per_batch: int = MAX_CHARS_PER_BATCH


def parse_front_matter(markdown: str) -> tuple[dict[str, str], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---", 4)
    if end == -1:
        return {}, markdown

    metadata: dict[str, str] = {}
    raw_metadata = markdown[4:end]
    body = markdown[end + len("\n---") :].lstrip()
    for line in raw_metadata.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = unquote_front_matter(value.strip())
    return metadata, body


def unquote_front_matter(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def load_articles(
    input_dir: Path,
    report_date: str,
    previous_date: str | None = None,
    disabled_feeds: set[str] | None = None,
) -> list[Article]:
    articles: list[Article] = []
    if not input_dir.exists():
        return articles
    disabled_feeds = disabled_feeds or set()

    for path in sorted(input_dir.glob(f"*/{report_date}/*.md")):
        article = _parse_article(path, report_date)
        if article.feed not in disabled_feeds:
            articles.append(article)

    if previous_date:
        for path in sorted(input_dir.glob(f"*/{previous_date}/*.md")):
            article = _parse_article(path, previous_date)
            if article.feed in disabled_feeds:
                continue
            article = Article(
                title=f"[昨日补遗] {article.title}",
                feed=article.feed,
                link=article.link,
                published=article.published,
                body=article.body,
                path=article.path,
            )
            articles.append(article)

    return articles


def _parse_article(path: Path, fallback_date: str) -> Article:
    markdown = path.read_text(encoding="utf-8")
    metadata, body = parse_front_matter(markdown)
    return Article(
        title=metadata.get("title") or first_heading(body) or path.stem,
        feed=metadata.get("feed") or path.parent.parent.name,
        link=metadata.get("link") or "",
        published=metadata.get("published") or fallback_date,
        body=strip_source_footer(body),
        path=path,
    )


def first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def strip_source_footer(markdown: str) -> str:
    return re.sub(r"\n+Source: \[.*?\]\(.*?\)\s*$", "", markdown.strip(), flags=re.S)


def today_local() -> str:
    return dt.datetime.now().date().isoformat()


def previous_date(date_str: str) -> str:
    return (dt.date.fromisoformat(date_str) - dt.timedelta(days=1)).isoformat()


def load_env_file(path: Path) -> dict[str, str]:
    document = EnvDocument.read(path)
    if document.errors:
        raise ValueError("；".join(document.errors))
    return document.values


def unquote_env_value(value: str) -> str:
    from env_file import unquote_value

    return unquote_value(value)


def config_value(name: str, file_values: dict[str, str]) -> str:
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value.strip()
    return file_values.get(name, "").strip()


def load_openai_config(env_file: Path = DEFAULT_ENV_FILE, settings_obj: settings_module.Settings | None = None) -> OpenAIConfig:
    if env_file == DEFAULT_ENV_FILE and settings_obj is not None:
        env_file = settings_obj.env_path
    config = settings_module.load_model_config(env_path=env_file, settings_obj=settings_obj)
    if config.errors:
        raise ValueError("；".join(config.errors))
    if config.missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(config.missing)}")
    return OpenAIConfig(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        temperature=config.temperature,
        max_chars_per_article=config.max_chars_per_article,
        max_chars_per_batch=config.max_chars_per_batch,
    )


def article_to_prompt(article: Article, max_chars: int = MAX_CHARS_PER_ARTICLE) -> str:
    body = article.body.strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n[内容已截断]"
    return textwrap.dedent(
        f"""\
        标题: {article.title}
        来源: {article.feed}
        发布时间: {article.published}
        链接: {article.link}
        本地文件: {article.path}

        内容:
        {body}
        """
    ).strip()


def build_batches(
    articles: list[Article],
    max_chars_per_article: int = MAX_CHARS_PER_ARTICLE,
    max_chars_per_batch: int = MAX_CHARS_PER_BATCH,
) -> list[list[Article]]:
    batches: list[list[Article]] = []
    current: list[Article] = []
    current_size = 0
    for article in articles:
        size = len(article_to_prompt(article, max_chars=max_chars_per_article))
        if current and current_size + size > max_chars_per_batch:
            batches.append(current)
            current = []
            current_size = 0
        current.append(article)
        current_size += size
    if current:
        batches.append(current)
    return batches


def chat_completion(
    config: OpenAIConfig,
    messages: list[dict[str, str]],
    timeout: int,
    retries: int | None = None,
) -> str:
    if retries is None:
        retries = 3
    payload = json.dumps(
        {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": rss_collector.USER_AGENT,
        },
        method="POST",
    )
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Model returned non-JSON response: {raw[:500]}") from exc
            try:
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"Unexpected model response: {data}") from exc
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                pass
            raise RuntimeError(f"Model request failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                wait = 2 ** attempt
                print(f"  chat retry {attempt}/{retries} in {wait}s: {exc}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"Model request failed after {retries} retries: {exc}") from exc

    raise last_exc  # type: ignore[misc]


def summarize_batch(config: OpenAIConfig, report_date: str, batch: list[Article], timeout: int) -> str:
    article_block = "\n\n---\n\n".join(
        article_to_prompt(article, max_chars=config.max_chars_per_article) for article in batch
    )
    return chat_completion(
        config,
        [
            {
                "role": "system",
                "content": "你是严谨的中文资讯分析师。只根据用户提供的 RSS 文章总结，不编造事实。",
            },
            {
                "role": "user",
                "content": textwrap.dedent(
                    f"""\
                    请把下面这些 {report_date} 的 RSS 文章压缩成中间摘要，供最终日报使用。

                    要求：
                    - 用中文。
                    - 保留重要事实、变化、数字、机构和产品名称。
                    - 每个重要判断后尽量保留来源标题或链接。
                    - 不要写最终日报标题。

                    文章：
                    {article_block}
                    """
                ),
            },
        ],
        timeout=timeout,
    )


def generate_final_report(
    config: OpenAIConfig,
    report_date: str,
    articles: list[Article],
    source_material: str,
    timeout: int,
) -> str:
    source_index = "\n".join(
        f"- [{article.title}]({article.link}) - {article.feed}" if article.link else f"- {article.title} - {article.feed}"
        for article in articles
    )
    report = chat_completion(
        config,
        [
            {
                "role": "system",
                "content": "你是严谨的中文日报编辑。只根据用户提供的材料写作，所有关键判断都要可追溯到来源。",
            },
            {
                "role": "user",
                "content": textwrap.dedent(
                    f"""\
                    请根据材料生成 {report_date} 的 RSS 日报。

                    必须使用以下 Markdown 结构：
                    # {report_date} RSS 日报

                    ## 今日概览
                    ## 重点事件
                    ## 趋势与判断
                    ## 值得继续关注
                    ## 来源索引

                    写作要求：
                    - 中文，结构化简报风格。
                    - 不要编造材料中没有的信息。
                    - 重点事件和趋势判断要尽量附带 Markdown 链接。
                    - 来源索引必须覆盖下面列出的全部来源。

                    材料：
                    {source_material}

                    全部来源：
                    {source_index}
                    """
                ),
            },
        ],
        timeout=timeout,
    )
    if not report.lstrip().startswith(f"# {report_date} RSS 日报"):
        report = f"# {report_date} RSS 日报\n\n{report.lstrip()}"
    return report.rstrip() + "\n"


def write_no_articles_report(output_dir: Path, report_date: str, input_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{report_date}.md"
    path.write_text(
        textwrap.dedent(
            f"""\
            # {report_date} RSS 日报

            今天没有找到可分析的 RSS Markdown。

            检查路径：`{input_dir}`
            """
        ),
        encoding="utf-8",
    )
    return path


def generate_report(
    report_date: str,
    input_dir: Path,
    output_dir: Path,
    config_path: Path,
    db_path: Path,
    should_fetch: bool,
    fetch_limit: int | None,
    timeout: int,
    summary: dict | None = None,
) -> Path:
    summary = summary if summary is not None else {}
    if should_fetch:
        rss_collector.collect(
            config=config_path,
            output=input_dir,
            db_path=db_path,
            timeout=timeout,
            limit=fetch_limit,
            default_date=report_date,
            summary=summary,
        )

    disabled_feeds = disabled_archive_names(config_path)
    summary["disabled_feeds"] = sorted(disabled_feeds)
    articles = load_articles(
        input_dir,
        report_date,
        previous_date=previous_date(report_date),
        disabled_feeds=disabled_feeds,
    )
    summary["article_count"] = len(articles)
    if not articles:
        path = write_no_articles_report(output_dir, report_date, input_dir)
        summary["report_path"] = str(path)
        print(f"No articles found for {report_date}. Wrote {path}.")
        return path

    config = load_openai_config()
    summary["model"] = {"model": config.model, "base_url": config.base_url}
    batches = build_batches(
        articles,
        max_chars_per_article=config.max_chars_per_article,
        max_chars_per_batch=config.max_chars_per_batch,
    )
    summary["batch_count"] = len(batches)
    if len(batches) == 1:
        source_material = "\n\n---\n\n".join(
            article_to_prompt(article, max_chars=config.max_chars_per_article) for article in articles
        )
    else:
        summaries = []
        for index, batch in enumerate(batches, start=1):
            print(f"Summarizing batch {index}/{len(batches)} ({len(batch)} article(s))...")
            summaries.append(summarize_batch(config, report_date, batch, timeout=timeout))
        source_material = "\n\n---\n\n".join(
            f"批次 {index} 摘要:\n{summary}" for index, summary in enumerate(summaries, start=1)
        )

    print(f"Generating final report from {len(articles)} article(s)...")
    report = generate_final_report(config, report_date, articles, source_material, timeout=timeout)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{report_date}.md"
    path.write_text(report, encoding="utf-8")
    summary["report_path"] = str(path)
    print(f"Wrote {path}.")
    return path


def build_parser(settings_obj: settings_module.Settings | None = None) -> argparse.ArgumentParser:
    settings_obj = settings_obj or settings_module.load_settings()
    parser = argparse.ArgumentParser(description="Generate a daily RSS report with an OpenAI-compatible model.")
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
        help="Maximum entries to fetch per feed before reporting. Use 0 for unlimited.",
    )
    parser.add_argument("--no-fetch", action="store_true", help="Skip RSS fetching and only analyze local Markdown.")
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
        "mode": "report",
        "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }

    try:
        with RunLock(lock_path(settings_obj.base_dir), label="仅日报", metadata={"mode": "report"}):
            path = generate_report(
                report_date=args.date,
                input_dir=args.input,
                output_dir=args.output,
                config_path=args.config,
                db_path=args.db,
                should_fetch=not args.no_fetch,
                fetch_limit=args.fetch_limit,
                timeout=args.timeout,
                summary=summary,
            )
        summary["report_path"] = str(path)
    except LockBusy as exc:
        summary.update({"ok": False, "error": str(exc)})
        _write_summary(args.summary_json, summary)
        parser.exit(1, f"error: {exc}\n")
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
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
