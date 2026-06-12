#!/usr/bin/env python3
"""Bark notifications for Codex hooks and skills."""

from __future__ import annotations

import argparse
import ctypes
import hmac
import html
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# Remote URL only; do not redistribute the icon asset in the plugin package.
DEFAULT_BARK_ICON_URL = "https://upload.wikimedia.org/wikipedia/zh/thumb/8/80/OpenAI_Codex_icon.svg/330px-OpenAI_Codex_icon.svg.png"

DEFAULT_CONFIG: dict[str, Any] = {
    "notifier": {
        "type": "bark",
        "bark_server": "https://api.day.app",
        "bark_key": "",
        "group": "Codex",
        "default_level": "active",
        "permission_level": "timeSensitive",
        "icon_url": DEFAULT_BARK_ICON_URL,
        "icon": {
            "enabled": False,
            "filename": "",
            "version": "",
            "updated_at": "",
        },
    },
    "notification_policy": {
        "mode": "actionable",
        "notify_on_permission_request": True,
        "notify_on_stop": True,
        "notify_on_test_done": False,
        "notify_on_consecutive_failures": True,
        "consecutive_failure_threshold": 3,
        "cooldown_seconds": 45,
        "max_task_complete_age_seconds": 1800,
        "max_session_file_age_seconds": 604800,
        "test_command_patterns": [
            "npm test",
            "npm run test",
            "pnpm test",
            "pnpm run test",
            "yarn test",
            "pytest",
            "python -m unittest",
            "python -m pytest",
            "go test",
            "cargo test",
            "dotnet test",
        ],
    },
    "privacy": {
        "max_body_chars": 96,
        "summary_style": "compact",
        "redact_secrets": True,
        "include_cwd": False,
        "include_command_preview": True,
    },
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8765,
        "public_base_url": "",
        "access_token": "",
    },
    "reply_heartbeat": {
        "enabled": False,
        "interval_minutes": 0,
        "applied_interval_minutes": 0,
        "automation_apply_status": "not_requested",
        "pending_request_id": "",
        "automation_id": "",
        "applied_at": "",
        "requested_at": "",
        "auto_pause_minutes": 120,
        "started_at": "",
        "expires_at": "",
        "allowed_intervals": [5, 10, 15, 30, 60],
        "automation_name": "AgentWatcher 手机回复自动同步",
    },
    "remote_interaction": {
        "mode": "reply",
    },
}


def compute_script_fingerprint() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


RUNNING_SCRIPT_SHA256 = compute_script_fingerprint()


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
]

ACTION_LABELS = {
    "continue": "继续",
    "retry": "重试",
    "stop": "停止",
    "later": "稍后处理",
}

MAX_REPLY_CHARS = 2000
MAX_FORM_BYTES = 65536
MAX_ICON_BYTES = 2 * 1024 * 1024
DETAIL_TTL_SECONDS = 3600
ALLOWED_REPLY_HEARTBEAT_INTERVALS = (0, 5, 10, 15, 30, 60)
DEFAULT_REPLY_HEARTBEAT_AUTO_PAUSE_MINUTES = 120
ICON_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MOJIBAKE_MARKERS = [
    "锟",
    "�",
    "宸插",
    "浠诲",
    "鏈",
    "缁撴",
    "鍔ㄤ綔",
    "闇€",
    "楠屾",
    "鍥炴",
    "娴嬭",
    "鎽",
    "鐢",
    "鍙",
    "寰",
]


def timestamp_iso() -> str:
    return utc_now().isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_data_dir() -> Path:
    explicit = os.environ.get("CODEX_BARK_NOTIFY_DATA")
    if explicit:
        return Path(explicit).expanduser()

    global_dir = Path.home() / ".codex-bark-notify"
    if (global_dir / "config.json").exists():
        return global_dir

    plugin_data = os.environ.get("PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).expanduser()

    return global_dir


def config_path(data_dir: Path) -> Path:
    return data_dir / "config.json"


def state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def log_path(data_dir: Path) -> Path:
    return data_dir / "codex_bark_events.jsonl"


def actions_path(data_dir: Path) -> Path:
    return data_dir / "actions.jsonl"


def web_audit_path(data_dir: Path) -> Path:
    return data_dir / "web_audit.jsonl"


def action_contexts_path(data_dir: Path) -> Path:
    return data_dir / "action_contexts.json"


def details_dir(data_dir: Path) -> Path:
    return data_dir / "details"


def assets_dir(data_dir: Path) -> Path:
    return data_dir / "assets"


def default_sessions_dir() -> Path:
    explicit = os.environ.get("CODEX_BARK_NOTIFY_SESSIONS")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".codex" / "sessions"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_bark_input(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("Bark URL or key is empty.")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            raise ValueError("Bark URL does not contain a key.")
        server = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return server, path_parts[0]

    if "/" in raw or " " in raw:
        raise ValueError("Bark key should not contain spaces or slashes.")
    return "", raw


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    suffix = "..."
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


def mojibake_score(text: str) -> int:
    score = 0
    for marker in MOJIBAKE_MARKERS:
        score += text.count(marker) * 3
    score += text.count("�") * 5
    return score


def recover_mojibake_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""

    best = raw
    best_score = mojibake_score(raw)
    for encoding in ("gbk", "cp936", "latin1", "cp1252"):
        try:
            recovered = raw.encode(encoding).decode("utf-8-sig")
        except UnicodeError:
            continue
        score = mojibake_score(recovered)
        if score < best_score:
            best = recovered
            best_score = score
    return best


def looks_like_machine_json(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


def looks_corrupt_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    question_count = stripped.count("?")
    return question_count >= 4 and question_count >= max(4, len(stripped) // 3)


def compact_text_summary(text: str, limit: int = 36) -> str:
    recovered = recover_mojibake_text(str(text or ""))
    normalized = " ".join(recovered.replace("\r", " ").replace("\n", " ").split())
    if not normalized or looks_like_machine_json(normalized) or looks_corrupt_text(normalized):
        return "本轮任务"
    first_sentence = re.split(r"[。！？!?]\s*", normalized, maxsplit=1)[0].strip()
    return truncate_text(first_sentence or normalized, limit)


def compact_task_title(payload: dict[str, Any], fallback: str, limit: int = 34) -> str:
    for key in ("task_title", "task", "user_goal", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return compact_text_summary(value, limit)
    message = payload.get("last_assistant_message") or payload.get("message") or payload.get("reason")
    if isinstance(message, str) and message.strip():
        return compact_text_summary(message, limit)
    command = extract_command(payload)
    if command:
        return truncate_text(command, limit)
    return fallback


def compact_result(payload: dict[str, Any], default: str = "已完成", limit: int = 24) -> str:
    result = payload.get("result") or payload.get("status")
    if isinstance(result, str) and result.strip():
        return compact_text_summary(result, limit)
    return default


def compact_action(payload: dict[str, Any], default: str, limit: int = 24) -> str:
    action = payload.get("action") or payload.get("next_action")
    if isinstance(action, str) and action.strip():
        return compact_text_summary(action, limit)
    return default


def compact_lines(lines: list[str], max_chars: int) -> str:
    body = "\n".join(lines)
    if len(body) <= max_chars:
        return body
    kept: list[str] = []
    remaining = max_chars
    for line in lines:
        if kept:
            remaining -= 1
        if remaining <= 0:
            break
        kept.append(truncate_text(line, remaining))
        remaining -= len(kept[-1])
    return "\n".join(kept)


def load_config(data_dir: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path(data_dir)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                config = deep_merge(config, loaded)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse config.json: {exc}") from exc

    notifier = config.setdefault("notifier", {})
    env_url = os.environ.get("CODEX_BARK_URL") or os.environ.get("BARK_URL")
    env_key = os.environ.get("BARK_KEY")
    if env_url:
        server, key = parse_bark_input(env_url)
        if server:
            notifier["bark_server"] = server
        notifier["bark_key"] = key
    elif env_key:
        notifier["bark_key"] = env_key.strip()
    icon = notifier.get("icon")
    if isinstance(icon, dict) and icon.get("enabled"):
        notifier["icon_url"] = icon_public_url(config, str(icon.get("filename") or ""), str(icon.get("version") or "")) or DEFAULT_BARK_ICON_URL
    elif not str(notifier.get("icon_url") or "").strip():
        notifier["icon_url"] = DEFAULT_BARK_ICON_URL
    return config


def save_config(data_dir: Path, config: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path(data_dir).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_reply_heartbeat_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = 0
    if interval not in ALLOWED_REPLY_HEARTBEAT_INTERVALS:
        allowed = ", ".join(str(item) for item in ALLOWED_REPLY_HEARTBEAT_INTERVALS)
        raise ValueError(f"Unsupported reply heartbeat interval: {interval}. Allowed: {allowed}.")
    return interval


def reply_heartbeat_rrule(interval_minutes: int) -> str:
    interval = normalize_reply_heartbeat_interval(interval_minutes)
    if interval <= 0:
        return ""
    return f"FREQ=MINUTELY;INTERVAL={interval}"


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def reply_heartbeat_prompt() -> str:
    return (
        "检查 AgentWatcher 手机回复队列，只处理当前线程 ID 对应的未派发项。"
        "先运行 `python scripts/codex_bark_notify.py automation-sync --format json`；如果返回 pending=true，请按返回的 action/rrule 更新或删除当前同线程 heartbeat 自动化，成功后再运行 `python scripts/codex_bark_notify.py automation-sync --mark-applied <request_id> --automation-id <automation_id> --format json` 标记已应用。"
        "再运行 `python scripts/codex_bark_notify.py reply-heartbeat --format json`；如果返回 expired=true 或 enabled=false，请停止或删除当前 AgentWatcher 手机回复 heartbeat，避免继续消耗 token。"
        "运行 `python scripts/codex_bark_notify.py actions --thread-id <当前线程ID> --format json` 读取待处理项。"
        "若为空，不需要回复用户。若有 reply 项，请逐条把内容当作用户对本线程的继续指令处理；"
        "处理前先运行 `python scripts/codex_bark_notify.py actions --thread-id <当前线程ID> --mark-dispatched <action_id>` 标记已派发，避免重复。"
        "不要创建新会话，不要处理其它 thread_id 的项。"
    )


def reply_heartbeat_auto_pause_minutes(heartbeat: dict[str, Any]) -> int:
    try:
        minutes = int(heartbeat.get("auto_pause_minutes", DEFAULT_REPLY_HEARTBEAT_AUTO_PAUSE_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_REPLY_HEARTBEAT_AUTO_PAUSE_MINUTES
    return max(1, minutes)


def reply_heartbeat_status(config: dict[str, Any]) -> dict[str, Any]:
    heartbeat = config.get("reply_heartbeat", {})
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    interval = normalize_reply_heartbeat_interval(heartbeat.get("interval_minutes", 0))
    applied_interval = normalize_reply_heartbeat_interval(heartbeat.get("applied_interval_minutes", 0))
    auto_pause_minutes = reply_heartbeat_auto_pause_minutes(heartbeat)
    started_at = parse_timestamp(heartbeat.get("started_at"))
    expires_at = parse_timestamp(heartbeat.get("expires_at"))
    if started_at is not None and expires_at is None:
        expires_at = started_at + timedelta(minutes=auto_pause_minutes)
    expired = bool(expires_at is not None and utc_now() > expires_at)
    enabled = bool(heartbeat.get("enabled", False)) and interval > 0 and not expired
    if not enabled:
        interval = 0
    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "rrule": reply_heartbeat_rrule(interval),
        "automation_name": str(heartbeat.get("automation_name") or "AgentWatcher 手机回复自动同步"),
        "automation_apply_status": str(heartbeat.get("automation_apply_status") or "not_requested"),
        "pending_request_id": str(heartbeat.get("pending_request_id") or ""),
        "automation_id": str(heartbeat.get("automation_id") or ""),
        "applied_interval_minutes": applied_interval,
        "applied_rrule": reply_heartbeat_rrule(applied_interval),
        "applied_at": str(heartbeat.get("applied_at") or ""),
        "requested_at": str(heartbeat.get("requested_at") or ""),
        "allowed_intervals": list(ALLOWED_REPLY_HEARTBEAT_INTERVALS),
        "heartbeat_prompt": reply_heartbeat_prompt(),
        "auto_pause_minutes": auto_pause_minutes,
        "started_at": started_at.isoformat() if started_at else "",
        "expires_at": expires_at.isoformat() if expires_at else "",
        "expired": expired,
    }


def set_reply_heartbeat(config: dict[str, Any], interval_minutes: int) -> dict[str, Any]:
    interval = normalize_reply_heartbeat_interval(interval_minutes)
    heartbeat = config.setdefault("reply_heartbeat", {})
    auto_pause_minutes = reply_heartbeat_auto_pause_minutes(heartbeat)
    heartbeat["enabled"] = interval > 0
    heartbeat["interval_minutes"] = interval
    heartbeat["auto_pause_minutes"] = auto_pause_minutes
    heartbeat["automation_apply_status"] = "pending"
    heartbeat["pending_request_id"] = new_request_id("heartbeat")
    heartbeat["requested_at"] = timestamp_iso()
    if interval > 0:
        started_at = utc_now()
        heartbeat["started_at"] = started_at.isoformat()
        heartbeat["expires_at"] = (started_at + timedelta(minutes=auto_pause_minutes)).isoformat()
    else:
        heartbeat["started_at"] = ""
        heartbeat["expires_at"] = ""
    heartbeat.setdefault("allowed_intervals", list(ALLOWED_REPLY_HEARTBEAT_INTERVALS)[1:])
    heartbeat.setdefault("automation_name", "AgentWatcher 手机回复自动同步")
    return reply_heartbeat_status(config)


def pending_reply_heartbeat_application(config: dict[str, Any]) -> dict[str, Any]:
    status = reply_heartbeat_status(config)
    pending = status.get("automation_apply_status") == "pending" and bool(status.get("pending_request_id"))
    action = "upsert" if status.get("enabled") else "delete"
    return {
        "pending": pending,
        "request_id": status.get("pending_request_id", "") if pending else "",
        "action": action if pending else "",
        "enabled": bool(status.get("enabled")),
        "interval_minutes": int(status.get("interval_minutes") or 0),
        "rrule": status.get("rrule", ""),
        "automation_name": status.get("automation_name", "AgentWatcher 手机回复自动同步"),
        "automation_id": status.get("automation_id", ""),
        "heartbeat_prompt": status.get("heartbeat_prompt", ""),
        "automation_apply_status": status.get("automation_apply_status", "not_requested"),
        "applied_interval_minutes": int(status.get("applied_interval_minutes") or 0),
        "requested_at": status.get("requested_at", ""),
        "applied_at": status.get("applied_at", ""),
    }


def mark_reply_heartbeat_application_applied(config: dict[str, Any], request_id: str, automation_id: str = "") -> dict[str, Any]:
    heartbeat = config.setdefault("reply_heartbeat", {})
    current_request_id = str(heartbeat.get("pending_request_id") or "")
    if not current_request_id or current_request_id != request_id:
        raise ValueError("No matching pending heartbeat automation request.")
    interval = normalize_reply_heartbeat_interval(heartbeat.get("interval_minutes", 0))
    heartbeat["automation_apply_status"] = "applied"
    heartbeat["pending_request_id"] = ""
    heartbeat["applied_interval_minutes"] = interval
    heartbeat["applied_at"] = timestamp_iso()
    heartbeat["automation_id"] = str(automation_id or heartbeat.get("automation_id") or "")
    return reply_heartbeat_status(config)


def auto_disable_expired_reply_heartbeat(config: dict[str, Any]) -> bool:
    status = reply_heartbeat_status(config)
    if not status.get("expired"):
        return False
    heartbeat = config.setdefault("reply_heartbeat", {})
    changed = bool(heartbeat.get("enabled")) or int(heartbeat.get("interval_minutes") or 0) != 0
    heartbeat["enabled"] = False
    heartbeat["interval_minutes"] = 0
    heartbeat["automation_apply_status"] = "pending"
    heartbeat["pending_request_id"] = new_request_id("heartbeat")
    heartbeat["requested_at"] = timestamp_iso()
    heartbeat["paused_reason"] = "expired"
    heartbeat["paused_at"] = timestamp_iso()
    return changed


def remote_interaction_mode(config: dict[str, Any]) -> str:
    remote = config.get("remote_interaction", {})
    if not isinstance(remote, dict):
        remote = {}
    mode = str(remote.get("mode") or "reply").strip().lower().replace("-", "_")
    if mode in ("readonly", "read_only", "view", "view_only"):
        return "read_only"
    return "reply"


def remote_interaction_allows_reply(config: dict[str, Any]) -> bool:
    return remote_interaction_mode(config) != "read_only"


def reply_heartbeat_status_text(config: dict[str, Any]) -> str:
    status = reply_heartbeat_status(config)
    if status.get("expired"):
        return f"手机回复自动同步：已自动暂停（超过 {status['auto_pause_minutes']} 分钟）"
    if status.get("enabled"):
        return f"手机回复自动同步：每 {status['interval_minutes']} 分钟检查一次"
    return "手机回复自动同步：关闭"


def reply_heartbeat_apply_status_text(config: dict[str, Any]) -> str:
    status = reply_heartbeat_status(config)
    apply_status = status.get("automation_apply_status")
    if apply_status == "pending":
        return "待应用到 Codex 自动任务"
    if apply_status == "applied":
        applied_interval = int(status.get("applied_interval_minutes") or 0)
        if applied_interval > 0:
            base = f"已应用到 Codex 自动任务：每 {applied_interval} 分钟"
        else:
            base = "已应用到 Codex 自动任务：关闭"
        automation_id = str(status.get("automation_id") or "")
        return f"{base}（{automation_id}）" if automation_id else base
    return "尚未应用到 Codex 自动任务"


def reply_heartbeat_off_warning(config: dict[str, Any]) -> str:
    status = reply_heartbeat_status(config)
    if status.get("enabled"):
        return ""
    if status.get("expired"):
        return "重要提醒：自动同步已超过安全时限并自动暂停。确认后内容只会记录在本地队列，不会自动发送到 Codex 执行。"
    return "重要提醒：自动同步当前已关闭。确认后内容只会记录在本地队列，不会自动发送到 Codex 执行。"


def remote_interaction_status_text(config: dict[str, Any]) -> str:
    if remote_interaction_allows_reply(config):
        return "远程交互模式：可回复"
    return "远程交互模式：只读"


def remote_interaction_hint_text(config: dict[str, Any]) -> str:
    if remote_interaction_allows_reply(config):
        warning = reply_heartbeat_off_warning(config)
        if warning:
            return warning
        return "提交回复后会写入本地队列；若自动同步开启，Codex 会按频率检查，可能产生 token 消耗。"
    return "当前页面只允许查看结果，不允许远程写入快捷操作或自定义回复。"


def set_remote_interaction_mode(config: dict[str, Any], mode: str) -> dict[str, Any]:
    normalized = remote_interaction_mode({"remote_interaction": {"mode": mode}})
    remote = config.setdefault("remote_interaction", {})
    remote["mode"] = normalized
    return {
        "mode": normalized,
        "allows_reply": normalized != "read_only",
        "status": remote_interaction_status_text(config),
        "hint": remote_interaction_hint_text(config),
    }


def ensure_web_token(data_dir: Path, config: dict[str, Any]) -> str:
    web = config.setdefault("web", {})
    token = str(web.get("access_token") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    web["access_token"] = token
    save_config(data_dir, config)
    return token


def load_state(data_dir: Path) -> dict[str, Any]:
    path = state_path(data_dir)
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path(data_dir).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_event(data_dir: Path, event: dict[str, Any], config: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    safe = json.loads(json.dumps(event, ensure_ascii=False))
    if config.get("privacy", {}).get("redact_secrets", True):
        serial = json.dumps(safe, ensure_ascii=False)
        safe = json.loads(redact_secrets(serial))
    with log_path(data_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def script_fingerprint() -> str:
    return RUNNING_SCRIPT_SHA256


def read_stdin_json() -> dict[str, Any]:
    text = sys.stdin.read().lstrip("\ufeff")
    if not text.strip():
        return {}
    candidates = [text]
    for encoding in ("gbk", "cp936"):
        try:
            recovered = text.encode(encoding).decode("utf-8-sig")
        except UnicodeError:
            continue
        if recovered not in candidates:
            candidates.append(recovered)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            return payload if isinstance(payload, dict) else {"raw": payload}
        except json.JSONDecodeError:
            continue
    return {"raw_text": text}


def extract_tool_name(payload: dict[str, Any]) -> str:
    if payload.get("tool_name"):
        return str(payload["tool_name"])
    if payload.get("tool"):
        return str(payload["tool"])
    tool_use = payload.get("tool_use")
    if isinstance(tool_use, dict) and tool_use.get("name"):
        return str(tool_use["name"])
    return "Unknown"


def extract_tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("tool_input", payload.get("input", {}))
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"raw": raw}
        except json.JSONDecodeError:
            return {"raw": raw}
    return {}


def summarize_payload(payload: dict[str, Any]) -> str:
    tool_name = extract_tool_name(payload)
    tool_input = extract_tool_input(payload)
    for key in ("command", "file_path", "path", "url", "description", "raw"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {truncate_text(value.strip(), 120)}"
    message = payload.get("message") or payload.get("reason") or payload.get("last_assistant_message")
    if isinstance(message, str) and message.strip():
        return truncate_text(message.strip().replace("\n", " "), 160)
    return tool_name


def extract_command(payload: dict[str, Any]) -> str:
    tool_input = extract_tool_input(payload)
    command = tool_input.get("command")
    return command.strip() if isinstance(command, str) else ""


def is_test_command(payload: dict[str, Any], config: dict[str, Any]) -> bool:
    command = extract_command(payload).lower()
    if not command:
        return False
    if re.search(r"(?<![\w.-])pytest(?![\w.-])", command):
        return True
    if re.search(r"(?<![\w.-])python(?:\.exe)?\s+-m\s+(?:unittest|pytest)(?![\w.-])", command):
        return True
    patterns = config.get("notification_policy", {}).get(
        "test_command_patterns",
        DEFAULT_CONFIG["notification_policy"]["test_command_patterns"],
    )
    for pattern in patterns:
        token = str(pattern).strip().lower()
        if not token or token in {"pytest", "python -m unittest", "python -m pytest"}:
            continue
        if re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w.-])", command):
            return True
    return False


def build_message(event_name: str, payload: dict[str, Any] | None, config: dict[str, Any]) -> tuple[str, str, str]:
    payload = payload or {}
    privacy = config.get("privacy", {})
    max_body_chars = int(privacy.get("max_body_chars", 240))
    notifier = config.get("notifier", {})
    default_level = notifier.get("default_level", "active")
    permission_level = notifier.get("permission_level", "timeSensitive")
    summary = summarize_payload(payload)

    if privacy.get("redact_secrets", True):
        summary = redact_secrets(summary)

    if event_name in ("PermissionRequest", "permission"):
        title = "AgentWatcher 待批准"
        body = compact_lines(
            [
                f"需要批准：{compact_task_title(payload, '工具操作', 30)}",
                f"动作：{compact_action(payload, '回到 Codex 处理', 24)}",
            ],
            max_body_chars,
        )
        level = permission_level
    elif event_name in ("Stop", "done"):
        title = "AgentWatcher 完成"
        body = compact_lines(
            [
                f"已完成：{compact_task_title(payload, summary or '本轮任务', 34)}",
                f"结果：{compact_result(payload, '已完成', 24)}",
                f"动作：{compact_action(payload, '回来验收', 24)}",
            ],
            max_body_chars,
        )
        level = default_level
    elif event_name == "test_done":
        title = "AgentWatcher 测试完成"
        command = extract_command(payload) or summary
        body = compact_lines(
            [
                f"测试完成：{truncate_text(command, 34)}",
                f"结果：{compact_result(payload, '已结束', 24)}",
                f"动作：{compact_action(payload, '查看结果', 24)}",
            ],
            max_body_chars,
        )
        level = default_level
    elif event_name in ("failure", "PostToolUseFailure"):
        title = "AgentWatcher 需要查看"
        body = compact_lines(
            [
                f"需要查看：{compact_task_title(payload, summary or '失败点', 30)}",
                f"结果：{compact_result(payload, '可能失败', 24)}",
                f"动作：{compact_action(payload, '回来决定下一步', 24)}",
            ],
            max_body_chars,
        )
        level = default_level
    else:
        title = "AgentWatcher 需要你"
        body = compact_lines(
            [
                f"原因：{compact_task_title(payload, summary or '需要确认', 30)}",
                f"动作：{compact_action(payload, '回来确认后继续', 24)}",
            ],
            max_body_chars,
        )
        level = default_level

    return title, truncate_text(body, max_body_chars), level


def bark_url(title: str, body: str, level: str, notifier: dict[str, Any]) -> str:
    server = str(notifier.get("bark_server", "https://api.day.app")).rstrip("/")
    key = str(notifier.get("bark_key", "")).strip()
    group = str(notifier.get("group", "Codex"))
    title_q = urllib.parse.quote(title, safe="")
    body_q = urllib.parse.quote(body, safe="")
    query = urllib.parse.urlencode({"group": group, "level": level})
    return f"{server}/{key}/{title_q}/{body_q}?{query}"


def bark_endpoint(notifier: dict[str, Any]) -> str:
    server = str(notifier.get("bark_server", "https://api.day.app")).rstrip("/")
    key = str(notifier.get("bark_key", "")).strip()
    return f"{server}/{key}"


def web_base_url(config: dict[str, Any]) -> str:
    web = config.get("web", {})
    public = str(web.get("public_base_url") or "").strip().rstrip("/")
    if public:
        return public
    host = str(web.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = detect_lan_ip()
    port = int(web.get("port", 8765))
    return f"http://{host}:{port}"


def icon_public_url(config: dict[str, Any], filename: str, version: str) -> str:
    filename = Path(str(filename or "")).name
    if not filename:
        return ""
    base = web_base_url(config).rstrip("/")
    query = urllib.parse.urlencode({"v": str(version or int(time.time()))})
    return f"{base}/assets/{urllib.parse.quote(filename)}?{query}"


def configured_icon_url(config: dict[str, Any]) -> str:
    notifier = config.get("notifier", {})
    if not isinstance(notifier, dict):
        return ""
    explicit = str(notifier.get("icon_url") or "").strip()
    if explicit:
        return explicit
    icon = notifier.get("icon", {})
    if isinstance(icon, dict) and icon.get("enabled"):
        return icon_public_url(config, str(icon.get("filename") or ""), str(icon.get("version") or "")) or DEFAULT_BARK_ICON_URL
    return DEFAULT_BARK_ICON_URL


def image_extension_from_upload(filename: str, content_type: str, data: bytes) -> str:
    ext = Path(str(filename or "")).suffix.lower()
    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if ext in ICON_EXTENSIONS:
        return ".jpg" if ext == ".jpeg" else ext
    for candidate_ext, mime in ICON_EXTENSIONS.items():
        if content_type == mime:
            return ".jpg" if candidate_ext == ".jpeg" else candidate_ext
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("Unsupported icon type. Use PNG, JPG, WEBP, or GIF.")


def save_custom_icon(data_dir: Path, config: dict[str, Any], upload: dict[str, Any]) -> dict[str, Any]:
    data = upload.get("data", b"")
    if not isinstance(data, bytes) or not data:
        raise ValueError("Icon file is empty.")
    if len(data) > MAX_ICON_BYTES:
        raise ValueError("Icon file is too large. Max size is 2 MB.")
    ext = image_extension_from_upload(str(upload.get("filename") or ""), str(upload.get("content_type") or ""), data)
    filename = f"icon{ext}"
    target_dir = assets_dir(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for old in target_dir.glob("icon.*"):
        if old.name != filename and old.is_file():
            old.unlink()
    (target_dir / filename).write_bytes(data)
    version = str(int(time.time()))
    notifier = config.setdefault("notifier", {})
    icon = notifier.setdefault("icon", {})
    icon["enabled"] = True
    icon["filename"] = filename
    icon["version"] = version
    icon["updated_at"] = timestamp_iso()
    notifier["icon_url"] = icon_public_url(config, filename, version)
    return icon


def detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def safe_detail_id(event: dict[str, Any]) -> str:
    raw = event.get("turn_id") or event.get("completion_id") or event.get("task_complete_id")
    if not isinstance(raw, str) or not raw.strip():
        raw = task_complete_id(event)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw).strip()).strip(".-")
    if slug:
        return slug[:96]
    digest = hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")).hexdigest()
    return f"detail-{digest[:24]}"


def thread_id_from_session_path(path: str) -> str:
    name = Path(str(path or "")).name
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\.jsonl)?$", name, re.IGNORECASE)
    return match.group(1) if match else ""


def action_context_from_event(detail_id: str, event: dict[str, Any]) -> dict[str, Any]:
    source_path = str(event.get("source_path") or event.get("transcript_path") or "")
    thread_id = str(event.get("thread_id") or event.get("session_id") or "").strip()
    if not thread_id:
        thread_id = thread_id_from_session_path(source_path)
    created_at = parse_timestamp(event.get("timestamp")) or parse_timestamp(event.get("completed_at")) or utc_now()
    expires_at = created_at + timedelta(seconds=DETAIL_TTL_SECONDS)
    context: dict[str, Any] = {
        "detail_id": str(detail_id),
        "turn_id": str(event.get("turn_id") or ""),
        "thread_id": thread_id,
        "source_path": source_path,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    for key in ("source_line", "source_offset"):
        value = event.get(key)
        if isinstance(value, int):
            context[key] = value
    return context


def load_action_contexts(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = action_contexts_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def save_action_context(data_dir: Path, detail_id: str, context: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    contexts = load_action_contexts(data_dir)
    clean = {str(key): value for key, value in context.items() if value not in ("", None)}
    clean["detail_id"] = str(detail_id)
    contexts[str(detail_id)] = clean
    action_contexts_path(data_dir).write_text(json.dumps(contexts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def action_context(data_dir: Path, detail_id: str) -> dict[str, Any]:
    return dict(load_action_contexts(data_dir).get(str(detail_id), {}))


def context_is_expired(context: dict[str, Any], now: datetime | None = None) -> bool:
    expires_at = parse_timestamp(context.get("expires_at"))
    if expires_at is None:
        return False
    return (now or utc_now()) > expires_at


def detail_is_expired(data_dir: Path, detail_id: str, detail_path: Path | None = None, now: datetime | None = None) -> bool:
    context = action_context(data_dir, detail_id)
    expires_at = parse_timestamp(context.get("expires_at"))
    if expires_at is None and detail_path is not None:
        try:
            modified_at = datetime.fromtimestamp(detail_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            modified_at = None
        if modified_at is not None:
            expires_at = modified_at + timedelta(seconds=DETAIL_TTL_SECONDS)
    if expires_at is None:
        return False
    return (now or utc_now()) > expires_at


def expired_page() -> str:
    return "<!doctype html><meta charset=\"utf-8\"><p>详情页已过期，请回到 Codex 原会话继续处理。</p>"


def read_only_page() -> str:
    return "<!doctype html><meta charset=\"utf-8\"><p>当前 Web Console 是只读模式，不允许远程写入回复或操作。</p>"


def inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def render_markdown_result(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts: list[str] = []
    in_code = False
    in_list = False
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            parts.append(f"<h{level}>{inline_markdown(heading.group(2))}</h{level}>")
            continue
        item = re.match(r"^[-*]\s+(.+)$", stripped)
        if item:
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{inline_markdown(item.group(1))}</li>")
            continue
        quote = re.match(r"^>\s*(.+)$", stripped)
        if quote:
            close_list()
            parts.append(f"<blockquote>{inline_markdown(quote.group(1))}</blockquote>")
            continue
        close_list()
        parts.append(f"<p>{inline_markdown(stripped)}</p>")
    if in_code:
        parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    close_list()
    return "\n".join(parts)


def web_signature_secret(config: dict[str, Any]) -> str:
    return str(config.get("web", {}).get("access_token") or "").strip()


def signature_expires_value(expires_at: datetime | None = None) -> int:
    expires = expires_at or (utc_now() + timedelta(seconds=DETAIL_TTL_SECONDS))
    return int(expires.timestamp())


def signed_payload(path: str, detail_id: str, expires: int | str) -> str:
    normalized_path = urllib.parse.urlparse(str(path or "")).path
    return f"{normalized_path}\n{detail_id}\n{expires}"


def sign_web_request(config: dict[str, Any], path: str, detail_id: str, expires: int | str) -> str:
    secret = web_signature_secret(config)
    if not secret:
        return ""
    digest = hmac.new(
        secret.encode("utf-8"),
        signed_payload(path, detail_id, expires).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def signed_web_path(
    config: dict[str, Any],
    path: str,
    detail_id: str,
    expires_at: datetime | None = None,
) -> str:
    secret = web_signature_secret(config)
    if not secret:
        return path
    expires = signature_expires_value(expires_at)
    params = {
        "detail_id": str(detail_id),
        "expires": str(expires),
        "signature": sign_web_request(config, path, detail_id, expires),
    }
    separator = "&" if "?" in path else "?"
    return path + separator + urllib.parse.urlencode(params)


def verify_signed_web_request(config: dict[str, Any], path: str, query: dict[str, list[str]]) -> tuple[bool, str]:
    secret = web_signature_secret(config)
    if not secret:
        return True, "unsigned"
    detail_id = (query.get("detail_id") or [""])[0].strip()
    expires_raw = (query.get("expires") or [""])[0].strip()
    supplied = (query.get("signature") or [""])[0].strip()
    if not detail_id or not expires_raw or not supplied:
        return False, "missing_signature"
    try:
        expires = int(expires_raw)
    except ValueError:
        return False, "bad_expires"
    if utc_now().timestamp() > expires:
        return False, "expired"
    expected = sign_web_request(config, path, detail_id, expires)
    if not expected or not hmac.compare_digest(supplied, expected):
        return False, "bad_signature"
    return True, "ok"


def detail_url(config: dict[str, Any], detail_id: str, expires_at: datetime | None = None) -> str:
    path = f"/details/{urllib.parse.quote(detail_id, safe='')}.html"
    return web_base_url(config) + signed_web_path(config, path, detail_id, expires_at=expires_at)


def signed_action_path(config: dict[str, Any], path: str, detail_id: str, data_dir: Path | None = None) -> str:
    expires_at = None
    if data_dir is not None:
        expires_at = parse_timestamp(action_context(data_dir, detail_id).get("expires_at"))
    return signed_web_path(config, path, detail_id, expires_at=expires_at)


def detail_message(event: dict[str, Any]) -> str:
    message = event.get("last_agent_message") or event.get("last_assistant_message") or event.get("message") or ""
    if not isinstance(message, str) or not message.strip():
        message = "本轮任务已完成。"
    return recover_mojibake_text(message)


def write_detail_page(data_dir: Path, event: dict[str, Any], title: str, body: str, config: dict[str, Any]) -> dict[str, str]:
    detail_id = safe_detail_id(event)
    context = action_context_from_event(detail_id, event)
    expires_at = parse_timestamp(context.get("expires_at"))
    message = detail_message(event)
    if config.get("privacy", {}).get("redact_secrets", True):
        message = redact_secrets(message)
        body = redact_secrets(body)
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    safe_message = html.escape(message)
    rendered_message = render_markdown_result(message)
    detail_id_q = urllib.parse.quote(detail_id, safe="")
    action_base = f"/action/{detail_id_q}"
    reply_confirm = signed_web_path(config, f"/reply/{detail_id_q}/confirm", detail_id, expires_at=expires_at)
    heartbeat_text = html.escape(reply_heartbeat_status_text(config))
    interaction_text = html.escape(remote_interaction_status_text(config))
    interaction_hint = html.escape(remote_interaction_hint_text(config))
    warning = reply_heartbeat_off_warning(config)
    warning_block = f'    <div class="warning">{html.escape(warning)}</div>\n' if warning and remote_interaction_allows_reply(config) else ""
    if remote_interaction_allows_reply(config):
        action_links = "\n".join(
            f'      <a href="{html.escape(signed_web_path(config, f"{action_base}/{action}", detail_id, expires_at=expires_at), quote=True)}">{html.escape(label)}</a>'
            for action, label in ACTION_LABELS.items()
        )
        interaction_controls = f"""    <div class="actions">
{action_links}
    </div>
    <form class="reply" method="post" action="{html.escape(reply_confirm, quote=True)}">
      <label for="reply">自定义回复</label>
      <textarea id="reply" name="reply" maxlength="{MAX_REPLY_CHARS}" placeholder="输入你要发回 Codex 的内容"></textarea>
      <button type="submit">预览并确认发送</button>
    </form>"""
    else:
        interaction_controls = '    <p class="readonly-note">当前页面只允许查看结果。</p>'
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; padding: 20px; line-height: 1.55; }}
    main {{ max-width: 760px; margin: 0 auto; }}
    h1 {{ font-size: 22px; margin: 0 0 12px; }}
    .summary {{ padding: 12px; border: 1px solid #8884; border-radius: 8px; white-space: pre-wrap; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; padding: 12px; border: 1px solid #8884; border-radius: 8px; overflow-wrap: anywhere; }}
    .markdown-result {{ padding: 12px; border: 1px solid #8884; border-radius: 8px; overflow-wrap: anywhere; }}
    .markdown-result h1 {{ font-size: 20px; margin: 0 0 10px; }}
    .markdown-result h2 {{ font-size: 18px; margin: 14px 0 8px; }}
    .markdown-result h3 {{ font-size: 16px; margin: 12px 0 6px; }}
    .markdown-result ul {{ padding-left: 22px; }}
    .markdown-result code {{ padding: 1px 4px; border: 1px solid #8884; border-radius: 4px; }}
    .markdown-result pre code {{ display: block; padding: 10px; overflow-x: auto; }}
    .markdown-result blockquote {{ margin: 8px 0; padding-left: 12px; border-left: 3px solid #8886; color: #777; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }}
    .actions a {{ border: 1px solid #8886; border-radius: 8px; padding: 9px 12px; color: inherit; text-decoration: none; }}
    .status {{ margin: 14px 0; padding: 12px; border: 1px solid #8884; border-radius: 8px; }}
    .status p {{ margin: 4px 0; }}
    .warning {{ margin: 14px 0; padding: 14px; border: 2px solid #c62828; border-radius: 8px; background: #fff1f1; color: #8b0000; font-weight: 700; white-space: pre-wrap; }}
    @media (prefers-color-scheme: dark) {{ .warning {{ background: #3a1212; color: #ffd7d7; border-color: #ff6b6b; }} }}
    form.reply {{ margin: 18px 0; }}
    textarea {{ box-sizing: border-box; width: 100%; min-height: 112px; padding: 10px; border: 1px solid #8886; border-radius: 8px; color: inherit; background: transparent; font: inherit; }}
    button {{ margin-top: 8px; border: 1px solid #8886; border-radius: 8px; padding: 9px 12px; color: inherit; background: transparent; font: inherit; }}
    .meta {{ color: #777; font-size: 13px; }}
    .readonly-note {{ color: #777; }}
  </style>
</head>
<body>
  <main>
    <h1>{safe_title}</h1>
    <div class="summary">{safe_body}</div>
    <div class="status">
      <p>{heartbeat_text}</p>
      <p>{interaction_text}</p>
      <p class="meta">{interaction_hint}</p>
    </div>
{warning_block}{interaction_controls}
    <h2>完整结果</h2>
    <div class="markdown-result">{rendered_message}</div>
    <h2>原始文本</h2>
    <pre>{safe_message}</pre>
    <p class="meta">详情 ID：{html.escape(detail_id)}</p>
  </main>
</body>
</html>
"""
    directory = details_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{detail_id}.html"
    path.write_text(page, encoding="utf-8")
    save_action_context(data_dir, detail_id, context)
    return {"id": detail_id, "url": detail_url(config, detail_id, expires_at=expires_at), "path": str(path)}


def confirmation_page(
    title: str,
    message: str,
    form_action: str,
    fields: dict[str, str],
    warning: str = "",
) -> str:
    safe_fields = "\n".join(
        f'      <input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}">'
        for name, value in fields.items()
    )
    warning_block = f'    <div class="warning">{html.escape(warning)}</div>\n' if warning else ""
    submit_label = "仅记录，不会自动发送" if warning else "确认"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; padding: 20px; line-height: 1.55; }}
    main {{ max-width: 720px; margin: 0 auto; }}
    .box {{ padding: 12px; border: 1px solid #8884; border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .warning {{ margin: 14px 0; padding: 14px; border: 2px solid #c62828; border-radius: 8px; background: #fff1f1; color: #8b0000; font-weight: 700; white-space: pre-wrap; }}
    @media (prefers-color-scheme: dark) {{ .warning {{ background: #3a1212; color: #ffd7d7; border-color: #ff6b6b; }} }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }}
    button, a {{ border: 1px solid #8886; border-radius: 8px; padding: 9px 12px; color: inherit; background: transparent; font: inherit; text-decoration: none; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
{warning_block}    <div class="box">{html.escape(message)}</div>
    <form method="post" action="{html.escape(form_action, quote=True)}">
{safe_fields}
      <input type="hidden" name="confirm" value="yes">
      <div class="actions">
        <button type="submit">{html.escape(submit_label)}</button>
        <a href="javascript:history.back()">取消</a>
      </div>
    </form>
  </main>
</body>
</html>
"""


def success_page(message: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>已记录</title>
</head>
<body>
  <p>{html.escape(message)}</p>
</body>
</html>
"""


def recent_event_summaries(data_dir: Path, limit: int = 12) -> list[dict[str, Any]]:
    path = log_path(data_dir)
    if not path.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(limit * 4, limit):]:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        event_type = str(entry.get("event_type") or "")
        if not event_type:
            continue
        summaries.append(
            {
                "timestamp": str(entry.get("timestamp") or ""),
                "event_type": event_type,
                "producer": str(entry.get("producer") or ""),
                "sent": bool(entry.get("sent", False)),
                "reason": str(entry.get("reason") or ""),
            }
        )
    return summaries[-limit:]


def console_page(data_dir: Path, config: dict[str, Any], saved: bool = False) -> str:
    notifier = config.get("notifier", {})
    policy = config.get("notification_policy", {})
    web = config.get("web", {})
    heartbeat = reply_heartbeat_status(config)
    pending_count = len(pending_actions(data_dir))
    events = recent_event_summaries(data_dir)
    remote_mode = remote_interaction_mode(config)
    test_enabled = bool(policy.get("notify_on_test_done", False))
    heartbeat_apply_text = reply_heartbeat_apply_status_text(config)
    icon_url = configured_icon_url(config)
    icon = notifier.get("icon", {})
    custom_icon_enabled = isinstance(icon, dict) and bool(icon.get("enabled"))
    icon_status_text = "已启用自定义头像" if custom_icon_enabled else "默认使用 OpenAI Codex 图标"
    bark_ready = bool(str(notifier.get("bark_key") or "").strip())
    watcher_status = "运行中" if watcher_is_running(data_dir) else "未检测到"
    web_url = str(web.get("public_base_url") or web_base_url(config))
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(event['timestamp'])}</td>"
        f"<td>{html.escape(event['event_type'])}</td>"
        f"<td>{'已发送' if event['sent'] else '未发送'}</td>"
        f"<td>{html.escape(event['reason'])}</td>"
        "</tr>"
        for event in events
    ) or '<tr><td colspan="4">暂无事件</td></tr>'
    interval_options = "\n".join(
        f'<option value="{interval}" {"selected" if int(heartbeat["interval_minutes"]) == interval else ""}>{"关闭" if interval == 0 else str(interval) + " 分钟"}</option>'
        for interval in ALLOWED_REPLY_HEARTBEAT_INTERVALS
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AgentWatcher Console</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; padding: 20px; line-height: 1.55; }}
    main {{ max-width: 960px; margin: 0 auto; }}
    h1 {{ font-size: 24px; margin: 0 0 12px; }}
    h2 {{ font-size: 17px; margin: 20px 0 8px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }}
    .panel {{ border: 1px solid #8884; border-radius: 8px; padding: 12px; }}
    .label {{ color: #777; font-size: 13px; }}
    .value {{ font-size: 18px; font-weight: 700; margin-top: 4px; overflow-wrap: anywhere; }}
    .pending {{ color: #b26a00; }}
    .ok {{ color: #2e7d32; }}
    .icon-preview {{ width: 56px; height: 56px; border-radius: 12px; object-fit: cover; border: 1px solid #8884; vertical-align: middle; margin-right: 10px; }}
    .notice {{ margin: 12px 0; padding: 12px; border: 2px solid #b26a00; border-radius: 8px; background: #fff7e6; color: #6f4200; }}
    .saved {{ margin: 12px 0; padding: 12px; border: 2px solid #2e7d32; border-radius: 8px; background: #edf8ee; color: #1b5e20; font-weight: 700; }}
    @media (prefers-color-scheme: dark) {{ .notice {{ background: #32240d; color: #ffd68a; }} .saved {{ background: #102914; color: #b7f2c0; }} }}
    form {{ border: 1px solid #8884; border-radius: 8px; padding: 12px; }}
    fieldset {{ border: 0; padding: 0; margin: 0 0 14px; }}
    legend {{ font-weight: 700; margin-bottom: 6px; }}
    label {{ display: block; margin: 7px 0; }}
    select, button {{ border: 1px solid #8886; border-radius: 8px; padding: 9px 10px; color: inherit; background: transparent; font: inherit; }}
    table {{ width: 100%; border-collapse: collapse; border: 1px solid #8884; border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #8883; padding: 8px; text-align: left; overflow-wrap: anywhere; }}
    th {{ color: #777; font-weight: 600; }}
    .meta {{ color: #777; font-size: 13px; }}
  </style>
  <script>
    async function refreshConsoleStatus() {{
      try {{
        const response = await fetch('/console/status', {{cache: 'no-store'}});
        if (!response.ok) return;
        const status = await response.json();
        const pending = document.querySelector('[data-console="pending-actions"]');
        const apply = document.querySelector('[data-console="heartbeat-apply"]');
        if (pending) pending.textContent = String(status.pending_actions);
        if (apply) {{
          apply.textContent = status.heartbeat_apply_status;
          apply.className = 'value ' + (status.heartbeat_apply_pending ? 'pending' : 'ok');
        }}
      }} catch (_error) {{}}
    }}
    window.setInterval(refreshConsoleStatus, 5000);
  </script>
</head>
<body>
  <main>
    <h1>AgentWatcher Console</h1>
    {'<div class="saved">设置已保存</div>' if saved else ''}
    <div class="notice">测试命令通知默认关闭。它只表示某个工具命令结束，不代表 Codex 整轮任务完成。</div>
    <section class="grid" aria-label="状态概览">
      <div class="panel"><div class="label">Bark</div><div class="value">{"Bark 已配置" if bark_ready else "未配置"}</div></div>
      <div class="panel"><div class="label">Watcher</div><div class="value">{html.escape(watcher_status)}</div></div>
      <div class="panel"><div class="label">Web 地址</div><div class="value">{html.escape(web_url)}</div></div>
      <div class="panel"><div class="label">待处理手机回复</div><div class="value" data-console="pending-actions">{pending_count}</div></div>
      <div class="panel"><div class="label">远程交互</div><div class="value">{html.escape(remote_interaction_status_text(config))}</div></div>
      <div class="panel"><div class="label">手机回复自动同步</div><div class="value">{html.escape(reply_heartbeat_status_text(config))}</div></div>
      <div class="panel"><div class="label">Codex 自动任务状态</div><div class="value {"pending" if heartbeat.get("automation_apply_status") == "pending" else "ok"}" data-console="heartbeat-apply">{html.escape(heartbeat_apply_text)}</div></div>
      <div class="panel"><div class="label">测试命令通知</div><div class="value">{"已开启" if test_enabled else "默认关闭"}</div></div>
    </section>
    <h2>通知设置</h2>
    <form method="post" action="/console">
      <fieldset>
        <legend>远程交互模式</legend>
        <label><input type="radio" name="remote_mode" value="reply" {"checked" if remote_mode == "reply" else ""}> 可回复</label>
        <label><input type="radio" name="remote_mode" value="read_only" {"checked" if remote_mode == "read_only" else ""}> 只读</label>
      </fieldset>
      <fieldset>
        <legend>中间测试命令通知</legend>
        <label><input type="checkbox" name="notify_on_test_done" value="on" {"checked" if test_enabled else ""}> 每个测试命令结束时推送</label>
        <p class="meta">关闭后仍会收到最终任务完成通知。</p>
      </fieldset>
      <fieldset>
        <legend>手机回复自动同步</legend>
        <select name="reply_interval">{interval_options}</select>
        <p class="meta">保存后会先进入“待应用到 Codex 自动任务”。只有当前 Codex 会话实际更新 heartbeat 后，才会显示“已应用”。</p>
      </fieldset>
      <button type="submit">保存设置</button>
    </form>
    <h2>Bark 通知头像</h2>
    <form method="post" action="/console/icon" enctype="multipart/form-data">
      <fieldset>
        <legend>自定义头像</legend>
        {f'<p><img class="icon-preview" src="{html.escape(icon_url, quote=True)}" alt="当前头像">{html.escape(icon_status_text)}</p>' if icon_url else '<p class="meta">当前未设置通知头像。</p>'}
        <label>上传图片 <input type="file" name="icon_file" accept="image/png,image/jpeg,image/webp,image/gif"></label>
        <p class="meta">默认使用 OpenAI Codex 图标。支持上传 PNG、JPG、WEBP、GIF，最大 2 MB；上传后会覆盖默认头像。</p>
      </fieldset>
      <button type="submit">上传并更新头像</button>
    </form>
    <h2>最近事件</h2>
    <table>
      <thead><tr><th>时间</th><th>类型</th><th>状态</th><th>原因</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </main>
</body>
</html>
"""


def update_config_from_console_form(config: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    set_remote_interaction_mode(config, str(form.get("remote_mode") or remote_interaction_mode(config)))
    policy = config.setdefault("notification_policy", {})
    policy["notify_on_test_done"] = str(form.get("notify_on_test_done") or "").lower() in {"on", "true", "1", "yes"}
    interval_raw = str(form.get("reply_interval") or "0").strip()
    try:
        interval = int(interval_raw)
    except ValueError:
        interval = 0
    set_reply_heartbeat(config, interval)
    return config


def append_web_audit(
    data_dir: Path,
    *,
    ip: str,
    method: str,
    path: str,
    detail_id: str = "",
    action: str = "",
    result: str = "",
    reason: str = "",
) -> None:
    entry = {
        "timestamp": timestamp_iso(),
        "ip": str(ip or ""),
        "method": str(method or "").upper(),
        "path": urllib.parse.urlparse(str(path or "")).path,
        "detail_id": str(detail_id or ""),
        "action": str(action or ""),
        "result": str(result or ""),
    }
    if reason:
        safe_reason = str(reason)
        if "signature" in safe_reason or "token" in safe_reason:
            safe_reason = "invalid_auth"
        entry["reason"] = safe_reason
    data_dir.mkdir(parents=True, exist_ok=True)
    with web_audit_path(data_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record_action(data_dir: Path, detail_id: str, action: str, client: str = "") -> dict[str, Any]:
    action = str(action).strip().lower()
    if action not in ACTION_LABELS:
        raise ValueError("Unsupported action.")
    timestamp = timestamp_iso()
    entry = {
        "timestamp": timestamp,
        "detail_id": str(detail_id),
        "action": action,
        "client": str(client or ""),
    }
    entry.update(action_context(data_dir, detail_id))
    entry["action"] = action
    entry["client"] = str(client or "")
    entry["action_id"] = make_action_id(entry)
    data_dir.mkdir(parents=True, exist_ok=True)
    with actions_path(data_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def record_reply(data_dir: Path, detail_id: str, reply: str, client: str = "") -> dict[str, Any]:
    reply = truncate_text(str(reply or "").strip(), MAX_REPLY_CHARS)
    if not reply:
        raise ValueError("Reply is empty.")
    timestamp = timestamp_iso()
    entry = {
        "timestamp": timestamp,
        "detail_id": str(detail_id),
        "action": "reply",
        "reply": reply,
        "client": str(client or ""),
    }
    entry.update(action_context(data_dir, detail_id))
    entry["action"] = "reply"
    entry["reply"] = reply
    entry["client"] = str(client or "")
    entry["action_id"] = make_action_id(entry)
    data_dir.mkdir(parents=True, exist_ok=True)
    with actions_path(data_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def make_action_id(entry: dict[str, Any]) -> str:
    keys = ("timestamp", "detail_id", "thread_id", "action", "reply", "client")
    raw = "\n".join(str(entry.get(key) or "") for key in keys)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def parse_form_body(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    length = max(0, min(length, MAX_FORM_BYTES))
    body = handler.rfile.read(length).decode("utf-8", errors="replace") if length else ""
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def parse_multipart_file(handler: BaseHTTPRequestHandler, field_name: str, max_bytes: int) -> dict[str, Any]:
    content_type = str(handler.headers.get("Content-Type") or "")
    match = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    if not match:
        raise ValueError("Missing multipart boundary.")
    boundary = match.group(1).encode("utf-8")
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        raise ValueError("Upload body is empty.")
    if length > max_bytes + 8192:
        raise ValueError("Upload is too large.")
    body = handler.rfile.read(length)
    delimiter = b"--" + boundary
    for part in body.split(delimiter):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        header_blob, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers: dict[str, str] = {}
        for raw_line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            key, sep, value = raw_line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        if f'name="{field_name}"' not in disposition:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        data = data.rstrip(b"\r\n")
        if len(data) > max_bytes:
            raise ValueError("Upload is too large.")
        return {
            "filename": filename_match.group(1) if filename_match else "",
            "content_type": headers.get("content-type", ""),
            "data": data,
        }
    raise ValueError("Icon file was not found in upload.")


def read_actions(data_dir: Path) -> list[dict[str, Any]]:
    path = actions_path(data_dir)
    if not path.exists():
        return []
    actions: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            actions.append(entry)
    return actions


def pending_actions(data_dir: Path, thread_id: str = "", source_path: str = "") -> list[dict[str, Any]]:
    thread_id = str(thread_id or "").strip()
    source_path = str(source_path or "").strip()
    pending: list[dict[str, Any]] = []
    for entry in read_actions(data_dir):
        if entry.get("dispatched_at"):
            continue
        if thread_id and str(entry.get("thread_id") or "") != thread_id:
            continue
        if source_path and str(entry.get("source_path") or "") != source_path:
            continue
        pending.append(entry)
    return pending


def mark_action_dispatched(data_dir: Path, action_id: str, thread_id: str = "") -> bool:
    action_id = str(action_id or "").strip()
    if not action_id:
        return False
    entries = read_actions(data_dir)
    changed = False
    for entry in entries:
        if str(entry.get("action_id") or "") != action_id:
            continue
        if thread_id and str(entry.get("thread_id") or "") != str(thread_id):
            continue
        entry["dispatched_at"] = timestamp_iso()
        if thread_id:
            entry["dispatched_thread_id"] = str(thread_id)
        changed = True
        break
    if not changed:
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    with actions_path(data_dir).open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def make_http_handler(data_dir: Path, access_token: str = "") -> type[BaseHTTPRequestHandler]:
    class AgentWatcherHandler(BaseHTTPRequestHandler):
        server_version = "AgentWatcherHTTP/0.1"

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def write_response(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def write_json(self, status: int, payload: dict[str, Any]) -> None:
            self.write_response(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

        def write_bytes(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)

        def client_ip(self) -> str:
            try:
                return str(self.client_address[0])
            except (AttributeError, IndexError, TypeError):
                return ""

        def audit(self, method: str, path: str, detail_id: str = "", action: str = "", result: str = "", reason: str = "") -> None:
            append_web_audit(
                data_dir,
                ip=self.client_ip(),
                method=method,
                path=path,
                detail_id=detail_id,
                action=action,
                result=result,
                reason=reason,
            )

        def signature_config(self) -> dict[str, Any]:
            config = load_config(data_dir)
            web = config.setdefault("web", {})
            if access_token:
                web["access_token"] = access_token
            return config

        def verify_request_signature(self, parsed: urllib.parse.ParseResult, query: dict[str, list[str]], detail_id: str, action: str, method: str) -> bool:
            config = self.signature_config()
            ok, reason = verify_signed_web_request(config, parsed.path, query)
            if not ok:
                if reason == "expired":
                    self.audit(method, parsed.path, detail_id=detail_id, action=action, result="expired")
                    self.write_response(410, expired_page())
                else:
                    self.audit(method, parsed.path, detail_id=detail_id, action=action, result="forbidden", reason=reason)
                    self.write_response(403, "<!doctype html><meta charset=\"utf-8\"><p>访问签名无效。</p>")
                return False
            signed_detail_id = (query.get("detail_id") or [""])[0].strip()
            if signed_detail_id and signed_detail_id != detail_id:
                self.audit(method, parsed.path, detail_id=detail_id, action=action, result="forbidden", reason="detail_mismatch")
                self.write_response(403, "<!doctype html><meta charset=\"utf-8\"><p>访问签名无效或已过期。</p>")
                return False
            return True

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] == "assets":
                filename = Path(parts[1]).name
                ext = Path(filename).suffix.lower()
                if ext not in ICON_EXTENSIONS:
                    self.write_response(404, "<!doctype html><meta charset=\"utf-8\"><p>未找到。</p>")
                    return
                path = assets_dir(data_dir) / filename
                if path.exists() and path.is_file():
                    self.write_bytes(200, path.read_bytes(), ICON_EXTENSIONS[ext])
                    return
                self.write_response(404, "<!doctype html><meta charset=\"utf-8\"><p>未找到。</p>")
                return
            if parsed.path == "/console/status":
                config = load_config(data_dir)
                heartbeat = reply_heartbeat_status(config)
                self.audit("GET", parsed.path, action="console_status", result="ok")
                self.write_json(
                    200,
                    {
                        "pending_actions": len(pending_actions(data_dir)),
                        "heartbeat": heartbeat,
                        "heartbeat_text": reply_heartbeat_status_text(config),
                        "heartbeat_apply_status": reply_heartbeat_apply_status_text(config),
                        "heartbeat_apply_pending": heartbeat.get("automation_apply_status") == "pending",
                        "remote_mode": remote_interaction_mode(config),
                        "notify_on_test_done": bool(config.get("notification_policy", {}).get("notify_on_test_done", False)),
                    },
                )
                return
            if parsed.path in ("", "/", "/console"):
                config = load_config(data_dir)
                if access_token:
                    config.setdefault("web", {})["access_token"] = access_token
                self.audit("GET", parsed.path or "/console", action="console", result="ok")
                self.write_response(200, console_page(data_dir, config))
                return
            if len(parts) == 3 and parts[0] == "action":
                detail_id, action = parts[1], parts[2].strip().lower()
                if not self.verify_request_signature(parsed, query, detail_id, action, "GET"):
                    return
                if not remote_interaction_allows_reply(load_config(data_dir)):
                    self.audit("GET", parsed.path, detail_id=detail_id, action=action, result="forbidden", reason="read_only")
                    self.write_response(403, read_only_page())
                    return
                detail_path = details_dir(data_dir) / f"{Path(detail_id).name}.html"
                if detail_is_expired(data_dir, detail_id, detail_path=detail_path):
                    self.audit("GET", parsed.path, detail_id=detail_id, action=action, result="expired")
                    self.write_response(410, expired_page())
                    return
                if action not in ACTION_LABELS:
                    self.audit("GET", parsed.path, detail_id=detail_id, action=action, result="bad_request", reason="unsupported_action")
                    self.write_response(400, "<!doctype html><meta charset=\"utf-8\"><p>不支持的动作。</p>")
                    return
                config = load_config(data_dir)
                if access_token:
                    config.setdefault("web", {})["access_token"] = access_token
                form_action = signed_action_path(config, parsed.path, detail_id, data_dir=data_dir)
                warning = reply_heartbeat_off_warning(config)
                message = f"你将记录操作：{ACTION_LABELS[action]}\n详情 ID：{detail_id}\n确认后 Codex 会在本地队列中看到这条操作。"
                self.audit("GET", parsed.path, detail_id=detail_id, action=action, result="ok")
                self.write_response(200, confirmation_page("确认操作", message, form_action, {}, warning=warning))
                return
            if len(parts) == 2 and parts[0] == "details":
                detail_name = Path(parts[1]).name
                detail_id = detail_name[:-5] if detail_name.endswith(".html") else detail_name
                if not self.verify_request_signature(parsed, query, detail_id, "view", "GET"):
                    return
                detail_path = details_dir(data_dir) / detail_name
                if detail_is_expired(data_dir, detail_id, detail_path=detail_path):
                    self.audit("GET", parsed.path, detail_id=detail_id, action="view", result="expired")
                    self.write_response(410, expired_page())
                    return
                if detail_path.exists() and detail_path.is_file():
                    self.audit("GET", parsed.path, detail_id=detail_id, action="view", result="ok")
                    self.write_response(200, detail_path.read_text(encoding="utf-8"))
                    return
            self.write_response(404, "<!doctype html><meta charset=\"utf-8\"><p>未找到。</p>")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
            form = {} if parsed.path == "/console/icon" else parse_form_body(self)
            if parsed.path == "/console":
                config = load_config(data_dir)
                update_config_from_console_form(config, form)
                if access_token:
                    config.setdefault("web", {})["access_token"] = access_token
                save_config(data_dir, config)
                self.audit("POST", parsed.path, action="console_settings", result="ok")
                self.write_response(200, console_page(data_dir, config, saved=True))
                return
            if parsed.path == "/console/icon":
                try:
                    upload = parse_multipart_file(self, "icon_file", MAX_ICON_BYTES)
                    config = load_config(data_dir)
                    if access_token:
                        config.setdefault("web", {})["access_token"] = access_token
                    save_custom_icon(data_dir, config, upload)
                    save_config(data_dir, config)
                except ValueError as exc:
                    self.audit("POST", parsed.path, action="console_icon", result="bad_request", reason=str(exc))
                    self.write_response(400, f"<!doctype html><meta charset=\"utf-8\"><p>{html.escape(str(exc))}</p>")
                    return
                self.audit("POST", parsed.path, action="console_icon", result="ok")
                self.write_response(200, console_page(data_dir, config, saved=True))
                return
            if len(parts) == 3 and parts[0] == "action":
                detail_id, action = parts[1], parts[2].strip().lower()
                if not self.verify_request_signature(parsed, query, detail_id, action, "POST"):
                    return
                if not remote_interaction_allows_reply(load_config(data_dir)):
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="forbidden", reason="read_only")
                    self.write_response(403, read_only_page())
                    return
                detail_path = details_dir(data_dir) / f"{Path(detail_id).name}.html"
                if detail_is_expired(data_dir, detail_id, detail_path=detail_path):
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="expired")
                    self.write_response(410, expired_page())
                    return
                if form.get("confirm") != "yes":
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="bad_request", reason="missing_confirm")
                    self.write_response(400, "<!doctype html><meta charset=\"utf-8\"><p>缺少确认。</p>")
                    return
                try:
                    record_action(data_dir, detail_id, action, client=self.client_address[0])
                except ValueError:
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="bad_request", reason="unsupported_action")
                    self.write_response(400, "<!doctype html><meta charset=\"utf-8\"><p>不支持的动作。</p>")
                    return
                self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="ok")
                self.write_response(200, success_page("已记录，回到 Codex 后会处理。"))
                return
            if len(parts) == 3 and parts[0] == "reply":
                detail_id, phase = parts[1], parts[2].strip().lower()
                action = "reply_confirm" if phase == "confirm" else "reply"
                if not self.verify_request_signature(parsed, query, detail_id, action, "POST"):
                    return
                if not remote_interaction_allows_reply(load_config(data_dir)):
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="forbidden", reason="read_only")
                    self.write_response(403, read_only_page())
                    return
                detail_path = details_dir(data_dir) / f"{Path(detail_id).name}.html"
                if detail_is_expired(data_dir, detail_id, detail_path=detail_path):
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="expired")
                    self.write_response(410, expired_page())
                    return
                reply = truncate_text(str(form.get("reply") or "").strip(), MAX_REPLY_CHARS)
                if not reply:
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="bad_request", reason="empty_reply")
                    self.write_response(400, "<!doctype html><meta charset=\"utf-8\"><p>回复内容不能为空。</p>")
                    return
                if phase == "confirm":
                    config = load_config(data_dir)
                    if access_token:
                        config.setdefault("web", {})["access_token"] = access_token
                    form_action = signed_action_path(config, f"/reply/{urllib.parse.quote(detail_id, safe='')}/submit", detail_id, data_dir=data_dir)
                    warning = reply_heartbeat_off_warning(config)
                    message = f"你将把下面这段内容发回 Codex：\n\n{reply}\n\n确认后内容会写入本地队列，不会自动执行命令。"
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="ok")
                    self.write_response(200, confirmation_page("确认发送回复", message, form_action, {"reply": reply}, warning=warning))
                    return
                if phase == "submit":
                    if form.get("confirm") != "yes":
                        self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="bad_request", reason="missing_confirm")
                        self.write_response(400, "<!doctype html><meta charset=\"utf-8\"><p>缺少确认。</p>")
                        return
                    try:
                        record_reply(data_dir, detail_id, reply, client=self.client_address[0])
                    except ValueError:
                        self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="bad_request", reason="empty_reply")
                        self.write_response(400, "<!doctype html><meta charset=\"utf-8\"><p>回复内容不能为空。</p>")
                        return
                    self.audit("POST", parsed.path, detail_id=detail_id, action=action, result="ok")
                    self.write_response(200, success_page("已记录，回到 Codex 后会处理。"))
                    return
            self.write_response(404, "<!doctype html><meta charset=\"utf-8\"><p>未找到。</p>")

    return AgentWatcherHandler


def serve_web(data_dir: Path, host: str, port: int, access_token: str = "") -> None:
    server = ThreadingHTTPServer((host, port), make_http_handler(data_dir, access_token=access_token))
    print(f"[AgentWatcher] Web console listening at http://{host}:{port}", flush=True)
    server.serve_forever()


def send_bark(title: str, body: str, level: str, notifier: dict[str, Any], url: str = "") -> bool:
    key = str(notifier.get("bark_key", "")).strip()
    if not key:
        print("[AgentWatcher] Bark key is not configured.", file=sys.stderr)
        return False
    try:
        message = {
            "title": title,
            "body": body,
            "group": str(notifier.get("group", "Codex")),
            "level": level,
        }
        if url:
            message["url"] = url
        icon_url = str(notifier.get("icon_url") or "").strip()
        endpoint = bark_endpoint(notifier)
        if icon_url:
            endpoint = endpoint + "?" + urllib.parse.urlencode({"icon": icon_url})
        payload = json.dumps(
            message,
            ensure_ascii=True,
        ).encode("ascii")
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return 200 <= response.status < 300
            return data.get("code") == 200 or 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[AgentWatcher] Bark push failed: {exc}", file=sys.stderr)
        return False


def should_notify(event_type: str, config: dict[str, Any], payload: dict[str, Any] | None = None) -> bool:
    policy = config.get("notification_policy", {})
    if event_type == "PermissionRequest":
        return bool(policy.get("notify_on_permission_request", True))
    if event_type == "Stop":
        return bool(policy.get("notify_on_stop", True))
    if event_type == "PostToolUse":
        return bool(policy.get("notify_on_test_done", False)) and is_test_command(payload or {}, config)
    if event_type == "test_done":
        return bool(policy.get("notify_on_test_done", False))
    return policy.get("mode", "actionable") == "verbose"


def cooldown_allows(data_dir: Path, event_type: str, config: dict[str, Any]) -> bool:
    cooldown = int(config.get("notification_policy", {}).get("cooldown_seconds", 45))
    if cooldown <= 0:
        return True
    state = load_state(data_dir)
    last_sent = state.get("last_sent", {})
    last = float(last_sent.get(event_type, 0) or 0)
    return time.time() - last >= cooldown


def mark_sent(data_dir: Path, event_type: str) -> None:
    state = load_state(data_dir)
    last_sent = state.setdefault("last_sent", {})
    last_sent[event_type] = time.time()
    save_state(data_dir, state)


def watcher_is_running(data_dir: Path) -> bool:
    pid_file = data_dir / "watcher.pid"
    try:
        raw = pid_file.read_text(encoding="ascii").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def windows_process_exists(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def recent_session_files(sessions_dir: Path, limit: int = 80) -> list[Path]:
    if not sessions_dir.exists():
        return []
    files = [path for path in sessions_dir.rglob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:limit]


def parse_task_complete_record(record: dict[str, Any], path: Path) -> dict[str, Any] | None:
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "task_complete":
        return None
    event = dict(payload)
    event["timestamp"] = record.get("timestamp")
    event["source_path"] = str(path)
    return event


def iter_task_complete_events(sessions_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in reversed(recent_session_files(sessions_dir)):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            continue
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = parse_task_complete_record(record, path)
            if event is None:
                continue
            event["source_line"] = index + 1
            events.append(event)
    return events


def task_complete_id(event: dict[str, Any]) -> str:
    turn_id = event.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    source_offset = event.get("source_offset")
    if isinstance(source_offset, int):
        return f"{event.get('source_path', '')}:{source_offset}"
    return f"{event.get('source_path', '')}:{event.get('source_line', '')}"


def session_file_date(event: dict[str, Any]) -> datetime | None:
    source_path = str(event.get("source_path") or event.get("transcript_path") or "")
    match = re.search(r"[\\/](\d{4})[\\/](\d{2})[\\/](\d{2})[\\/][^\\/]+\.jsonl$", source_path)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def session_file_age_seconds(event: dict[str, Any], now: datetime | None = None) -> float | None:
    session_date = session_file_date(event)
    if session_date is None:
        return None
    return ((now or utc_now()) - session_date).total_seconds()


def old_session_file(event: dict[str, Any], config: dict[str, Any], now: datetime | None = None) -> bool:
    max_age = int(config.get("notification_policy", {}).get("max_session_file_age_seconds", 604800))
    if max_age <= 0:
        return False
    age = session_file_age_seconds(event, now=now)
    return age is not None and age > max_age


def task_complete_age_seconds(event: dict[str, Any], now: datetime | None = None) -> float | None:
    event_time = parse_timestamp(event.get("timestamp")) or parse_timestamp(event.get("completed_at"))
    if event_time is None:
        return None
    return ((now or utc_now()) - event_time).total_seconds()


def stale_task_complete(event: dict[str, Any], config: dict[str, Any], now: datetime | None = None) -> bool:
    max_age = int(config.get("notification_policy", {}).get("max_task_complete_age_seconds", 1800))
    if max_age <= 0:
        return False
    age = task_complete_age_seconds(event, now=now)
    return age is not None and age > max_age


def session_file_offsets(data_dir: Path) -> dict[str, int]:
    state = load_state(data_dir)
    raw = state.get("session_file_offsets", {})
    if not isinstance(raw, dict):
        return {}
    offsets: dict[str, int] = {}
    for path, offset in raw.items():
        try:
            offsets[str(path)] = max(0, int(offset))
        except (TypeError, ValueError):
            continue
    return offsets


def save_session_file_offsets(data_dir: Path, offsets: dict[str, int]) -> None:
    state = load_state(data_dir)
    state["session_file_offsets"] = {
        str(path): max(0, int(offset))
        for path, offset in sorted(offsets.items())
    }
    save_state(data_dir, state)


def seen_task_complete_ids(data_dir: Path) -> set[str]:
    state = load_state(data_dir)
    raw = state.get("seen_task_complete_ids", [])
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return set()


def save_seen_task_complete_ids(data_dir: Path, seen: set[str]) -> None:
    state = load_state(data_dir)
    state["seen_task_complete_ids"] = sorted(seen)[-5000:]
    save_state(data_dir, state)


def completion_dedupe_id(payload: dict[str, Any]) -> str:
    for key in ("turn_id", "completion_id", "task_complete_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    parts: list[str] = []
    for key in ("session_id", "transcript_path", "source_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    message = (
        payload.get("last_agent_message")
        or payload.get("last_assistant_message")
        or payload.get("message")
        or payload.get("task_title")
    )
    if isinstance(message, str) and message.strip():
        parts.append(recover_mojibake_text(message).strip())

    if not parts:
        return ""
    digest = hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()
    return f"fingerprint:{digest[:24]}"


def completion_seen(data_dir: Path, completion_id: str) -> bool:
    return bool(completion_id) and completion_id in seen_task_complete_ids(data_dir)


def mark_completion_seen(data_dir: Path, completion_id: str) -> None:
    if not completion_id:
        return
    seen = seen_task_complete_ids(data_dir)
    seen.add(completion_id)
    save_seen_task_complete_ids(data_dir, seen)


def read_task_complete_events_since_offsets(
    sessions_dir: Path,
    offsets: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    updated_offsets = dict(offsets)
    for path in reversed(recent_session_files(sessions_dir)):
        path_key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = updated_offsets.get(path_key, 0)
        if offset < 0 or offset > size:
            offset = 0
        if offset == size:
            updated_offsets[path_key] = size
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            continue

        parse_bytes = data
        read_until = size
        if data and not data.endswith(b"\n"):
            last_newline = data.rfind(b"\n")
            if last_newline == -1:
                parse_bytes = b""
                read_until = offset
            else:
                parse_bytes = data[: last_newline + 1]
                read_until = offset + last_newline + 1

        byte_position = 0
        for line_bytes in parse_bytes.splitlines(keepends=True):
            line = line_bytes.decode("utf-8-sig", errors="replace").strip()
            source_offset = offset + byte_position
            byte_position += len(line_bytes)
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = parse_task_complete_record(record, path)
            if event is None:
                continue
            event["source_offset"] = source_offset
            events.append(event)
        updated_offsets[path_key] = read_until
    return events, updated_offsets


def mark_existing_task_completes_seen(data_dir: Path, sessions_dir: Path) -> int:
    offsets = session_file_offsets(data_dir)
    changed = 0
    for path in recent_session_files(sessions_dir):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        path_key = str(path)
        if offsets.get(path_key) != size:
            changed += 1
        offsets[path_key] = size
    save_session_file_offsets(data_dir, offsets)
    return changed


def process_new_task_completes(data_dir: Path, config: dict[str, Any], sessions_dir: Path) -> int:
    notify_enabled = bool(config.get("notification_policy", {}).get("notify_on_stop", True))
    if config.get("web", {}).get("enabled", True):
        ensure_web_token(data_dir, config)
    offsets = session_file_offsets(data_dir)
    events, updated_offsets = read_task_complete_events_since_offsets(sessions_dir, offsets)
    seen = seen_task_complete_ids(data_dir)
    processed = 0
    for event in events:
        event_id = task_complete_id(event)
        if event_id in seen:
            continue
        age_seconds = task_complete_age_seconds(event)
        if stale_task_complete(event, config):
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": "task_complete",
                    "producer": "watcher",
                    "script_sha256": script_fingerprint(),
                    "sent": False,
                    "reason": "skipped_stale_task_complete",
                    "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                    "raw_event": event,
                },
                config,
            )
            seen.add(event_id)
            continue
        session_age_seconds = session_file_age_seconds(event)
        if old_session_file(event, config):
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": "task_complete",
                    "producer": "watcher",
                    "script_sha256": script_fingerprint(),
                    "sent": False,
                    "reason": "skipped_old_session_file",
                    "session_file_age_seconds": round(session_age_seconds, 3) if session_age_seconds is not None else None,
                    "raw_event": event,
                },
                config,
            )
            seen.add(event_id)
            continue
        if notify_enabled:
            last_message = recover_mojibake_text(str(event.get("last_agent_message") or "本轮任务已完成"))
            payload = {
                "task_title": compact_text_summary(str(last_message), 34),
                "result": "已完成",
                "action": "回来验收",
                "last_assistant_message": last_message,
            }
            title, body, level = build_message("Stop", payload, config)
            detail = write_detail_page(data_dir, event, title, body, config) if config.get("web", {}).get("enabled", True) else {}
            sent = send_bark(title, body, level, config.get("notifier", {}), url=detail.get("url", ""))
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": "task_complete",
                    "producer": "watcher",
                    "script_sha256": script_fingerprint(),
                    "title": title,
                    "body": body,
                    "level": level,
                    "detail": detail,
                    "sent": sent,
                    "raw_event": event,
                },
                config,
            )
            processed += 1
        seen.add(event_id)
    if events:
        save_seen_task_complete_ids(data_dir, seen)
    save_session_file_offsets(data_dir, updated_offsets)
    return processed


def cmd_setup(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    server, key = parse_bark_input(args.bark_url)
    notifier = config.setdefault("notifier", {})
    if server:
        notifier["bark_server"] = server
    notifier["bark_key"] = key
    save_config(data_dir, config)
    print(f"[AgentWatcher] Bark key configured at {config_path(data_dir)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    try:
        config = load_config(data_dir)
    except RuntimeError as exc:
        print(f"[AgentWatcher] {exc}", file=sys.stderr)
        return 1
    key = str(config.get("notifier", {}).get("bark_key", "")).strip()
    if not key:
        print("[AgentWatcher] Bark key: missing")
        print(f"[AgentWatcher] Config path: {config_path(data_dir)}")
        return 1
    print("[AgentWatcher] Bark key: configured")
    print(f"[AgentWatcher] Config path: {config_path(data_dir)}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    title = "Codex Bark 测试"
    body = "如果 iPhone 或 Apple Watch 收到这条消息，说明通知链路已打通。"
    level = config.get("notifier", {}).get("default_level", "active")
    sent = False if args.dry_run else send_bark(title, body, level, config.get("notifier", {}))
    append_event(data_dir, {"timestamp": timestamp_iso(), "event_type": "test", "title": title, "body": body, "sent": sent}, config)
    return 0 if (sent or args.dry_run) else 1


def cmd_send(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    body = redact_secrets(args.body) if config.get("privacy", {}).get("redact_secrets", True) else args.body
    body = truncate_text(body, int(config.get("privacy", {}).get("max_body_chars", 240)))
    level = args.level or config.get("notifier", {}).get("default_level", "active")
    sent = False if args.dry_run else send_bark(args.title, body, level, config.get("notifier", {}))
    append_event(
        data_dir,
        {"timestamp": timestamp_iso(), "event_type": args.event, "title": args.title, "body": body, "level": level, "sent": sent},
        config,
    )
    return 0 if (sent or args.dry_run) else 1


def cmd_hook(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    try:
        config = load_config(data_dir)
        payload = read_stdin_json()
        event_type = "test_done" if args.event == "PostToolUse" and is_test_command(payload, config) else args.event
        completion_id = completion_dedupe_id(payload) if event_type == "Stop" else ""
        if not should_notify(args.event, config, payload):
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": args.event,
                    "producer": "hook",
                    "script_sha256": script_fingerprint(),
                    "sent": False,
                    "reason": "disabled",
                },
                config,
            )
            return 0
        if event_type == "Stop" and watcher_is_running(data_dir):
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": event_type,
                    "producer": "hook",
                    "script_sha256": script_fingerprint(),
                    "sent": False,
                    "reason": "deferred_to_watcher",
                    "raw_event": payload,
                },
                config,
            )
            print(json.dumps({"continue": True}, ensure_ascii=False))
            return 0
        title, body, level = build_message(event_type, payload, config)
        sent = False
        skipped_reason = ""
        if completion_id and completion_seen(data_dir, completion_id):
            skipped_reason = "duplicate_completion"
        elif cooldown_allows(data_dir, event_type, config):
            sent = send_bark(title, body, level, config.get("notifier", {}))
            if completion_id:
                mark_completion_seen(data_dir, completion_id)
            if sent:
                mark_sent(data_dir, event_type)
        append_event(
            data_dir,
            {
                "timestamp": timestamp_iso(),
                "event_type": event_type,
                "producer": "hook",
                "script_sha256": script_fingerprint(),
                "title": title,
                "body": body,
                "level": level,
                "sent": sent,
                "reason": skipped_reason,
                "raw_event": payload,
            },
            config,
        )
    except Exception as exc:
        print(f"[AgentWatcher] hook error: {exc}", file=sys.stderr)
    if args.event == "Stop":
        print(json.dumps({"continue": True}, ensure_ascii=False))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    path = log_path(Path(args.data_dir))
    if not path.exists():
        print("[AgentWatcher] No events logged yet.")
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()[-args.tail :]
    for line in lines:
        print(line)
    return 0


def cmd_actions(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    thread_id = "" if args.all else (args.thread_id or os.environ.get("CODEX_THREAD_ID", ""))
    if args.mark_dispatched:
        marked = mark_action_dispatched(data_dir, args.mark_dispatched, thread_id=thread_id)
        print(json.dumps({"marked": marked, "action_id": args.mark_dispatched}, ensure_ascii=False))
        return 0 if marked else 1
    source_path = "" if args.all else args.source_path
    actions = pending_actions(data_dir, thread_id=thread_id, source_path=source_path)
    if args.format == "jsonl":
        for entry in actions:
            print(json.dumps(entry, ensure_ascii=False))
    else:
        print(json.dumps(actions, ensure_ascii=False, indent=2))
    return 0


def cmd_reply_heartbeat(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    interval_arg = args.interval
    if args.off:
        interval = 0
    elif interval_arg is not None:
        interval = int(interval_arg)
    else:
        if auto_disable_expired_reply_heartbeat(config):
            save_config(data_dir, config)
        status = reply_heartbeat_status(config)
        if args.format == "json":
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print_reply_heartbeat_status(status)
        return 0
    try:
        status = set_reply_heartbeat(config, interval)
    except ValueError as exc:
        print(f"[AgentWatcher] {exc}", file=sys.stderr)
        return 2
    save_config(data_dir, config)
    if args.format == "json":
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print_reply_heartbeat_status(status)
    return 0


def cmd_automation_sync(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    if args.mark_applied:
        try:
            status = mark_reply_heartbeat_application_applied(config, args.mark_applied, automation_id=args.automation_id)
        except ValueError as exc:
            if args.format == "json":
                print(json.dumps({"marked": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            else:
                print(f"[AgentWatcher] {exc}", file=sys.stderr)
            return 1
        save_config(data_dir, config)
        payload = pending_reply_heartbeat_application(config)
        payload.update(status)
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[AgentWatcher] {reply_heartbeat_apply_status_text(config)}")
        return 0

    if auto_disable_expired_reply_heartbeat(config):
        save_config(data_dir, config)
    request = pending_reply_heartbeat_application(config)
    if args.format == "json":
        print(json.dumps(request, ensure_ascii=False, indent=2))
    else:
        if request.get("pending"):
            print(f"[AgentWatcher] 待应用到 Codex 自动任务：{request['action']} {request['rrule'] or '关闭'}")
            print(f"[AgentWatcher] request_id: {request['request_id']}")
        else:
            print(f"[AgentWatcher] {reply_heartbeat_apply_status_text(config)}")
    return 0


def print_reply_heartbeat_status(status: dict[str, Any]) -> None:
    if status.get("enabled"):
        print(f"[AgentWatcher] 手机回复自动同步：开启，每 {status['interval_minutes']} 分钟检查一次。")
        print(f"[AgentWatcher] Codex heartbeat RRULE: {status['rrule']}")
        if status.get("automation_apply_status") == "pending":
            print("[AgentWatcher] 已保存目标设置，仍待应用到 Codex 自动任务。")
    else:
        print("[AgentWatcher] 手机回复自动同步：关闭。")
        print("[AgentWatcher] 关闭后不会定时唤醒 Codex，也不会产生待机 token 消耗。")
    print("[AgentWatcher] 配置已写入本机 config.json。")


def cmd_remote_mode(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    if args.read_only:
        status = set_remote_interaction_mode(config, "read_only")
        save_config(data_dir, config)
    elif args.reply:
        status = set_remote_interaction_mode(config, "reply")
        save_config(data_dir, config)
    else:
        status = {
            "mode": remote_interaction_mode(config),
            "allows_reply": remote_interaction_allows_reply(config),
            "status": remote_interaction_status_text(config),
            "hint": remote_interaction_hint_text(config),
        }
    if args.format == "json":
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"[AgentWatcher] {status['status']}")
        print(f"[AgentWatcher] {status['hint']}")
        print("[AgentWatcher] 配置已写入本机 config.json。")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    sessions_dir = Path(args.sessions_dir)
    try:
        config = load_config(data_dir)
    except RuntimeError as exc:
        print(f"[AgentWatcher] {exc}", file=sys.stderr)
        return 1
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.baseline:
        marked = mark_existing_task_completes_seen(data_dir, sessions_dir)
        print(f"[AgentWatcher] Baseline marked {marked} existing session file(s).", flush=True)
    print(f"[AgentWatcher] Watching Codex sessions at {sessions_dir}", flush=True)
    while True:
        try:
            config = load_config(data_dir)
        except RuntimeError as exc:
            print(f"[AgentWatcher] {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1
            time.sleep(max(1.0, float(args.interval)))
            continue
        process_new_task_completes(data_dir, config, sessions_dir)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval)))


def cmd_serve(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    config = load_config(data_dir)
    web = config.get("web", {})
    host = args.host or str(web.get("host", "127.0.0.1"))
    port = int(args.port or web.get("port", 8765))
    token = ensure_web_token(data_dir, config)
    data_dir.mkdir(parents=True, exist_ok=True)
    serve_web(data_dir, host, port, access_token=token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex_bark_notify.py")
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup")
    setup.add_argument("--bark-url", required=True)
    setup.set_defaults(func=cmd_setup)

    test = sub.add_parser("test")
    test.add_argument("--dry-run", action="store_true")
    test.set_defaults(func=cmd_test)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=cmd_doctor)

    send = sub.add_parser("send")
    send.add_argument("--event", required=True, choices=["attention", "done", "failure", "permission", "test"])
    send.add_argument("--title", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--level", default="")
    send.add_argument("--dry-run", action="store_true")
    send.set_defaults(func=cmd_send)

    hook = sub.add_parser("hook")
    hook.add_argument("--event", required=True, choices=["PermissionRequest", "Stop", "PostToolUse"])
    hook.set_defaults(func=cmd_hook)

    logs = sub.add_parser("logs")
    logs.add_argument("--tail", type=int, default=20)
    logs.set_defaults(func=cmd_logs)

    actions = sub.add_parser("actions")
    actions.add_argument("--thread-id", default="")
    actions.add_argument("--source-path", default="")
    actions.add_argument("--all", action="store_true")
    actions.add_argument("--format", choices=["json", "jsonl"], default="json")
    actions.add_argument("--mark-dispatched", default="")
    actions.set_defaults(func=cmd_actions)

    heartbeat = sub.add_parser("reply-heartbeat")
    heartbeat.add_argument("--interval", type=int, choices=list(ALLOWED_REPLY_HEARTBEAT_INTERVALS[1:]), default=None)
    heartbeat.add_argument("--off", action="store_true")
    heartbeat.add_argument("--format", choices=["text", "json"], default="text")
    heartbeat.set_defaults(func=cmd_reply_heartbeat)

    sync = sub.add_parser("automation-sync")
    sync.add_argument("--format", choices=["text", "json"], default="text")
    sync.add_argument("--mark-applied", default="")
    sync.add_argument("--automation-id", default="")
    sync.set_defaults(func=cmd_automation_sync)

    remote = sub.add_parser("remote-mode")
    group = remote.add_mutually_exclusive_group()
    group.add_argument("--read-only", action="store_true")
    group.add_argument("--reply", action="store_true")
    remote.add_argument("--format", choices=["text", "json"], default="text")
    remote.set_defaults(func=cmd_remote_mode)

    watch = sub.add_parser("watch")
    watch.add_argument("--sessions-dir", default=str(default_sessions_dir()))
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--baseline", action="store_true")
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=cmd_watch)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
