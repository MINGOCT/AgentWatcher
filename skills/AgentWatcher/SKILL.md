---
name: AgentWatcher
description: 当用户需要通过 Bark 将 Codex 的完成、待批准、需要注意、远程详情页或手机回复通知发送到 iPhone 和 Apple Watch，或排查漏通知、重复通知、旧通知、乱码通知时使用。
---

# AgentWatcher

AgentWatcher 是 Codex 的本地通知插件。它通过 Bark 把 Codex 的待批准、任务完成、需要注意等事件推送到 iPhone，并可同步到 Apple Watch。

项目地址：https://github.com/MINGOCT/AgentWatcher

## 默认原则

只发送有行动价值的通知：

- Codex 等待用户批准或决定。
- 长时间任务完成。
- 任务失败、卡住或需要用户回来处理。
- 用户明确要求某个构建、测试或脚本结束后通知。

不要把完整代码、完整日志、完整对话、`.env`、API key、SSH key、Bark key、access token 或长输出放进 Bark 正文。

`PostToolUse` 中间测试命令通知默认关闭。测试命令结束只代表某个工具命令完成，不代表整轮 Codex 任务结束；只有用户明确需要每个测试命令结束都推送时，才把 `notification_policy.notify_on_test_done` 设为 `true`。

## 快速配置

在插件根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup_agentwatcher.ps1" -BarkUrl "https://api.day.app/<YOUR_BARK_KEY>/"
```

启动后台 watcher：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start_codex_bark_watcher.ps1"
```

开机自启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install_watcher_startup.ps1"
```

检查配置：

```powershell
python scripts/codex_bark_notify.py doctor
```

发送测试通知：

```powershell
python scripts/codex_bark_notify.py test
```

## Web Console

Web Console 默认由本机服务提供：

```text
http://127.0.0.1:8765/console
```

Console 用来查看和调整：

- Bark 是否已配置。
- Bark 通知头像。
- watcher 是否运行。
- Web 公网地址。
- 只读 / 可回复模式。
- 手机回复自动同步频率。
- Codex 自动任务应用状态。
- 测试命令通知开关。
- 待处理手机回复数量。
- 最近事件摘要。

Console 只展示安全摘要，不显示 Bark key、access token、signature、自定义回复正文或完整 assistant 输出。

待处理手机回复数量和 Codex 自动任务应用状态会自动刷新。远程模式和测试命令通知开关保存后立刻写入配置；手机回复自动同步频率保存后会先进入“待应用到 Codex 自动任务”，只有当前 Codex 会话真实更新 heartbeat 后才显示“已应用”。

## Bark 通知头像

Bark 的通知头像使用 `icon` 参数，值必须是手机可访问的图片 URL。AgentWatcher 默认使用 OpenAI Codex 图标的 Wikimedia 远程图片链接；Web Console 支持上传 PNG、JPG、WEBP、GIF，最大 2 MB，上传后会覆盖默认头像。

发送 Bark 通知时，头像必须放在 Bark 请求地址的 `?icon=...` 参数里，例如 `https://api.day.app/<BarkToken>?icon=<图标URL>`。不要只把 `icon` 放进通知正文 JSON；部分 Bark 链路会忽略正文里的头像字段。

上传后的头像保存在：

```text
~/.codex-bark-notify/assets/
```

不要把头像文件提交到 GitHub 或打进插件包。默认头像只保存远程 URL，不复制图片文件。默认头像是公网可访问的 Wikimedia 图片，不需要配置 `web.public_base_url`。上传自定义头像后，AgentWatcher 会自动生成 `/assets/icon.<ext>?v=<version>` URL，避免 Bark 缓存旧头像；手机不在同一局域网时必须先配置 `web.public_base_url`。

如果用户说通知头像没有变化，检查核心脚本是否把头像编码到请求 URL 的 `icon` query 参数、watcher 是否已重启、自定义头像 URL 是否能被手机访问，以及 Bark/iOS 是否缓存了旧头像。

## Desktop Watcher

Codex Desktop 不一定会在所有纯聊天任务或新对话里触发 `Stop` hook。watcher 会监听 `~/.codex/sessions` 中新增的 `task_complete` JSONL 事件，并自动发送完成通知。

watcher 会把文件偏移记录在：

```text
~/.codex-bark-notify/state.json
```

保护策略：

- 默认只发送最近 30 分钟内产生的 `task_complete`。
- 旧事件会记录为 `skipped_stale_task_complete`。
- 路径日期超过 7 天的 session 文件会记录为 `skipped_old_session_file`。
- 7 天阈值由 `notification_policy.max_session_file_age_seconds` 控制，设为 `0` 可关闭。

## 手机详情页和回复

完成通知可以带详情页链接。详情页显示：

- 简短通知摘要。
- Markdown 渲染的完整结果。
- 原始文本。
- 手机回复自动同步状态。
- 只读 / 可回复模式。
- 快捷操作和自定义回复框。

所有快捷操作和自定义回复都必须二次确认后才会写入本地队列：

```text
~/.codex-bark-notify/actions.jsonl
```

队列项只表示用户意图，不自动执行系统命令。Codex 读取后仍需按当前上下文判断是否安全、是否需要再次确认。

手机回复必须回到原通知对应的 Codex 会话。处理队列时必须按当前 `CODEX_THREAD_ID` 过滤，不能把 A 会话的手机回复投递到 B 会话，也不能为了处理回复新建会话。

查看当前会话待处理回复：

```powershell
python scripts/codex_bark_notify.py actions
```

处理前标记派发：

```powershell
python scripts/codex_bark_notify.py actions --thread-id <thread_id> --mark-dispatched <action_id>
```

## 远程交互模式

公网访问时可切换两种模式：

```text
read_only：只查看结果，不显示快捷操作和自定义回复；服务端拒绝写入。
reply：允许快捷操作和自定义回复；写入前仍需二次确认。
```

命令：

```powershell
python scripts/codex_bark_notify.py remote-mode --read-only
python scripts/codex_bark_notify.py remote-mode --reply
```

## 手机回复自动同步

自动同步依赖 Codex App 的同线程 heartbeat，会定时唤醒 Codex，因此会消耗 token。默认关闭。

可选频率：

```text
关闭 / 5 / 10 / 15 / 30 / 60 分钟
```

命令：

```powershell
python scripts/codex_bark_notify.py reply-heartbeat --interval 15
python scripts/codex_bark_notify.py reply-heartbeat --off
```

查看待应用到 Codex heartbeat 的请求：

```powershell
python scripts/codex_bark_notify.py automation-sync --format json
```

若返回 `pending=true`，需要把返回的 `rrule` 应用到当前同线程 heartbeat 自动化。应用成功后标记：

```powershell
python scripts/codex_bark_notify.py automation-sync --mark-applied <request_id> --automation-id <automation_id> --format json
```

开启后默认最多持续 120 分钟。超过时限后页面应显示已自动暂停，Codex 应停止或删除对应 heartbeat，避免长期消耗 token。

## 公网安全

如果把 Web Console 暴露到公网，优先使用 HTTPS、Cloudflare Tunnel、Tailscale 或其他受控入口，不建议长期裸露 HTTP 端口。

详情页、快捷操作和自定义回复链接使用 HMAC 签名。URL 只包含 `detail_id`、`expires` 和 `signature`，服务端必须校验路径、过期时间和签名。不要把 `access_token` 明文放进 URL。

审计日志位置：

```text
~/.codex-bark-notify/web_audit.jsonl
```

审计日志只记录 IP、时间、detail_id、action、请求路径和结果，不记录 access token、signature 或自定义回复正文。

## 常见排查

如果没收到完成通知：

1. 打开 `/console` 查看 watcher 是否运行、Bark 是否配置。
2. 查看 `~/.codex-bark-notify/codex_bark_events.jsonl`。
3. 如果看到 `skipped_stale_task_complete`，说明事件时间太旧。
4. 如果看到 `skipped_old_session_file`，说明 session 文件路径日期超过阈值。
5. 如果看到 `PostToolUse` 且 `reason=disabled`，这是中间工具命令通知被默认关闭，属于正常行为。

如果收到太多测试通知：

- 在 `/console` 关闭“测试命令通知”。
- 或设置 `notification_policy.notify_on_test_done=false`。
