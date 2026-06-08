---
name: AgentWatcher
description: Use when the user wants compact Codex progress, approval, completion, test, or attention notifications sent through Bark to iPhone or Apple Watch; includes setup, automatic Desktop watcher fallback, test push, safe message formatting, and manual notification during long-running tasks.
---

# AgentWatcher

当用户希望 Codex 通过 Bark 把进度、审批、任务完成、测试完成或需要注意的事件推送到 iPhone / Apple Watch 时，使用这个 Skill。

项目地址：https://github.com/MINGOCT/AgentWatcher

## 使用原则

只在有行动价值的场景发送通知：

- Codex 等待用户批准或决定。
- 长时间任务完成。
- 常见测试命令完成。
- 用户明确要求某个构建、测试或脚本结束后通知。
- 连续失败、可能卡住或需要用户回来处理。

通知正文保持简短，优先控制在 96 个字符以内。不要发送完整代码、完整日志、完整对话、`.env`、API key、SSH key、Bark key 或长输出。

## 工作流程

1. 如果不确定是否已配置，在插件根目录运行：
   `python scripts/codex_bark_notify.py doctor`
2. 配置 Bark URL 或 key：
   `python scripts/codex_bark_notify.py setup --bark-url "<Bark URL 或 key>"`
3. 设置或验证时发送测试通知：
   `python scripts/codex_bark_notify.py test`
4. 在 Codex Desktop 中启用自动任务完成通知，确保 watcher 正在运行：
   `powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/start_codex_bark_watcher.ps1"`
5. 需要开机自启时，安装 watcher 快捷方式：
   `powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/install_watcher_startup.ps1"`

## Desktop Watcher

Codex Desktop 不一定会在所有纯聊天任务或新对话里触发 `Stop` hook。watcher 会监听 `~/.codex/sessions` 中新增的 `task_complete` JSONL 事件，并自动发送完成通知。

watcher 会把文件偏移记录在：

```text
~/.codex-bark-notify/state.json
```

启动时会先把已有 session 文件作为基线，只处理之后追加的新内容，避免历史任务重复推送。

## 紧凑摘要

不要把最后一条完整 assistant 回复塞进通知。优先使用下面这种适合锁屏和 Apple Watch 的三行摘要：

```text
已完成：任务短名
结果：已完成/测试通过/可能失败
动作：回来验收
```

详细原始事件写入日志，不进入 Bark 正文：

```text
~/.codex-bark-notify/codex_bark_events.jsonl
```

## 手动通知

用户明确要求通知时，可以使用 `send`：

```powershell
python scripts/codex_bark_notify.py send --event done --title "AgentWatcher 完成" --body "已完成：任务短名`n结果：已完成`n动作：回来验收"
```

事件类型：

- `permission`：Codex 正在等待用户批准。
- `attention`：Codex 需要用户做决定。
- `done`：任务已经完成。
- `failure`：Codex 可能卡住或连续失败。

## 隐私要求

发送 Bark 通知前必须先摘要和脱敏。不要把完整 transcript、源码文件、命令输出、`.env`、API key、SSH key、Bark key 或长日志发送到 Bark。
