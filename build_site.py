#!/usr/bin/env python3
"""Build a static GitHub Pages site from Markdown reports."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
SITE_DIR = ROOT / "docs"
SITE_REPORTS_DIR = SITE_DIR / "reports"
SITE_TITLE = "RSS 日报"


@dataclass(frozen=True)
class Report:
    date: str
    title: str
    markdown: str
    summary: str


def inline_markdown(value: str) -> str:
    text = html.escape(value)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: (
            f'<a href="{html.escape(match.group(2), quote=True)}">'
            f"{match.group(1)}</a>"
        ),
        text,
    )
    return text


def flush_paragraph(parts: list[str], paragraph: list[str]) -> None:
    if not paragraph:
        return
    parts.append(f"<p>{inline_markdown(' '.join(paragraph).strip())}</p>")
    paragraph.clear()


def close_list(parts: list[str], list_type: str | None) -> None:
    if list_type:
        parts.append(f"</{list_type}>")


def markdown_to_html(markdown: str) -> str:
    parts: list[str] = []
    paragraph: list[str] = []
    list_type: str | None = None

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush_paragraph(parts, paragraph)
            close_list(parts, list_type)
            list_type = None
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph(parts, paragraph)
            close_list(parts, list_type)
            list_type = None
            level = len(heading.group(1))
            parts.append(f"<h{level}>{inline_markdown(heading.group(2))}</h{level}>")
            continue

        unordered = re.match(r"^\s*[-*]\s+(.+)$", line)
        if unordered:
            flush_paragraph(parts, paragraph)
            if list_type != "ul":
                close_list(parts, list_type)
                parts.append("<ul>")
                list_type = "ul"
            parts.append(f"<li>{inline_markdown(unordered.group(1))}</li>")
            continue

        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if ordered:
            flush_paragraph(parts, paragraph)
            if list_type != "ol":
                close_list(parts, list_type)
                parts.append("<ol>")
                list_type = "ol"
            parts.append(f"<li>{inline_markdown(ordered.group(1))}</li>")
            continue

        paragraph.append(line.strip())

    flush_paragraph(parts, paragraph)
    close_list(parts, list_type)
    return "\n".join(parts)


def extract_title(markdown: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown, flags=re.M)
    return match.group(1).strip() if match else fallback


def extract_summary(markdown: str) -> str:
    match = re.search(r"##\s+今日概览\s+(.+?)(?:\n##\s+|\Z)", markdown, flags=re.S)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`#]", "", text)
    return text[:180] + ("..." if len(text) > 180 else "")


def load_reports() -> list[Report]:
    reports: list[Report] = []
    for path in sorted(REPORTS_DIR.glob("*.md"), reverse=True):
        markdown = path.read_text(encoding="utf-8")
        date = path.stem
        reports.append(
            Report(
                date=date,
                title=extract_title(markdown, f"{date} RSS 日报"),
                markdown=markdown,
                summary=extract_summary(markdown),
            )
        )
    return reports


def page(title: str, body: str, home_href: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - {SITE_TITLE}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ec;
      --accent: #0f766e;
      --accent-soft: #e6fffb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    a {{ color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .bar, main {{
      width: min(920px, calc(100% - 32px));
      margin: 0 auto;
    }}
    .bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 0;
    }}
    .brand {{
      color: var(--text);
      font-weight: 700;
      text-decoration: none;
    }}
    main {{ padding: 34px 0 56px; }}
    h1, h2, h3 {{ line-height: 1.3; letter-spacing: 0; }}
    h1 {{ margin: 0 0 20px; font-size: clamp(28px, 5vw, 42px); }}
    h2 {{ margin-top: 36px; padding-top: 20px; border-top: 1px solid var(--line); }}
    p, li {{ color: #263244; }}
    code {{ background: var(--accent-soft); padding: 2px 5px; border-radius: 4px; }}
    .meta {{ color: var(--muted); margin-top: -8px; }}
    .reports {{ display: grid; gap: 14px; margin-top: 24px; }}
    .report-card {{
      display: block;
      padding: 18px 20px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      text-decoration: none;
      color: inherit;
    }}
    .report-card:hover {{ border-color: var(--accent); }}
    .report-card strong {{ display: block; font-size: 18px; }}
    .report-card span {{ display: block; margin-top: 6px; color: var(--muted); }}
    article {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: clamp(20px, 4vw, 42px);
    }}
    article h1:first-child {{ margin-top: 0; }}
    @media (max-width: 640px) {{
      .bar {{ align-items: flex-start; flex-direction: column; }}
      main {{ padding-top: 24px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <a class="brand" href="{html.escape(home_href, quote=True)}">RSS 日报</a>
      <span>GitHub Pages</span>
    </div>
  </header>
  <main>
{body}
  </main>
</body>
</html>
"""


def build_index(reports: list[Report]) -> str:
    items = "\n".join(
        f"""      <a class="report-card" href="reports/{report.date}.html">
        <strong>{html.escape(report.title)}</strong>
        <span>{html.escape(report.summary or "查看这一天的 RSS 日报。")}</span>
      </a>"""
        for report in reports
    )
    body = f"""    <h1>RSS 日报</h1>
    <p class="meta">共 {len(reports)} 篇日报，按日期倒序排列。</p>
    <div class="reports">
{items}
    </div>"""
    return page("首页", body, "index.html")


def build_report(report: Report) -> str:
    body = f"""    <article>
{indent(markdown_to_html(report.markdown), 6)}
    </article>"""
    return page(report.title, body, "../index.html")


def indent(value: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in value.splitlines())


def main() -> int:
    reports = load_reports()
    if not reports:
        raise SystemExit("No reports found in reports/*.md")

    SITE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "index.html").write_text(build_index(reports), encoding="utf-8")
    for report in reports:
        (SITE_REPORTS_DIR / f"{report.date}.html").write_text(build_report(report), encoding="utf-8")

    print(f"Built {len(reports)} report page(s) in {SITE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
