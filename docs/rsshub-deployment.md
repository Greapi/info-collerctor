# RSSHub 部署指南

本项目可以把 RSSHub 作为本地 RSS 生成服务使用，再由 `rss_collector.py`
读取 RSSHub 输出的 RSS 地址并保存为 Markdown。

## 方案

- RSSHub: 提供 RSS 路由，默认监听 `127.0.0.1:1200`。
- Redis: RSSHub 缓存，减少重复请求和被上游限流的概率。
- browserless: 给需要浏览器渲染的 RSSHub 路由使用。

RSSHub 官方文档推荐 Docker Compose 部署，并说明 Docker 镜像包括
`diygod/rsshub` 与 `ghcr.io/diygod/rsshub`，Compose 方案可以同时带上 Redis
和 browserless。

参考：

- https://docs.rsshub.app/zh/deploy/
- https://docs.rsshub.app/deploy/

## 首次启动

准备环境变量文件：

```bash
cp .env.rsshub.example .env.rsshub
```

启动服务：

```bash
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml up -d
```

查看状态：

```bash
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml ps
```

健康检查：

```bash
curl http://127.0.0.1:1200/healthz
```

浏览器访问：

```text
http://127.0.0.1:1200
```

## 接入采集脚本

RSSHub 的路由本质上也是 RSS 地址。把需要的路由写进 `feeds.txt` 即可：

```text
DIYgod GitHub Activity | http://127.0.0.1:1200/github/activity/DIYgod
```

也可以先查看 `feeds.rsshub.example.txt`，选择需要的条目复制到 `feeds.txt`。

注意：RSSHub 的不同路由需要的配置不同。比如当前版本的
`/github/trending/:since/:language/:spoken_language?` 需要
`GITHUB_ACCESS_TOKEN`，否则会返回 503 和 `ConfigNotFoundError`。遇到这类错误时，
先查对应路由文档，把需要的 token、cookie 或 API key 写进 `.env.rsshub` 后重启。

GitHub Trending 示例：

```env
GITHUB_ACCESS_TOKEN=ghp_your_token_here
```

```bash
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml up -d
```

```text
GitHub Trending Python | http://127.0.0.1:1200/github/trending/daily/python
```

采集：

```bash
python3 rss_collector.py --config feeds.txt --output ./rss-downloads
```

生成日报：

```bash
python3 daily_report.py --date 2026-04-20
```

完整工作流：

```bash
python3 daily_workflow.py --date 2026-04-20
```

## 端口和访问范围

默认只绑定本机：

```env
RSSHUB_HOST=127.0.0.1
RSSHUB_PORT=1200
```

如果部署在服务器并需要外部访问，可以改成：

```env
RSSHUB_HOST=0.0.0.0
RSSHUB_PORT=1200
```

外部开放前建议放到 Nginx/Caddy 后面，并加访问控制或防火墙规则。RSSHub
可能会访问第三方站点，公开实例也更容易触发上游限流。

## 更新

拉取镜像并重启：

```bash
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml pull
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml up -d
```

如需可复现部署，把 `.env.rsshub` 里的 `RSSHUB_IMAGE` 从 `latest` 改为固定日期
或固定 commit tag。

## 停止和清理

停止服务：

```bash
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml down
```

停止并删除 Redis 缓存卷：

```bash
docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml down -v
```

## 常见问题

1. `curl /healthz` 不通

   先看容器状态和日志：

   ```bash
   docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml ps
   docker compose --env-file .env.rsshub -f docker-compose.rsshub.yml logs --tail=100 rsshub
   ```

2. 某些路由返回错误

   先打开 RSSHub 对应路由文档，确认是否需要 token、cookie、API key 或
   browserless。需要的配置写进 `.env.rsshub` 后重启。

3. macOS 上端口被占用

   修改 `.env.rsshub`：

   ```env
   RSSHUB_PORT=1201
   ```

   然后把 `feeds.txt` 中的地址同步改成 `http://127.0.0.1:1201/...`。
