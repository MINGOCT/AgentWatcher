#!/usr/bin/env python3
"""Bark notifications for Codex hooks and skills."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "notifier": {
        "type": "bark",
        "bark_server": "https://api.day.app",
        "bark_key": "",
        "group": "Codex",
        "default_level": "active",
        "permission_level": "timeSensitive",
    },
    "notification_policy": {
        "mode": "actionable",
        "notify_on_permission_request": True,
        "notify_on_stop": True,
        "notify_on_test_done": True,
        "notify_on_consecutive_failures": True,
        "consecutive_failure_threshold": 3,
        "cooldown_seconds": 45,
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
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
]

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
    return datetime.now(timezone.utc).isoformat()


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
    return config


def save_config(data_dir: Path, config: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path(data_dir).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    patterns = config.get("notification_policy", {}).get(
        "test_command_patterns",
        DEFAULT_CONFIG["notification_policy"]["test_command_patterns"],
    )
    for pattern in patterns:
        if str(pattern).lower() in command:
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


def send_bark(title: str, body: str, level: str, notifier: dict[str, Any]) -> bool:
    key = str(notifier.get("bark_key", "")).strip()
    if not key:
        print("[AgentWatcher] Bark key is not configured.", file=sys.stderr)
        return False
    try:
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "group": str(notifier.get("group", "Codex")),
                "level": level,
            },
            ensure_ascii=True,
        ).encode("ascii")
        req = urllib.request.Request(
            bark_endpoint(notifier),
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
        return bool(policy.get("notify_on_test_done", True)) and is_test_command(payload or {}, config)
    if event_type == "test_done":
        return bool(policy.get("notify_on_test_done", True))
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
    offsets = session_file_offsets(data_dir)
    events, updated_offsets = read_task_complete_events_since_offsets(sessions_dir, offsets)
    seen = seen_task_complete_ids(data_dir)
    processed = 0
    for event in events:
        event_id = task_complete_id(event)
        if event_id in seen:
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
            sent = send_bark(title, body, level, config.get("notifier", {}))
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": "task_complete",
                    "title": title,
                    "body": body,
                    "level": level,
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
            append_event(data_dir, {"timestamp": timestamp_iso(), "event_type": args.event, "sent": False, "reason": "disabled"}, config)
            return 0
        if event_type == "Stop" and watcher_is_running(data_dir):
            append_event(
                data_dir,
                {
                    "timestamp": timestamp_iso(),
                    "event_type": event_type,
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
        process_new_task_completes(data_dir, config, sessions_dir)
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval)))


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

    watch = sub.add_parser("watch")
    watch.add_argument("--sessions-dir", default=str(default_sessions_dir()))
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--baseline", action="store_true")
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
