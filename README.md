# RSS Markdown Collector

一个本地 RSS/Atom 收集脚本，会把新条目保存为 Markdown 文件，并用 SQLite 记录已保存内容，避免重复下载。

## 使用

编辑 `feeds.txt`：

```text
Hacker News | https://news.ycombinator.com/rss
Example | https://example.com/feed.xml
```

运行：

```bash
python3 rss_collector.py --output /path/to/your/folder
```

常用参数：

```bash
python3 rss_collector.py \
  --config feeds.txt \
  --output ./rss-downloads \
  --db .rss_collector.sqlite3 \
  --limit 20
```

输出目录结构：

```text
rss-downloads/
  hacker-news/
    2026-04-17/
      example-title.md
```

Markdown 文件会包含 YAML front matter、标题、RSS 条目正文/摘要，以及原始链接。

## 配置格式

`feeds.txt` 支持注释和空行：

```text
# 名称 | RSS 地址
My Feed | https://example.com/rss.xml

# 也可以只写 URL
https://example.org/feed.atom
```

## 去重

脚本会把每个条目的 `guid`、Atom `id` 或链接写入 `.rss_collector.sqlite3`。再次运行时，只保存新条目。

## 生成日报

日报脚本会默认先拉取最新 RSS，再读取当天保存的 Markdown，调用兼容 OpenAI Chat Completions 协议的模型生成中文结构化日报。

模型配置可以写在项目根目录 `.env`：

```bash
OPENAI_API_KEY="你的 API Key"
OPENAI_MODEL="deepseek-chat"
OPENAI_BASE_URL="https://api.deepseek.com"
```

也可以继续使用同名环境变量临时覆盖 `.env`。

生成指定日期日报：

```bash
python3 daily_report.py --date 2026-04-16
```

只分析本地已有 Markdown，不重新拉取 RSS：

```bash
python3 daily_report.py --date 2026-04-16 --no-fetch
```

默认输出：

```text
reports/
  2026-04-16.md
```

可覆盖输入和输出目录：

```bash
python3 daily_report.py \
  --date 2026-04-16 \
  --input ./rss-downloads \
  --output ./reports
```
