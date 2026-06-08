# AgentWatcher 中文使用说明

`AgentWatcher` 通过 Bark 把 Codex 的待批准、任务完成、测试完成等通知推送到 iPhone，并可同步到 Apple Watch。

## 快速使用

1. 在 iPhone 安装 Bark，允许通知。
2. 复制 Bark 首页的推送 URL 或 device key。
3. 在 PowerShell 进入插件目录并运行配置命令。

```powershell
cd "$HOME\plugins\AgentWatcher"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup_agentwatcher.ps1" -BarkUrl "https://api.day.app/YOUR_KEY/"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start_codex_bark_watcher.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_watcher_startup.ps1"
```

把 `YOUR_KEY` 换成你的 Bark key。也可以填完整自建 Bark URL，例如：

```text
https://bark.example.com/YOUR_KEY/
```

配置会写入：

```text
~/.codex-bark-notify/config.json
```

## 需要 Python 吗

需要。当前版本依赖 Python 3.8+ 执行通知脚本。

但普通用户不需要手动运行 `python ...` 命令；PowerShell 脚本会自动查找 Python。如果找不到，请安装 Python 3，或设置：

```powershell
[Environment]::SetEnvironmentVariable("CODEX_BARK_NOTIFY_PYTHON", "<python.exe 的完整路径>", "User")
```

## 自动通知

启动 watcher 后，Codex 任务完成会自动推送，不需要每次在对话里要求通知。

通知正文默认是紧凑三行：

```text
已完成：任务短名
结果：已完成/测试通过/可能失败
动作：回来验收
```

## 常用命令

停止 watcher：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop_codex_bark_watcher.ps1"
```

卸载开机自启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\uninstall_watcher_startup.ps1"
```

检查 Python：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\resolve_python.ps1"
```

发送测试通知：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\test_agentwatcher.ps1"
```
