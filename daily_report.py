#!/usr/bin/env python3
"""Generate a Chinese daily report from locally archived RSS Markdown files."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import rss_collector


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


def load_articles(input_dir: Path, report_date: str) -> list[Article]:
    articles: list[Article] = []
    if not input_dir.exists():
        return articles

    for path in sorted(input_dir.glob(f"*/{report_date}/*.md")):
        markdown = path.read_text(encoding="utf-8")
        metadata, body = parse_front_matter(markdown)
        articles.append(
            Article(
                title=metadata.get("title") or first_heading(body) or path.stem,
                feed=metadata.get("feed") or path.parent.parent.name,
                link=metadata.get("link") or "",
                published=metadata.get("published") or report_date,
                body=strip_source_footer(body),
                path=path,
            )
        )
    return articles


def first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def strip_source_footer(markdown: str) -> str:
    return re.sub(r"\n+Source: \[.*?\]\(.*?\)\s*$", "", markdown.strip(), flags=re.S)


def today_local() -> str:
    return dt.datetime.now().date().isoformat()


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"Invalid env line at {path}:{line_no}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid env key at {path}:{line_no}")
        values[key] = unquote_env_value(value)
    return values


def unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def config_value(name: str, file_values: dict[str, str]) -> str:
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value.strip()
    return file_values.get(name, "").strip()


def load_openai_config(env_file: Path = DEFAULT_ENV_FILE) -> OpenAIConfig:
    file_values = load_env_file(env_file)
    api_key = config_value("OPENAI_API_KEY", file_values)
    model = config_value("OPENAI_MODEL", file_values)
    base_url = (config_value("OPENAI_BASE_URL", file_values) or DEFAULT_BASE_URL).rstrip("/")
    missing = []
    if not api_key:
        missing.append("OPENAI_API_KEY")
    if not model:
        missing.append("OPENAI_MODEL")
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
    return OpenAIConfig(api_key=api_key, model=model, base_url=base_url)


def article_to_prompt(article: Article) -> str:
    body = article.body.strip()
    if len(body) > MAX_CHARS_PER_ARTICLE:
        body = body[:MAX_CHARS_PER_ARTICLE].rstrip() + "\n\n[内容已截断]"
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


def build_batches(articles: list[Article]) -> list[list[Article]]:
    batches: list[list[Article]] = []
    current: list[Article] = []
    current_size = 0
    for article in articles:
        size = len(article_to_prompt(article))
        if current and current_size + size > MAX_CHARS_PER_BATCH:
            batches.append(current)
            current = []
            current_size = 0
        current.append(article)
        current_size += size
    if current:
        batches.append(current)
    return batches


def chat_completion(config: OpenAIConfig, messages: list[dict[str, str]], timeout: int) -> str:
    payload = json.dumps(
        {
            "model": config.model,
            "messages": messages,
            "temperature": 0.2,
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Model request failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Model request failed: {exc}") from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected model response: {data}") from exc


def summarize_batch(config: OpenAIConfig, report_date: str, batch: list[Article], timeout: int) -> str:
    article_block = "\n\n---\n\n".join(article_to_prompt(article) for article in batch)
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
) -> Path:
    if should_fetch:
        rss_collector.collect(
            config=config_path,
            output=input_dir,
            db_path=db_path,
            timeout=timeout,
            limit=fetch_limit,
        )

    articles = load_articles(input_dir, report_date)
    if not articles:
        path = write_no_articles_report(output_dir, report_date, input_dir)
        print(f"No articles found for {report_date}. Wrote {path}.")
        return path

    config = load_openai_config()
    batches = build_batches(articles)
    if len(batches) == 1:
        source_material = "\n\n---\n\n".join(article_to_prompt(article) for article in articles)
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
    print(f"Wrote {path}.")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a daily RSS report with an OpenAI-compatible model.")
    parser.add_argument("--date", default=today_local(), help="Report date in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="RSS Markdown input directory.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Report output directory.")
    parser.add_argument("--config", type=Path, default=rss_collector.DEFAULT_CONFIG, help="RSS feed config file.")
    parser.add_argument("--db", type=Path, default=rss_collector.DEFAULT_DB, help="RSS deduplication SQLite file.")
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout in seconds.")
    parser.add_argument("--fetch-limit", type=int, help="Maximum entries to fetch per feed before reporting.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip RSS fetching and only analyze local Markdown.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.exit(2, "error: --date must use YYYY-MM-DD format\n")

    try:
        generate_report(
            report_date=args.date,
            input_dir=args.input,
            output_dir=args.output,
            config_path=args.config,
            db_path=args.db,
            should_fetch=not args.no_fetch,
            fetch_limit=args.fetch_limit,
            timeout=args.timeout,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
