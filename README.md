# RSS Markdown Collector

一个本地 RSS/Atom 收集器：把新条目保存为 Markdown，用 SQLite 去重，调用 OpenAI 兼容模型生成中文日报，并通过 SMTP 发送。项目仍可用 3 个 Python 脚本和 Docker Compose（RSSHub）直接运行；现在额外提供一个**仅监听本机**的可视化配置界面，统一管理订阅源、模型、邮件、路径、运行参数与定时任务。

## 快速开始

要求 Python 3.11+。配置界面只使用标准库：

```bash
python3 config_ui.py
```

启动后终端会打印类似：

```text
http://127.0.0.1:8765/?token=...
```

用这个带 `token` 的链接打开浏览器即可。服务只绑定 `127.0.0.1`，写入接口需要本次启动生成的 token；页面不会显示密钥明文。按 `Ctrl+C` 退出。

常用参数：

```bash
python3 config_ui.py --project-dir /path/to/info-collerctor
python3 config_ui.py --port 8877 --no-browser
```

## 配置入口与优先级

配置仍以文件为唯一真相源，界面只做读写、校验和运行编排：

| 配置 | 载体 | 读取方 |
| --- | --- | --- |
| RSS 源列表、启用开关、备注 | `feeds.txt` | 3 个脚本 |
| 模型 3 项 + 邮件 9 项（含密钥） | `.env` | `daily_report.py`、`daily_workflow.py`、`config_ui.py` |
| RSSHub 主机/端口/镜像/代理/token | `.env.rsshub` | `docker compose`、`config_ui.py` |
| 路径、超时、抓取条数、日期偏移、模型高级参数、定时时间 | `config.json` | 3 个脚本、`config_ui.py` |
| 每日执行 | `~/Library/LaunchAgents/com.info-collector.daily.plist`（由界面生成） | `launchd` |

生效优先级（从高到低）：

```text
CLI 参数 > 环境变量 > .env / .env.rsshub > config.json > 脚本默认常量
```

界面会显示每项的真实生效值和来源；如果同名环境变量正在覆盖界面设置，会明确提示。`.env` 中的模型/邮件密钥优先于 `config.json` 中的非敏感参数。

`config.json` 可以在首次保存时自动创建；参考 `config.json.example`。它不保存任何密钥，适合进版本管理。`.env`、`.env.rsshub`、数据库、归档和日志已被 `.gitignore` 排除。

## 可视化界面

左侧 5 个页签：

- **总览**：配置就绪度、最近运行、真实生效值与来源、快捷运行。
- **订阅源**：列表、启用/禁用、排序、增删改、导入/导出、测试抓取（只读）、重命名迁移。
- **集成配置**：模型预设与测试连接、SMTP 表单与发送测试邮件、RSSHub 配置/健康检查/容器操作。
- **运行**：仅采集 / 仅生成日报 / 完整工作流 / 仅构建站点；日期、是否抓取、是否发邮件；实时日志、结构化摘要、运行历史与停止。
- **系统**：路径与运行参数、配置体检、launchd/crontab、去重库维护、归档 Markdown 浏览、站点构建与确认式发布。

安全与写入保护：

- 仅监听 `127.0.0.1`；写接口需要启动时生成的一次性会话 token，并校验 `Host`/`Origin`。
- `.env` 保存后权限为 `600`；写前生成 `.bak` 与最多 5 份时间戳备份；使用临时文件 + `os.replace` 原子替换。
- 保留 `.env` / `.env.rsshub` 的注释与未识别键；`feeds.txt` 保留注释、空行和未知扩展列。
- 页面加载后文件被外部修改时，保存会返回冲突提示，避免覆盖别人的改动。
- 密钥默认掩码，只显示后 4 位；运行日志会替换已知密钥为 `****`。
- 首次启动如果检测到旧布局（存在 `feeds.txt` / `.env` / `.env.rsshub` 但没有 `config.json`），会先展示“将读取到的配置”预览，确认后才允许写入。

### 订阅源格式

`feeds.txt` 兼容原来的 `名称 | URL`，并新增向后兼容的追加列：

```text
# 注释和空行会被保留
Hacker News | https://news.ycombinator.com/rss
观察中的源 | https://example.com/rss | enabled=false | note=暂停观察
```

- `enabled=false`：在采集与日报环节跳过，不删除历史归档。
- `note=...`：仅用于展示。
- 未知的 `key=value` 列会原样保留。

URL 校验仅允许 `http`/`https`；重复检测忽略末尾 `/` 与主机大小写。归档目录名和去重主键使用名称生成的 slug，因此改名会影响历史。界面提供三种重命名方式：

1. 仅改显示名：历史目录和去重表仍用旧名称，会重复归档。
2. 同步迁移历史目录：移动目录并修正文件路径，但去重主键仍是旧名称，仍可能重复归档。
3. 同步重写去重表（推荐）：迁移目录并重写去重主键，避免重复归档；失败时回滚 `feeds.txt`。

### 模型/邮件测试

- **测试连接**：向 Base URL 的 `/chat/completions` 发一次最小请求，显示 HTTP 状态、耗时、错误原文和修改建议。
- **发送测试邮件**：直接使用表单当前值连接 SMTP，依次显示连接、STARTTLS、认证、发送阶段的错误，不需要先保存。
- 端口 465/587 会给出 SSL/STARTTLS 建议；允许覆盖，但会提示组合冲突。

## 命令行用法

CLI 行为保持可用，`feeds.txt`、`.env` 仍可手改。脚本默认值从 `config.json` 读取，仍可用参数临时覆盖。

采集：

```bash
python3 rss_collector.py --output ./rss-downloads --limit 100
# --limit 0 表示不限制；默认从 config.json 的 run.fetch_limit 读取（100）
```

生成日报（默认日期为今天减 `run.date_offset`，默认偏移 1 天）：

```bash
python3 daily_report.py --date 2026-04-16
python3 daily_report.py --date 2026-04-16 --no-fetch
```

完整工作流：

```bash
python3 daily_workflow.py                 # 采集 + 日报 + 邮件
python3 daily_workflow.py --no-send       # 不发邮件
python3 daily_workflow.py --no-fetch      # 不抓取，只用本地 Markdown
```

构建站点：

```bash
python3 build_site.py
```

所有脚本都支持 `--summary-json PATH`，会写出一份结构化运行摘要，供界面或外部监控读取。采集/日报/工作流使用 `logs/.run.lock` 文件锁；已有任务运行时，第二个进程会直接提示“已有任务在运行”而不是并发写入。

## 定时任务

不再维护仓库里写死旧路径的 plist 模板。在「系统」页填写执行时间后点击安装，界面会用项目真实绝对路径生成：

```text
~/Library/LaunchAgents/com.info-collector.daily.plist
```

并执行 `launchctl bootout` / `bootstrap`。界面会显示已安装、已加载、路径是否失效、下次执行时间与最近日志；项目移动后可一键“更新并重新加载”。

非 macOS 不会写入系统配置，只显示 crontab 示例，例如：

```text
0 4 * * * cd /path/to/info-collerctor && /usr/bin/python3 daily_workflow.py >> logs/daily_workflow.log 2>&1
```

## RSSHub

```bash
cp .env.rsshub.example .env.rsshub
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml up -d
```

RSSHub 的主机、端口、镜像、代理和 token 从 `.env.rsshub` 读取；`docker-compose.rsshub.yml` 中的代理已改为 `${RSSHUB_PROXY:-}`，不再硬编码。界面的「集成配置」可以编辑这些值、检查 `/healthz`、查看容器状态并启停容器。

更多说明见 `docs/rsshub-deployment.md`。

## GitHub Pages

```bash
python3 build_site.py
git add reports docs
git commit -m "Add daily report YYYY-MM-DD"
git push
```

界面「系统」页也提供构建、本地预览和确认式发布；发布只暂存 `reports/` 与 `docs/`，并会拦截 `.env`、`.env.rsshub`、`.sqlite3`、`rss-downloads/`、`logs/`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖配置优先级、`.env`/`feeds.txt` 保留注释与未知键、备份与冲突检测、重命名迁移、HTTP 写入鉴权、测试抓取、运行锁与归档读取。

## 目录结构

```text
config_ui.py          # 本地配置服务（标准库 http.server + 原生 JS）
web/                  # 单页界面：index.html / app.js / styles.css
settings.py           # 统一配置解析与来源标记
feed_config.py        # feeds.txt 解析、校验、渲染
env_file.py           # 保留注释的 .env 读写
config_store.py       # 备份、原子写、冲突检测、重命名迁移
runlock.py            # CLI 与界面共用的运行锁
config.json.example   # 非敏感配置示例
```
