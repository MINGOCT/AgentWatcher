# AgentWatcher 中文使用说明

`AgentWatcher` 是一个 Codex 本地通知插件。它通过 Bark 把 Codex 的待批准、任务完成、需要注意等事件推送到 iPhone，并可同步到 Apple Watch。

项目地址：https://github.com/MINGOCT/AgentWatcher

AgentWatcher 适合这些场景：

- Codex 跑完长任务后自动通知你。
- Codex 等待批准或需要你回来处理时提醒你。
- 在手机上查看完整结果，并把回复写回原 Codex 会话。
- 通过 Web Console 查看状态、调整通知和远程回复设置。

默认通知会压缩成适合锁屏和手表阅读的短摘要，不会把完整代码、完整日志或敏感 token 放进 Bark 正文。

## 快速开始

### 1. 准备 Bark

在 iPhone 安装并打开 Bark，允许通知，然后复制 Bark 首页的推送地址或 device key。

完整 Bark 地址通常长这样：

```text
https://api.day.app/<YOUR_BARK_KEY>/
```

也可以只复制 key：

```text
xxxxxxxx
```

### 2. 配置 AgentWatcher

在 PowerShell 进入插件目录：

```powershell
cd "$HOME\plugins\AgentWatcher"
```

运行一行配置命令，把 `<YOUR_BARK_KEY>` 换成你的 Bark key 或完整 Bark URL：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup_agentwatcher.ps1" -BarkUrl "https://api.day.app/<YOUR_BARK_KEY>/"
```

这条命令会：

- 把 Bark 配置写入 `~/.codex-bark-notify/config.json`。
- 发送一条测试通知。

不要把 `config.json`、Bark key、完整 Bark URL 提交到 GitHub。

### 3. 开启自动完成通知

启动后台 watcher：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start_codex_bark_watcher.ps1"
```

安装开机自启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_watcher_startup.ps1"
```

从这一步开始，Codex 任务完成后可以自动推送，不需要每次在对话里说“跑完通知我”。

## Python 要求

当前版本需要电脑上有 Python 3.8+ 可用，因为通知逻辑由 Python 脚本执行。

普通用户通常不需要手动运行 `python ...` 命令。PowerShell 脚本会按顺序自动查找：

- `CODEX_BARK_NOTIFY_PYTHON` 指定的 Python。
- `python`。
- `python3`。
- `py`。

如果系统找不到 Python，请安装 Python 3，或设置：

```powershell
[Environment]::SetEnvironmentVariable("CODEX_BARK_NOTIFY_PYTHON", "<python.exe 的完整路径>", "User")
```

重新打开 Codex 或 PowerShell 后再运行配置命令。

## Web Console

启动 watcher 后，本机默认提供 Web Console：

```text
http://127.0.0.1:8765/console
```

Web Console 可以查看和调整：

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

页面不会展示 Bark key、access token、signature、自定义回复正文或完整任务输出。

### 哪些设置实时生效

- 远程模式：保存后立刻写入配置，新的 `/action` 和 `/reply` 请求会按新模式处理。
- 测试命令通知开关：保存后立刻写入配置，后续 hook / watcher 读取新配置。
- 待处理手机回复数量：页面会自动刷新。
- Codex 自动任务状态：页面会自动刷新。
- 手机回复自动同步频率：保存后先进入“待应用到 Codex 自动任务”。只有当前 Codex 会话真实更新 heartbeat 后，才会显示“已应用到 Codex 自动任务”。

### Bark 通知头像

Bark 支持用 `icon` 参数设置通知头像。AgentWatcher 默认使用 OpenAI Codex 图标的 Wikimedia 远程图片链接作为通知头像，也可以在 Web Console 上传自己的头像覆盖默认值。

发送 Bark 通知时，AgentWatcher 会把头像 URL 放在 Bark 请求地址的 `?icon=...` 参数里，而不是放在通知正文里。例如：

```text
https://api.day.app/<YOUR_BARK_KEY>?icon=<图标URL>
```

使用限制：

- 支持 PNG、JPG、WEBP、GIF。
- 单个文件最大 2 MB。
- 默认头像只保存远程 URL，不会把图标文件放进插件压缩包。
- 头像文件保存在 `~/.codex-bark-notify/assets/`，不会放进插件压缩包。
- 默认头像是公网可访问的 Wikimedia 图片，不需要配置 `web.public_base_url`。
- 上传自定义头像后，Bark 需要能访问头像 URL；如果手机不在同一局域网，请先配置 `web.public_base_url`。
- 上传自定义头像后，AgentWatcher 会给头像 URL 自动加 `?v=...`，避免 Bark 缓存旧头像。

如果上传头像后通知仍显示旧头像，优先检查：

- 是否已重启 watcher，让后台进程加载新版脚本。
- `web.public_base_url` 是否是手机可以访问的地址。
- Bark 或 iOS 是否缓存了旧头像；重新上传头像会生成新的 `?v=...`。
- 核心脚本是否已经更新到会把头像放在 Bark 请求地址的 `?icon=...` 参数里。

## 自动通知如何工作

AgentWatcher 有两层通知机制：

1. **Codex hooks**：当 Codex 触发 `PermissionRequest`、`Stop`、`PostToolUse` 时发送通知。
2. **Desktop watcher**：监控 `~/.codex/sessions` 里的 Codex 会话 JSONL，发现新的 `task_complete` 事件后发送完成通知。

watcher 启动时会先把已有 session 文件记录为基线，只监听之后新增的内容，避免把历史任务重复推送。

watcher 默认只发送最近 30 分钟内产生的 `task_complete`。如果本地状态文件异常、会话文件被重写或 watcher 重新扫到很久以前的完成事件，AgentWatcher 会跳过推送，并在 `codex_bark_events.jsonl` 中记录 `skipped_stale_task_complete`。

Codex 有时会把今天的新完成事件追加到很早以前创建的 session 文件里。为了避免旧会话文件看起来像“旧版本通知”，watcher 默认跳过路径日期超过 7 天的 session 文件，并记录 `skipped_old_session_file`。阈值由 `notification_policy.max_session_file_age_seconds` 控制；设为 `0` 可关闭这层保护。

`PostToolUse` 中间测试命令通知默认关闭。否则 Codex 在任务过程中每跑一次 `pytest`、`npm test` 等命令都会推送“测试完成”，这不代表整轮任务已经结束，容易打扰用户。

确实需要每个测试命令结束都通知时，可以在 Web Console 打开开关，或在 `config.json` 里设置：

```json
{
  "notification_policy": {
    "notify_on_test_done": true
  }
}
```

## 手机详情页和自定义回复

完成通知可以带一个详情页链接。点开后可以看到完整结果、快捷操作和自定义回复框。

详情页会显示：

```text
手机回复自动同步：关闭 / 每 N 分钟检查一次 / 已自动暂停
远程交互模式：只读 / 可回复
```

详情页和回复入口默认有效期为 60 分钟。过期后，详情页、快捷操作和自定义回复都会提示已过期。

详情页、快捷操作和自定义回复链接会使用 HMAC 签名。URL 只包含 `detail_id`、`expires` 和 `signature`，不会把本机 `access_token` 明文放进链接。服务端会校验签名、过期时间和请求路径，避免别人猜测或篡改 action。

完整结果会同时显示 Markdown 渲染视图和原始文本。Markdown 渲染会先转义 HTML，只支持安全的常见格式，不执行脚本。

快捷操作包括：

```text
继续 / 重试 / 停止 / 稍后处理
```

自定义回复可以输入任意文字，例如：

```text
请继续修复刚才失败的测试，并完成后再通知我。
```

为了避免误触，快捷操作和自定义回复都需要二次确认。确认后内容会写入：

```text
~/.codex-bark-notify/actions.jsonl
```

这些内容只是本地队列，不会自动执行系统命令。Codex 读取后仍会按当前上下文判断如何处理。

手机回复必须回到原通知对应的 Codex 会话。AgentWatcher 会给每条回复记录：

```text
detail_id
turn_id
thread_id
source_path
source_offset
```

查看当前会话的待处理回复：

```powershell
python .\scripts\codex_bark_notify.py actions
```

查看全部待处理回复：

```powershell
python .\scripts\codex_bark_notify.py actions --all
```

如果要把手机回复交给 Codex 继续处理，必须投递到队列里的 `thread_id` 对应的原会话，不能新建会话。

## 只读 / 可回复模式

如果你把 Web Console 暴露到公网，但只想远程查看结果，可以切到只读模式。只读模式下：

- 详情页不显示快捷操作。
- 详情页不显示自定义回复框。
- 服务端会拒绝手工构造的 `/action` 和 `/reply` 写入请求。
- 不会写入 `actions.jsonl`。

切到只读模式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\set_remote_mode.ps1" -ReadOnly
```

切回可回复模式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\set_remote_mode.ps1" -Reply
```

查看当前模式：

```powershell
python .\scripts\codex_bark_notify.py remote-mode
```

## 手机回复自动同步

AgentWatcher 的任务完成通知和本地 watcher 不会消耗 Codex token。只有“手机回复自动同步”会消耗 token，因为它依赖 Codex App 的同线程 heartbeat 定时唤醒当前会话检查手机回复队列。

默认自动同步是关闭的。可选频率：

```text
关闭 / 5 / 10 / 15 / 30 / 60 分钟
```

推荐值是 15 或 30 分钟。频率越高，手机回复越快被处理，但待机 token 消耗也越高。

为了避免用户忘记关闭导致长期消耗 token，自动同步开启后默认最多持续 120 分钟。超过 120 分钟后，AgentWatcher 会显示“已自动暂停”，并要求 Codex 停止或删除对应 heartbeat。

### 设置频率

在 Web Console 里可以直接选择频率。也可以在插件目录运行：

```powershell
cd "$HOME\plugins\AgentWatcher"
```

开启 15 分钟检查一次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\set_reply_heartbeat.ps1" -Interval 15
```

关闭自动同步：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\set_reply_heartbeat.ps1" -Off
```

查看当前配置：

```powershell
python .\scripts\codex_bark_notify.py reply-heartbeat
```

### 应用到 Codex 自动任务

Web Console 或命令行修改自动同步频率后，AgentWatcher 会先保存目标设置，并生成一个待应用请求。查看请求：

```powershell
python .\scripts\codex_bark_notify.py automation-sync --format json
```

如果返回 `pending=true`，Codex 需要把返回的 `rrule` 应用到当前同线程 heartbeat 自动化。应用成功后标记为已应用：

```powershell
python .\scripts\codex_bark_notify.py automation-sync --mark-applied <request_id> --automation-id <automation_id> --format json
```

Console 会区分两种状态：

```text
待应用到 Codex 自动任务：配置已保存，但真实 heartbeat 还没更新。
已应用到 Codex 自动任务：真实 heartbeat 已按该频率运行。
```

当自动同步关闭或已自动暂停时，手机详情页和二次确认页会明显提示：

```text
自动同步当前已关闭。确认后内容只会记录在本地队列，不会自动发送到 Codex 执行。
```

二次确认按钮也会改成：

```text
仅记录，不会自动发送
```

## 公网访问

如果手机不在同一个局域网，可以把电脑的 `8765` 端口映射到公网，然后在配置里设置公网地址。

配置文件：

```text
~/.codex-bark-notify/config.json
```

示例：

```json
{
  "web": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8765,
    "public_base_url": "https://your-domain.example.com",
    "access_token": "保留现有值"
  }
}
```

如果使用公网 IP：

```json
"public_base_url": "http://你的公网IP:8765"
```

改完后重启 watcher：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop_codex_bark_watcher.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start_codex_bark_watcher.ps1"
```

安全建议：

- 优先使用 HTTPS 域名、Cloudflare Tunnel、Tailscale 或其他受控入口。
- 不建议长期裸露 HTTP 端口。
- `access_token` 会作为 HMAC 签名密钥保存在本机配置中，不要提交到 GitHub，也不要发给别人。
- 如果只想远程看结果，建议切到只读模式。

## 常用命令

发送测试通知：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\test_agentwatcher.ps1"
```

只检查 Python 是否可用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\resolve_python.ps1"
```

检查 Bark 配置：

```powershell
python .\scripts\codex_bark_notify.py doctor
```

查看最近事件日志：

```powershell
python .\scripts\codex_bark_notify.py logs --tail 20
```

停止 watcher：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop_codex_bark_watcher.ps1"
```

卸载开机自启：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\uninstall_watcher_startup.ps1"
```

## 运行目录

个人配置和运行日志默认在：

```text
~/.codex-bark-notify
```

Codex 会话目录默认在：

```text
~/.codex/sessions
```

可以用环境变量覆盖：

```text
CODEX_BARK_NOTIFY_DATA
CODEX_BARK_NOTIFY_SESSIONS
CODEX_BARK_NOTIFY_PYTHON
```

常见运行文件：

```text
~/.codex-bark-notify/config.json
~/.codex-bark-notify/watcher.pid
~/.codex-bark-notify/web.pid
~/.codex-bark-notify/watcher.log
~/.codex-bark-notify/watcher.err.log
~/.codex-bark-notify/web.log
~/.codex-bark-notify/web.err.log
~/.codex-bark-notify/codex_bark_events.jsonl
~/.codex-bark-notify/actions.jsonl
~/.codex-bark-notify/web_audit.jsonl
~/.codex-bark-notify/assets/
```

## 排查

### 没收到完成通知

优先检查：

1. 打开 `/console`，确认 Bark 已配置、watcher 正在运行。
2. 运行 `python .\scripts\codex_bark_notify.py doctor`。
3. 查看 `~/.codex-bark-notify/codex_bark_events.jsonl`。
4. 如果看到 `skipped_stale_task_complete`，说明事件时间太旧。
5. 如果看到 `skipped_old_session_file`，说明 session 文件路径日期超过阈值。
6. 如果看到 `PostToolUse` 且 `reason=disabled`，这是中间工具命令通知被默认关闭，属于正常行为。

### 收到太多测试通知

- 在 Web Console 关闭“测试命令通知”。
- 或设置 `notification_policy.notify_on_test_done=false`。

### Apple Watch 收不到

优先检查：

- iPhone `设置 -> 通知 -> Bark`：确认允许通知。
- iPhone `Watch` App：确认 Bark 通知会镜像到 Apple Watch。
- iPhone 专注模式：确认没有屏蔽 Bark。
- Bark App：首页 key 没有复制错。
- AgentWatcher：重新运行快速配置命令，看 iPhone 是否收到测试通知。

插件默认把待批准通知用 Bark 的 `timeSensitive` 级别发送，普通完成通知用 `active` 级别发送。是否能突破专注模式，仍取决于 iOS 设置。

## 自建 Bark Server

如果使用自建 Bark Server，配置时直接填完整 URL：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup_agentwatcher.ps1" -BarkUrl "https://bark.example.com/<YOUR_BARK_KEY>/"
```

## 隐私和安全

AgentWatcher 默认只发送简短摘要，不发送完整代码、完整日志、完整对话、`.env`、API key、SSH key 或 Bark key。事件日志也会对常见 token 做脱敏。

审计日志位置：

```text
~/.codex-bark-notify/web_audit.jsonl
```

审计日志只记录 IP、时间、detail_id、action、请求路径和结果，不记录 `access_token`、`signature` 或自定义回复正文。

注意：Bark 推送内容会经过 Bark 服务端。如果内容敏感，建议使用自建 Bark Server。
