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

## 使用 RSSHub 扩展 RSS 来源

如果目标网站没有原生 RSS，可以同时部署 RSSHub，让 RSSHub 负责把网站路由转换成 RSS，再由本项目采集。

本项目已提供 Docker Compose 配置：

```bash
cp .env.rsshub.example .env.rsshub
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml up -d
```

启动后访问：

```text
http://127.0.0.1:1200
```

把 RSSHub 路由写进 `feeds.txt`：

```text
DIYgod GitHub Activity | http://127.0.0.1:1200/github/activity/DIYgod
```

更多说明见 `docs/rsshub-deployment.md`，示例源见 `feeds.rsshub.example.txt`。

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

## 每日工作流

`daily_workflow.py` 会编排完整流程：

1. 拉取 `feeds.txt` 中的 RSS。
2. 生成前一天的日报。
3. 通过 SMTP 把日报正文和 Markdown 附件发送到邮箱。

在 `.env` 中补充邮件配置：

```bash
SMTP_HOST="smtp.example.com"
SMTP_PORT="465"
SMTP_USERNAME="your-email@example.com"
SMTP_PASSWORD="your-smtp-password"
SMTP_USE_SSL="true"
SMTP_USE_STARTTLS="false"
MAIL_FROM="your-email@example.com"
MAIL_TO="recipient@example.com"
MAIL_SUBJECT_PREFIX="RSS 日报"
```

手动运行完整流程：

```bash
python3 daily_workflow.py
```

默认会处理“昨天”的日报，并且每个 RSS 源最多拉取 100 条，避免首次运行时从大型 RSS 源回填过多历史内容。也可以指定日期：

```bash
python3 daily_workflow.py --date 2026-04-19
```

只采集和生成日报，不发送邮件：

```bash
python3 daily_workflow.py --no-send
```

macOS 每天早上 4 点定时运行可使用 `launchd/com.info-collector.daily.plist` 模板：

```bash
mkdir -p logs
cp launchd/com.info-collector.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.info-collector.daily.plist
```

定时任务日志会写入 `logs/`。

## GitHub Pages 在线查看

本项目可以把 `reports/*.md` 构建成静态 HTML，发布到 GitHub Pages。

生成静态站点：

```bash
python3 build_site.py
```

输出位置：

```text
docs/
  index.html
  reports/
    2026-04-22.html
```

在 GitHub 仓库设置里开启 Pages：

1. 进入 `Settings` -> `Pages`。
2. `Source` 选择 `Deploy from a branch`。
3. `Branch` 选择你的主分支，目录选择 `/docs`。
4. 保存后等待 GitHub Pages 发布。

本地生成并发布日报的推荐流程：

```bash
python3 daily_workflow.py --no-send
python3 build_site.py
git add reports docs
git commit -m "Add daily report YYYY-MM-DD"
git push
```
