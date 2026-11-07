# Config UI visual evidence

这些截图用于 PR 审查，不是产品页面。

- **Before**：改动前基线（`568f1bc`）没有图形界面，配置分散在 `feeds.txt`、`.env.example` 与
  `launchd/com.info-collector.daily.plist`。截图直接渲染自该提交中的真实文件。
- **After**：改动后运行 `python3 config_ui.py` 的实际界面截图；演示项目包含示例订阅源、归档、
  日报和运行历史，数据均为本地示例，密钥只显示掩码。
- **After dark**：顶栏主题按钮切换到深色模式后的实际截图；刷新页面后主题从 `localStorage` 恢复。

运行方式：

```bash
python3 config_ui.py --project-dir <演示项目> --no-browser
```

截图环境：macOS，Chrome headless，1440px 宽；系统页为整页截图。
