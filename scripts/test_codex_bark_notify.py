import json
import os
import tempfile
import unittest
import urllib.parse
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from typing import Any

import codex_bark_notify as notify


def run_handler(
    handler,
    method: str,
    path: str,
    payload: bytes = b"",
    client: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    request_headers = {"Content-Length": str(len(payload))}
    if headers:
        request_headers.update(headers)

    class FakeHandler(handler):
        client_address = (client, 12345)
        headers = request_headers
        rfile = BytesIO(payload)

        def __init__(self):
            self.path = path

        def send_response(self, code):
            captured["code"] = code

        def send_header(self, key, value):
            captured.setdefault("headers", {})[key] = value

        def end_headers(self):
            captured["ended"] = True

        @property
        def wfile(self):
            class Writer:
                def write(self, data):
                    captured["body"] = data

            return Writer()

    instance = FakeHandler()
    if method.upper() == "POST":
        instance.do_POST()
    else:
        instance.do_GET()
    return captured


class BarkNotifyTests(unittest.TestCase):
    def test_public_files_do_not_embed_local_user_paths(self):
        plugin_root = Path(__file__).resolve().parents[1]
        public_files = [
            plugin_root / "README_CN.md",
            plugin_root / "hooks" / "hooks.json",
            plugin_root / "scripts" / "resolve_python.ps1",
            plugin_root / "scripts" / "setup_agentwatcher.ps1",
            plugin_root / "scripts" / "set_reply_heartbeat.ps1",
            plugin_root / "scripts" / "test_agentwatcher.ps1",
            plugin_root / "scripts" / "invoke_codex_bark_notify.ps1",
            plugin_root / "scripts" / "start_codex_bark_watcher.ps1",
            plugin_root / "scripts" / "stop_codex_bark_watcher.ps1",
            plugin_root / "scripts" / "install_watcher_startup.ps1",
            plugin_root / "scripts" / "uninstall_watcher_startup.ps1",
            plugin_root / "skills" / "AgentWatcher" / "SKILL.md",
        ]
        forbidden = [
            "C:\\Users\\zhang",
            "D:\\agentwatch",
            "codex-runtimes",
            ".cache\\codex",
        ]

        for path in public_files:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                with self.subTest(path=path, needle=needle):
                    self.assertNotIn(needle, text)

    def test_parse_bark_input_accepts_full_url(self):
        server, key = notify.parse_bark_input("https://api.day.app/abc123/")

        self.assertEqual(server, "https://api.day.app")
        self.assertEqual(key, "abc123")

    def test_parse_bark_input_accepts_plain_key(self):
        server, key = notify.parse_bark_input("abc123")

        self.assertEqual(server, "")
        self.assertEqual(key, "abc123")

    def test_redact_secrets_masks_common_tokens(self):
        openai_token = "sk-" + "abcdefghijklmnopqrstuvwxyz"
        github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz"
        text = "token " + openai_token + " and " + github_token

        redacted = notify.redact_secrets(text)

        self.assertNotIn(openai_token, redacted)
        self.assertNotIn(github_token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_build_message_truncates_body(self):
        config = {
            "privacy": {
                "max_body_chars": 80,
                "include_command_preview": True,
            }
        }
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo " + ("x" * 300)},
        }

        title, body, level = notify.build_message("PermissionRequest", payload, config)

        self.assertEqual(title, "AgentWatcher 待批准")
        self.assertEqual(level, "timeSensitive")
        self.assertLessEqual(len(body), 80)
        self.assertIn("动作：", body)

    def test_setup_writes_config_to_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            code = notify.main([
                "--data-dir", str(data_dir),
                "setup",
                "--bark-url", "https://api.day.app/abc123/",
            ])

            self.assertEqual(code, 0)
            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["notifier"]["bark_server"], "https://api.day.app")
            self.assertEqual(config["notifier"]["bark_key"], "abc123")

    def test_load_config_tolerates_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                "\ufeff" + json.dumps({"notifier": {"bark_key": "abc123"}}),
                encoding="utf-8",
            )

            config = notify.load_config(data_dir)

            self.assertEqual(config["notifier"]["bark_key"], "abc123")

    def test_reply_heartbeat_defaults_to_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = notify.load_config(Path(tmp))

        heartbeat = notify.reply_heartbeat_status(config)

        self.assertFalse(heartbeat["enabled"])
        self.assertEqual(heartbeat["interval_minutes"], 0)
        self.assertEqual(heartbeat["rrule"], "")
        self.assertIn(15, heartbeat["allowed_intervals"])

    def test_reply_heartbeat_command_writes_enabled_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stdout = StringIO()

            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T09:00:00Z")):
                with redirect_stdout(stdout):
                    code = notify.main([
                        "--data-dir", str(data_dir),
                        "reply-heartbeat",
                        "--interval", "15",
                        "--format", "json",
                    ])

            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertTrue(status["enabled"])
            self.assertEqual(status["interval_minutes"], 15)
            self.assertEqual(status["rrule"], "FREQ=MINUTELY;INTERVAL=15")
            self.assertEqual(status["auto_pause_minutes"], 120)
            self.assertEqual(status["expires_at"], "2026-06-11T11:00:00+00:00")
            self.assertFalse(status["expired"])
            self.assertIn("actions --thread-id", status["heartbeat_prompt"])

            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertTrue(config["reply_heartbeat"]["enabled"])
            self.assertEqual(config["reply_heartbeat"]["interval_minutes"], 15)
            self.assertEqual(config["reply_heartbeat"]["started_at"], "2026-06-11T09:00:00+00:00")
            self.assertEqual(config["reply_heartbeat"]["expires_at"], "2026-06-11T11:00:00+00:00")
            self.assertEqual(config["reply_heartbeat"]["automation_apply_status"], "pending")
            self.assertTrue(config["reply_heartbeat"]["pending_request_id"])

    def test_automation_sync_returns_pending_request_and_mark_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stdout = StringIO()

            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir", str(data_dir),
                    "reply-heartbeat",
                    "--interval", "15",
                    "--format", "json",
                ])

            self.assertEqual(code, 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir", str(data_dir),
                    "automation-sync",
                    "--format", "json",
                ])

            self.assertEqual(code, 0)
            request = json.loads(stdout.getvalue())
            self.assertTrue(request["pending"])
            self.assertEqual(request["action"], "upsert")
            self.assertEqual(request["interval_minutes"], 15)
            self.assertEqual(request["rrule"], "FREQ=MINUTELY;INTERVAL=15")
            self.assertIn("heartbeat_prompt", request)
            request_id = request["request_id"]

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir", str(data_dir),
                    "automation-sync",
                    "--mark-applied", request_id,
                    "--automation-id", "auto-123",
                    "--format", "json",
                ])

            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertFalse(status["pending"])
            self.assertEqual(status["automation_apply_status"], "applied")
            self.assertEqual(status["automation_id"], "auto-123")
            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["reply_heartbeat"]["applied_interval_minutes"], 15)
            self.assertEqual(config["reply_heartbeat"]["automation_apply_status"], "applied")
            self.assertEqual(config["reply_heartbeat"].get("pending_request_id"), "")

    def test_reply_heartbeat_status_auto_pauses_after_expiration(self):
        config = {
            "reply_heartbeat": {
                "enabled": True,
                "interval_minutes": 15,
                "auto_pause_minutes": 120,
                "started_at": "2026-06-11T09:00:00Z",
                "expires_at": "2026-06-11T11:00:00Z",
            }
        }

        with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T11:00:01Z")):
            status = notify.reply_heartbeat_status(config)

        self.assertFalse(status["enabled"])
        self.assertEqual(status["interval_minutes"], 0)
        self.assertEqual(status["rrule"], "")
        self.assertTrue(status["expired"])
        self.assertIn("已自动暂停", notify.reply_heartbeat_status_text(config))

    def test_reply_heartbeat_command_auto_disables_expired_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "reply_heartbeat": {
                            "enabled": True,
                            "interval_minutes": 15,
                            "expires_at": "2026-06-11T11:00:00Z",
                        }
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()

            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T11:00:01Z")):
                with redirect_stdout(stdout):
                    code = notify.main([
                        "--data-dir", str(data_dir),
                        "reply-heartbeat",
                        "--format", "json",
                    ])

            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertFalse(status["enabled"])
            self.assertTrue(status["expired"])

            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertFalse(config["reply_heartbeat"]["enabled"])
            self.assertEqual(config["reply_heartbeat"]["interval_minutes"], 0)

    def test_reply_heartbeat_command_can_turn_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"reply_heartbeat": {"enabled": True, "interval_minutes": 5}}),
                encoding="utf-8",
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir", str(data_dir),
                    "reply-heartbeat",
                    "--off",
                    "--format", "json",
                ])

            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertFalse(status["enabled"])
            self.assertEqual(status["interval_minutes"], 0)
            self.assertEqual(status["rrule"], "")

            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertFalse(config["reply_heartbeat"]["enabled"])
            self.assertEqual(config["reply_heartbeat"]["interval_minutes"], 0)

    def test_reply_heartbeat_off_warning_is_shown_on_confirmation_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"reply_heartbeat": {"enabled": False, "interval_minutes": 0}}),
                encoding="utf-8",
            )
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/action/turn-abc/continue", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_GET()

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn('class="warning"', body)
            self.assertIn("重要提醒", body)
            self.assertIn("自动同步当前已关闭", body)
            self.assertIn("不会自动发送到 Codex 执行", body)
            self.assertIn("仅记录，不会自动发送", body)

    def test_reply_heartbeat_off_warning_is_shown_on_reply_confirmation_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"reply_heartbeat": {"enabled": False, "interval_minutes": 0}}),
                encoding="utf-8",
            )
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            payload = "reply=请继续修复失败的测试".encode("utf-8")
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/reply/turn-abc/confirm", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)
                headers = {"Content-Length": str(len(payload))}
                rfile = BytesIO(payload)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_POST()

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn('class="warning"', body)
            self.assertIn("重要提醒", body)
            self.assertIn("自动同步当前已关闭", body)
            self.assertIn("不会自动发送到 Codex 执行", body)
            self.assertIn("仅记录，不会自动发送", body)

    def test_remote_mode_command_writes_read_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stdout = StringIO()

            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir", str(data_dir),
                    "remote-mode",
                    "--read-only",
                    "--format", "json",
                ])

            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertEqual(status["mode"], "read_only")
            self.assertFalse(status["allows_reply"])

            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["remote_interaction"]["mode"], "read_only")

    def test_remote_mode_command_writes_reply_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"remote_interaction": {"mode": "read_only"}}),
                encoding="utf-8",
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir", str(data_dir),
                    "remote-mode",
                    "--reply",
                    "--format", "json",
                ])

            self.assertEqual(code, 0)
            status = json.loads(stdout.getvalue())
            self.assertEqual(status["mode"], "reply")
            self.assertTrue(status["allows_reply"])

            config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["remote_interaction"]["mode"], "reply")

    def test_default_data_dir_prefers_existing_global_config_over_plugin_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            plugin_data = root / "plugin-data"
            global_data = home / ".codex-bark-notify"
            global_data.mkdir(parents=True)
            plugin_data.mkdir()
            (global_data / "config.json").write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"PLUGIN_DATA": str(plugin_data)}, clear=True):
                with patch.object(notify.Path, "home", return_value=home):
                    data_dir = notify.default_data_dir()

            self.assertEqual(data_dir, global_data)

    def test_send_dry_run_logs_event_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "notifier": {
                            "type": "bark",
                            "bark_server": "https://api.day.app",
                            "bark_key": "abc123",
                            "group": "Codex",
                            "default_level": "active",
                            "permission_level": "timeSensitive",
                        },
                        "notification_policy": {"cooldown_seconds": 0},
                        "privacy": {"max_body_chars": 240, "redact_secrets": True},
                    }
                ),
                encoding="utf-8",
            )

            code = notify.main([
                "--data-dir", str(data_dir),
                "send",
                "--event", "done",
                "--title", "Codex 完成",
                "--body", "测试通过",
                "--dry-run",
            ])

            self.assertEqual(code, 0)
            lines = (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["event_type"], "done")
            self.assertFalse(event["sent"])

    def test_send_bark_posts_ascii_safe_json_payload(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def read(self):
                return b'{"code":200}'

        notifier = {
            "bark_server": "https://api.day.app",
            "bark_key": "abc123",
            "group": "Codex",
        }

        with patch.object(notify.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            sent = notify.send_bark("AgentWatcher 完成", "已完成：任务完成", "active", notifier)

        self.assertTrue(sent)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.day.app/abc123")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertNotIn("%E5", request.full_url)
        self.assertTrue(request.data.isascii())
        self.assertIn(b"\\u5b8c\\u6210", request.data)
        self.assertNotIn("完成".encode("utf-8"), request.data)
        payload = json.loads(request.data.decode("ascii"))
        self.assertEqual(payload["title"], "AgentWatcher 完成")
        self.assertEqual(payload["body"], "已完成：任务完成")
        self.assertEqual(payload["group"], "Codex")
        self.assertEqual(payload["level"], "active")

    def test_send_bark_includes_detail_url_when_provided(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def read(self):
                return b'{"code":200}'

        notifier = {
            "bark_server": "https://api.day.app",
            "bark_key": "abc123",
            "group": "Codex",
        }

        with patch.object(notify.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            sent = notify.send_bark("AgentWatcher 完成", "已完成：任务完成", "active", notifier, url="http://127.0.0.1:8765/details/turn.html")

        self.assertTrue(sent)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("ascii"))
        self.assertEqual(payload["url"], "http://127.0.0.1:8765/details/turn.html")

    def test_send_bark_includes_custom_icon_when_configured(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def read(self):
                return b'{"code":200}'

        notifier = {
            "bark_server": "https://api.day.app",
            "bark_key": "abc123",
            "group": "Codex",
            "icon_url": "https://agent.example.com/assets/icon.png?v=123",
        }

        with patch.object(notify.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            sent = notify.send_bark("AgentWatcher 完成", "已完成：任务完成", "active", notifier)

        self.assertTrue(sent)
        request = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(query["icon"], ["https://agent.example.com/assets/icon.png?v=123"])
        payload = json.loads(request.data.decode("ascii"))
        self.assertNotIn("icon", payload)

    def test_send_bark_includes_default_codex_icon_from_loaded_config(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def read(self):
                return b'{"code":200}'

        with tempfile.TemporaryDirectory() as tmp:
            config = notify.load_config(Path(tmp))
            notifier = config["notifier"]
            notifier["bark_key"] = "abc123"

            with patch.object(notify.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
                sent = notify.send_bark("AgentWatcher 完成", "已完成：任务完成", "active", notifier)

        self.assertTrue(sent)
        request = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(query["icon"], [notify.DEFAULT_BARK_ICON_URL])

    def test_load_config_replaces_legacy_blank_icon_url_with_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"notifier": {"icon_url": ""}}),
                encoding="utf-8",
            )

            config = notify.load_config(data_dir)

        self.assertEqual(config["notifier"]["icon_url"], notify.DEFAULT_BARK_ICON_URL)

    def test_write_detail_page_redacts_secrets_and_returns_local_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            event = {
                "turn_id": "turn-detail",
                "source_path": str(Path(tmp) / "rollout-2026-06-08T10-46-06-019ea51f-84c5-7b63-bbfe-790adc5f615c.jsonl"),
                "source_offset": 123,
                "last_agent_message": "已完成部署。\n不要泄露 " + ("ghp_" + "abcdefghijklmnopqrstuvwxyz"),
            }
            config = {
                "web": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 8765,
                    "access_token": "secret123",
                },
                "privacy": {"redact_secrets": True},
            }

            detail = notify.write_detail_page(data_dir, event, "AgentWatcher 完成", "已完成：部署", config)

            self.assertEqual(detail["id"], "turn-detail")
            parsed = urllib.parse.urlparse(detail["url"])
            query = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(parsed.scheme, "http")
            self.assertEqual(parsed.netloc, "127.0.0.1:8765")
            self.assertEqual(parsed.path, "/details/turn-detail.html")
            self.assertEqual(query["detail_id"], ["turn-detail"])
            self.assertIn("expires", query)
            self.assertIn("signature", query)
            self.assertNotIn("token", query)
            html = (data_dir / "details" / "turn-detail.html").read_text(encoding="utf-8")
            self.assertIn("已完成部署", html)
            self.assertIn("[REDACTED]", html)
            self.assertNotIn("ghp_" + "abcdefghijklmnopqrstuvwxyz", html)
            self.assertIn("/action/turn-detail/continue?detail_id=turn-detail", html)
            self.assertIn("/action/turn-detail/stop?detail_id=turn-detail", html)
            self.assertIn("/reply/turn-detail/confirm?detail_id=turn-detail", html)
            self.assertNotIn("token=", html)
            self.assertIn('name="reply"', html)
            contexts = json.loads((data_dir / "action_contexts.json").read_text(encoding="utf-8"))
            self.assertEqual(contexts["turn-detail"]["detail_id"], "turn-detail")
            self.assertEqual(contexts["turn-detail"]["turn_id"], "turn-detail")
            self.assertEqual(contexts["turn-detail"]["thread_id"], "019ea51f-84c5-7b63-bbfe-790adc5f615c")
            self.assertEqual(contexts["turn-detail"]["source_offset"], 123)

    def test_record_reply_includes_detail_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            notify.save_action_context(
                data_dir,
                "turn-123",
                {
                    "detail_id": "turn-123",
                    "turn_id": "turn-123",
                    "thread_id": "thread-a",
                    "source_path": "session-a.jsonl",
                    "source_offset": 456,
                },
            )

            entry = notify.record_reply(data_dir, "turn-123", "请继续修复失败的测试", client="127.0.0.1")

            self.assertEqual(entry["thread_id"], "thread-a")
            self.assertEqual(entry["source_path"], "session-a.jsonl")
            self.assertEqual(entry["source_offset"], 456)

    def test_action_context_includes_60_minute_expiration(self):
        event = {
            "turn_id": "turn-expire",
            "timestamp": "2026-06-11T09:00:00Z",
            "source_path": "rollout-2026-06-08T10-46-06-019ea51f-84c5-7b63-bbfe-790adc5f615c.jsonl",
        }

        context = notify.action_context_from_event("turn-expire", event)

        self.assertEqual(context["created_at"], "2026-06-11T09:00:00+00:00")
        self.assertEqual(context["expires_at"], "2026-06-11T10:00:00+00:00")
        self.assertFalse(notify.context_is_expired(context, now=notify.parse_timestamp("2026-06-11T09:59:59Z")))
        self.assertTrue(notify.context_is_expired(context, now=notify.parse_timestamp("2026-06-11T10:00:01Z")))

    def test_markdown_rendering_escapes_html_and_formats_common_blocks(self):
        rendered = notify.render_markdown_result("# 标题\n\n- 项目\n\n`code`\n\n<script>alert(1)</script>")

        self.assertIn("<h1>标题</h1>", rendered)
        self.assertIn("<li>项目</li>", rendered)
        self.assertIn("<code>code</code>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)

    def test_detail_page_includes_markdown_result_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            event = {
                "turn_id": "turn-markdown",
                "last_agent_message": "# 完成\n\n- 已通过测试",
            }
            config = {"web": {"enabled": True, "host": "127.0.0.1", "port": 8765}, "privacy": {"redact_secrets": True}}

            notify.write_detail_page(data_dir, event, "AgentWatcher 完成", "已完成：测试", config)

            page = (data_dir / "details" / "turn-markdown.html").read_text(encoding="utf-8")
            self.assertIn('class="markdown-result"', page)
            self.assertIn("<h1>完成</h1>", page)
            self.assertIn("<li>已通过测试</li>", page)
            self.assertIn("<pre>", page)

    def test_detail_page_shows_reply_heartbeat_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            event = {
                "turn_id": "turn-heartbeat-status",
                "last_agent_message": "任务完成",
            }
            config = {
                "web": {"enabled": True, "host": "127.0.0.1", "port": 8765},
                "reply_heartbeat": {"enabled": True, "interval_minutes": 15},
                "remote_interaction": {"mode": "reply"},
                "privacy": {"redact_secrets": True},
            }

            notify.write_detail_page(data_dir, event, "AgentWatcher 完成", "已完成：测试", config)

            page = (data_dir / "details" / "turn-heartbeat-status.html").read_text(encoding="utf-8")
            self.assertIn("手机回复自动同步：每 15 分钟检查一次", page)
            self.assertIn("远程交互模式：可回复", page)

    def test_detail_page_emphasizes_reply_heartbeat_off_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            event = {
                "turn_id": "turn-warning",
                "last_agent_message": "任务完成",
            }
            config = {
                "web": {"enabled": True, "host": "127.0.0.1", "port": 8765},
                "reply_heartbeat": {"enabled": False, "interval_minutes": 0},
                "remote_interaction": {"mode": "reply"},
                "privacy": {"redact_secrets": True},
            }

            notify.write_detail_page(data_dir, event, "AgentWatcher 完成", "已完成：测试", config)

            page = (data_dir / "details" / "turn-warning.html").read_text(encoding="utf-8")
            self.assertIn('class="warning"', page)
            self.assertIn("重要提醒", page)
            self.assertIn("不会自动发送到 Codex 执行", page)

    def test_detail_page_read_only_mode_hides_actions_and_reply_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            event = {
                "turn_id": "turn-read-only",
                "last_agent_message": "任务完成",
            }
            config = {
                "web": {"enabled": True, "host": "127.0.0.1", "port": 8765},
                "remote_interaction": {"mode": "read_only"},
                "privacy": {"redact_secrets": True},
            }

            notify.write_detail_page(data_dir, event, "AgentWatcher 完成", "已完成：测试", config)

            page = (data_dir / "details" / "turn-read-only.html").read_text(encoding="utf-8")
            self.assertIn("远程交互模式：只读", page)
            self.assertIn("当前页面只允许查看结果", page)
            self.assertNotIn("/action/turn-read-only/continue", page)
            self.assertNotIn("/reply/turn-read-only/confirm", page)
            self.assertNotIn('name="reply"', page)

    def test_detail_url_uses_lan_ip_when_host_is_all_interfaces(self):
        config = {
            "web": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 8765,
                "access_token": "tok123",
            }
        }

        with patch.object(notify, "detect_lan_ip", return_value="192.168.1.23"):
            url = notify.detail_url(config, "turn-1")

        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.netloc, "192.168.1.23:8765")
        self.assertEqual(parsed.path, "/details/turn-1.html")
        self.assertEqual(query["detail_id"], ["turn-1"])
        self.assertIn("expires", query)
        self.assertIn("signature", query)
        self.assertNotIn("token", query)

    def test_signed_url_rejects_tampered_path(self):
        config = {"web": {"access_token": "secret123"}}
        url = notify.signed_web_path(config, "/action/turn-1/continue", "turn-1")
        tampered = url.replace("/continue?", "/stop?")

        self.assertTrue(notify.verify_signed_web_request(config, "/action/turn-1/continue", urllib.parse.parse_qs(urllib.parse.urlparse(url).query))[0])
        self.assertFalse(notify.verify_signed_web_request(config, "/action/turn-1/stop", urllib.parse.parse_qs(urllib.parse.urlparse(tampered).query))[0])

    def test_signed_url_expires(self):
        config = {"web": {"access_token": "secret123"}}

        with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T10:00:00Z")):
            url = notify.signed_web_path(config, "/details/turn-1.html", "turn-1", expires_at=notify.parse_timestamp("2026-06-11T10:30:00Z"))

        with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T10:30:01Z")):
            ok, reason = notify.verify_signed_web_request(config, "/details/turn-1.html", urllib.parse.parse_qs(urllib.parse.urlparse(url).query))

        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_record_action_writes_safe_queue_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            entry = notify.record_action(data_dir, "turn-123", "continue", client="127.0.0.1")

            self.assertEqual(entry["detail_id"], "turn-123")
            self.assertEqual(entry["action"], "continue")
            lines = (data_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            saved = json.loads(lines[0])
            self.assertEqual(saved["detail_id"], "turn-123")
            self.assertEqual(saved["action"], "continue")
            self.assertEqual(saved["client"], "127.0.0.1")

    def test_record_reply_writes_safe_queue_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            entry = notify.record_reply(data_dir, "turn-123", "请继续修复失败的测试", client="127.0.0.1")

            self.assertTrue(entry["action_id"])
            self.assertEqual(entry["detail_id"], "turn-123")
            self.assertEqual(entry["action"], "reply")
            self.assertEqual(entry["reply"], "请继续修复失败的测试")
            lines = (data_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            saved = json.loads(lines[0])
            self.assertEqual(saved["detail_id"], "turn-123")
            self.assertEqual(saved["action"], "reply")
            self.assertEqual(saved["reply"], "请继续修复失败的测试")
            self.assertEqual(saved["client"], "127.0.0.1")

    def test_pending_actions_filters_by_thread_id_and_ignores_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            actions = [
                {"timestamp": "1", "detail_id": "a", "action_id": "one", "action": "reply", "reply": "same", "thread_id": "thread-a"},
                {"timestamp": "2", "detail_id": "b", "action": "reply", "reply": "other", "thread_id": "thread-b"},
                {"timestamp": "3", "detail_id": "c", "action": "reply", "reply": "done", "thread_id": "thread-a", "dispatched_at": "now"},
            ]
            with (data_dir / "actions.jsonl").open("w", encoding="utf-8") as handle:
                for action in actions:
                    handle.write(json.dumps(action, ensure_ascii=False) + "\n")

            pending = notify.pending_actions(data_dir, thread_id="thread-a")

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reply"], "same")

    def test_mark_action_dispatched_updates_matching_action_id_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            actions = [
                {"timestamp": "1", "detail_id": "a", "action_id": "one", "action": "reply", "reply": "same", "thread_id": "thread-a"},
                {"timestamp": "2", "detail_id": "b", "action_id": "two", "action": "reply", "reply": "other", "thread_id": "thread-a"},
            ]
            with (data_dir / "actions.jsonl").open("w", encoding="utf-8") as handle:
                for action in actions:
                    handle.write(json.dumps(action, ensure_ascii=False) + "\n")

            updated = notify.mark_action_dispatched(data_dir, "one", thread_id="thread-a")

            self.assertTrue(updated)
            entries = [json.loads(line) for line in (data_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(entries[0]["dispatched_at"])
            self.assertEqual(entries[0]["dispatched_thread_id"], "thread-a")
            self.assertNotIn("dispatched_at", entries[1])

    def test_cmd_actions_prints_only_current_thread_pending_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            actions = [
                {"timestamp": "1", "detail_id": "a", "action": "reply", "reply": "same", "thread_id": "thread-a"},
                {"timestamp": "2", "detail_id": "b", "action": "reply", "reply": "other", "thread_id": "thread-b"},
            ]
            with (data_dir / "actions.jsonl").open("w", encoding="utf-8") as handle:
                for action in actions:
                    handle.write(json.dumps(action, ensure_ascii=False) + "\n")

            stdout = StringIO()
            with redirect_stdout(stdout), patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}):
                code = notify.main(["--data-dir", str(data_dir), "actions"])

            self.assertEqual(code, 0)
            text = stdout.getvalue()
            self.assertIn("same", text)
            self.assertNotIn("other", text)

    def test_cmd_actions_marks_dispatched_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with (data_dir / "actions.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": "1",
                            "detail_id": "a",
                            "action_id": "one",
                            "action": "reply",
                            "reply": "same",
                            "thread_id": "thread-a",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = notify.main(["--data-dir", str(data_dir), "actions", "--thread-id", "thread-a", "--mark-dispatched", "one"])

            self.assertEqual(code, 0)
            self.assertIn('"marked": true', stdout.getvalue())
            saved = json.loads((data_dir / "actions.jsonl").read_text(encoding="utf-8"))
            self.assertTrue(saved["dispatched_at"])

    def test_cmd_actions_marks_action_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with (data_dir / "actions.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": "1", "detail_id": "a", "action_id": "one", "action": "reply", "reply": "same", "thread_id": "thread-a"}, ensure_ascii=False) + "\n")

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = notify.main([
                    "--data-dir",
                    str(data_dir),
                    "actions",
                    "--mark-dispatched",
                    "one",
                    "--thread-id",
                    "thread-a",
                ])

            self.assertEqual(code, 0)
            self.assertIn('"marked": true', stdout.getvalue())
            entry = json.loads((data_dir / "actions.jsonl").read_text(encoding="utf-8"))
            self.assertTrue(entry["dispatched_at"])

    def test_hook_permission_request_sends_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "notifier": {
                            "type": "bark",
                            "bark_server": "https://api.day.app",
                            "bark_key": "abc123",
                            "group": "Codex",
                            "default_level": "active",
                            "permission_level": "timeSensitive",
                        },
                        "notification_policy": {
                            "mode": "actionable",
                            "notify_on_permission_request": True,
                            "notify_on_stop": True,
                            "cooldown_seconds": 0,
                        },
                        "privacy": {
                            "max_body_chars": 240,
                            "redact_secrets": True,
                            "include_command_preview": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "npm test"}})

            with patch.object(notify.sys, "stdin") as fake_stdin, patch.object(notify, "send_bark") as send:
                fake_stdin.read.return_value = payload
                send.return_value = True
                code = notify.main(["--data-dir", str(data_dir), "hook", "--event", "PermissionRequest"])

            self.assertEqual(code, 0)
            send.assert_called_once()
            title = send.call_args.args[0]
            self.assertEqual(title, "AgentWatcher 待批准")

    def test_stop_hook_outputs_json_and_sends_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "notifier": {
                            "type": "bark",
                            "bark_server": "https://api.day.app",
                            "bark_key": "abc123",
                            "group": "Codex",
                            "default_level": "active",
                            "permission_level": "timeSensitive",
                        },
                        "notification_policy": {
                            "mode": "actionable",
                            "notify_on_stop": True,
                            "cooldown_seconds": 0,
                        },
                        "privacy": {"max_body_chars": 240, "redact_secrets": True},
                    }
                ),
                encoding="utf-8",
            )
            payload = json.dumps({"last_assistant_message": "任务完成"})

            with patch.object(notify.sys, "stdin") as fake_stdin, patch.object(notify, "send_bark") as send:
                fake_stdin.read.return_value = payload
                send.return_value = True
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = notify.main(["--data-dir", str(data_dir), "hook", "--event", "Stop"])

            self.assertEqual(code, 0)
            send.assert_called_once()
            output = json.loads(stdout.getvalue())
            self.assertTrue(output["continue"])

    def test_hook_event_log_records_hook_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "notifier": {
                            "type": "bark",
                            "bark_server": "https://api.day.app",
                            "bark_key": "abc123",
                            "group": "Codex",
                            "default_level": "active",
                            "permission_level": "timeSensitive",
                        },
                        "notification_policy": {
                            "mode": "actionable",
                            "notify_on_stop": True,
                            "cooldown_seconds": 0,
                        },
                        "privacy": {"max_body_chars": 240, "redact_secrets": True},
                    }
                ),
                encoding="utf-8",
            )
            payload = json.dumps({"turn_id": "turn-hook", "last_assistant_message": "done"})

            with patch.object(notify.sys, "stdin") as fake_stdin, patch.object(notify, "send_bark") as send:
                fake_stdin.read.return_value = payload
                send.return_value = True
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = notify.main(["--data-dir", str(data_dir), "hook", "--event", "Stop"])

            self.assertEqual(code, 0)
            events = [json.loads(line) for line in (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "Stop")
            self.assertEqual(events[-1]["producer"], "hook")
            self.assertIn("script_sha256", events[-1])

    def test_read_stdin_json_tolerates_utf8_bom(self):
        with patch.object(notify.sys, "stdin") as fake_stdin:
            fake_stdin.read.return_value = "\ufeff{\"tool_name\":\"Bash\"}"

            payload = notify.read_stdin_json()

        self.assertEqual(payload["tool_name"], "Bash")

    def test_read_stdin_json_recovers_gbk_mojibake(self):
        mojibake = b"\xef\xbb\xbf{\"tool_name\":\"Bash\"}".decode("gbk")
        with patch.object(notify.sys, "stdin") as fake_stdin:
            fake_stdin.read.return_value = mojibake

            payload = notify.read_stdin_json()

        self.assertEqual(payload["tool_name"], "Bash")

    def test_posttooluse_is_silent_by_default(self):
        config = {
            "notification_policy": {
                "mode": "actionable",
                "notify_on_permission_request": True,
                "notify_on_stop": True,
            }
        }

        self.assertFalse(notify.should_notify("PostToolUse", config))

    def test_posttooluse_test_command_stays_silent_by_default(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "npm test"}}

        self.assertFalse(notify.should_notify("PostToolUse", notify.DEFAULT_CONFIG, payload))

    def test_posttooluse_test_command_notifies_when_enabled(self):
        config = {
            "notification_policy": {
                "mode": "actionable",
                "notify_on_test_done": True,
            }
        }
        payload = {"tool_name": "Bash", "tool_input": {"command": "npm test"}}

        self.assertTrue(notify.should_notify("PostToolUse", config, payload))

    def test_cleanup_command_with_pytest_named_files_is_not_test_command(self):
        config = {
            "notification_policy": {
                "mode": "actionable",
                "notify_on_test_done": True,
            }
        }
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "if (Test-Path .pytest_api.sqlite) { Remove-Item .pytest_api.sqlite }; 'cleanup-ok'",
            },
        }

        self.assertFalse(notify.is_test_command(payload, config))

    def test_posttooluse_non_test_command_stays_silent(self):
        config = {
            "notification_policy": {
                "mode": "actionable",
                "notify_on_test_done": True,
            }
        }
        payload = {"tool_name": "Bash", "tool_input": {"command": "dir"}}

        self.assertFalse(notify.should_notify("PostToolUse", config, payload))

    def test_build_message_for_test_done(self):
        config = {"privacy": {"max_body_chars": 120, "redact_secrets": True}}
        payload = {"tool_name": "Bash", "tool_input": {"command": "npm test"}}

        title, body, level = notify.build_message("test_done", payload, config)

        self.assertEqual(title, "AgentWatcher 测试完成")
        self.assertEqual(level, "active")
        self.assertIn("npm test", body)
        self.assertIn("动作：查看结果", body)

    def test_stop_message_uses_compact_summary_instead_of_full_last_message(self):
        config = {"privacy": {"max_body_chars": 96, "redact_secrets": True, "summary_style": "compact"}}
        payload = {
            "task_title": "修改 AgentWatcher 通知摘要",
            "result": "测试通过",
            "action": "回来验收",
            "last_assistant_message": "这是一段很长很长的最后回复，里面包含大量实现细节、路径、测试日志和用户在手机通知里根本看不完的内容。",
        }

        title, body, level = notify.build_message("Stop", payload, config)

        self.assertEqual(title, "AgentWatcher 完成")
        self.assertEqual(level, "active")
        self.assertLessEqual(len(body), 96)
        self.assertIn("已完成：修改 AgentWatcher 通知摘要", body)
        self.assertIn("结果：测试通过", body)
        self.assertIn("动作：回来验收", body)
        self.assertNotIn("大量实现细节", body)

    def test_stop_message_ignores_machine_json_summary(self):
        config = {"privacy": {"max_body_chars": 96, "redact_secrets": True, "summary_style": "compact"}}
        payload = {"last_assistant_message": "{\"suggestions\":[]}"}

        title, body, level = notify.build_message("Stop", payload, config)

        self.assertEqual(title, "AgentWatcher 完成")
        self.assertEqual(level, "active")
        self.assertIn("已完成：本轮任务", body)
        self.assertNotIn("suggestions", body)

    def test_compact_summary_falls_back_to_first_sentence(self):
        text = "第一句应该保留。第二句不应该进入 Apple Watch 摘要。第三句也不需要。"

        summary = notify.compact_text_summary(text, 20)

        self.assertEqual(summary, "第一句应该保留")

    def test_compact_summary_recovers_mojibake_text(self):
        mojibake = "已完成：任务完成".encode("utf-8").decode("gbk")

        summary = notify.compact_text_summary(mojibake, 20)

        self.assertEqual(summary, "已完成：任务完成")

    def test_plugin_manifest_uses_agentwatcher_name(self):
        plugin_root = Path(__file__).resolve().parents[1]
        manifest = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "AgentWatcher")
        self.assertEqual(manifest["interface"]["displayName"], "AgentWatcher")

    def test_iter_task_complete_events_reads_codex_session_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}})
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-06-08T04:40:22.573Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-123",
                            "last_agent_message": "任务完成",
                            "completed_at": 1780893622,
                            "duration_ms": 11899,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            events = list(notify.iter_task_complete_events(sessions_dir))

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["turn_id"], "turn-123")
            self.assertEqual(events[0]["last_agent_message"], "任务完成")
            self.assertEqual(events[0]["source_path"], str(session_file))

    def test_process_new_task_completes_sends_once_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-456",
                            "last_agent_message": "测试完成",
                            "completed_at": 1780893622,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {"notify_on_stop": True, "cooldown_seconds": 0},
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with patch.object(notify, "send_bark") as send:
                with patch.object(notify, "utc_now", return_value=datetime.fromtimestamp(1780893622, tz=timezone.utc)):
                    send.return_value = True
                    processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)
            send.assert_called_once()
            title, body, level, _notifier = send.call_args.args
            self.assertEqual(title, "AgentWatcher 完成")
            self.assertIn("已完成：", body)
            self.assertEqual(level, "active")

            with patch.object(notify, "send_bark") as send:
                processed_again = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed_again, 0)
            send.assert_not_called()

    def test_process_new_task_completes_creates_detail_and_sends_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-detail-url",
                            "last_agent_message": "完整结果可以在手机详情页看到。",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "web": {"enabled": True, "host": "127.0.0.1", "port": 8765},
                "notification_policy": {"notify_on_stop": True, "cooldown_seconds": 0},
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with patch.object(notify, "send_bark") as send:
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)
            send.assert_called_once()
            sent_url = send.call_args.kwargs["url"]
            parsed = urllib.parse.urlparse(sent_url)
            query = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(parsed.path, "/details/turn-detail-url.html")
            self.assertEqual(query["detail_id"], ["turn-detail-url"])
            self.assertIn("expires", query)
            self.assertIn("signature", query)
            self.assertNotIn("token", query)
            html = (data_dir / "details" / "turn-detail-url.html").read_text(encoding="utf-8")
            self.assertIn("完整结果可以在手机详情页看到", html)

    def test_agentwatcher_http_handler_shows_action_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/action/turn-abc/continue", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_GET()

            self.assertEqual(captured["code"], 200)
            body = captured["body"].decode("utf-8")
            self.assertIn("确认操作", body)
            self.assertIn("/action/turn-abc/continue?detail_id=turn-abc", body)
            self.assertIn("signature=", body)
            self.assertNotIn("token=", body)
            self.assertFalse((data_dir / "actions.jsonl").exists())

    def test_console_get_shows_status_and_recent_events_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = notify.load_config(data_dir)
            config["notifier"]["bark_key"] = "secret-bark-key"
            config["web"]["access_token"] = "secret-web-token"
            config["web"]["public_base_url"] = "https://agent.example.com"
            config["notification_policy"]["notify_on_test_done"] = False
            notify.save_config(data_dir, config)
            notify.append_event(
                data_dir,
                {
                    "timestamp": "2026-06-12T03:00:00Z",
                    "event_type": "task_complete",
                    "title": "AgentWatcher 完成",
                    "sent": True,
                    "detail": {"url": "https://agent.example.com/details/a.html?signature=secret"},
                    "raw_event": {"last_agent_message": "不要显示完整正文"},
                },
                config,
            )
            handler = notify.make_http_handler(data_dir, access_token="secret-web-token")

            captured = run_handler(handler, "GET", "/console")

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn("AgentWatcher Console", body)
            self.assertIn("Bark 已配置", body)
            self.assertIn("测试命令通知", body)
            self.assertIn("默认关闭", body)
            self.assertIn("task_complete", body)
            self.assertNotIn("secret-bark-key", body)
            self.assertNotIn("secret-web-token", body)
            self.assertNotIn("signature=secret", body)
            self.assertNotIn("不要显示完整正文", body)

    def test_console_post_updates_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = notify.load_config(data_dir)
            config["web"]["access_token"] = "secret-web-token"
            notify.save_config(data_dir, config)
            handler = notify.make_http_handler(data_dir, access_token="secret-web-token")
            payload = (
                "remote_mode=read_only&notify_on_test_done=on&reply_interval=15"
            ).encode("utf-8")

            captured = run_handler(handler, "POST", "/console", payload=payload)

            self.assertEqual(captured["code"], 200)
            body = captured["body"].decode("utf-8")
            self.assertIn("设置已保存", body)
            updated = notify.load_config(data_dir)
            self.assertEqual(updated["remote_interaction"]["mode"], "read_only")
            self.assertTrue(updated["notification_policy"]["notify_on_test_done"])
            self.assertTrue(updated["reply_heartbeat"]["enabled"])
            self.assertEqual(updated["reply_heartbeat"]["interval_minutes"], 15)
            self.assertEqual(updated["reply_heartbeat"]["automation_apply_status"], "pending")
            self.assertTrue(updated["reply_heartbeat"]["pending_request_id"])
            self.assertIn("待应用到 Codex 自动任务", body)

    def test_console_uploads_custom_icon_and_serves_versioned_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = notify.load_config(data_dir)
            config["web"]["public_base_url"] = "https://agent.example.com"
            notify.save_config(data_dir, config)
            handler = notify.make_http_handler(data_dir)
            boundary = "----AgentWatcherBoundary"
            image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
            payload = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="icon_file"; filename="avatar.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode("utf-8") + image + f"\r\n--{boundary}--\r\n".encode("utf-8")

            captured = run_handler(
                handler,
                "POST",
                "/console/icon",
                payload=payload,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

            self.assertEqual(captured["code"], 200)
            updated = notify.load_config(data_dir)
            icon = updated["notifier"]["icon"]
            self.assertTrue(icon["enabled"])
            self.assertEqual(icon["filename"], "icon.png")
            self.assertTrue(icon["version"])
            self.assertEqual(updated["notifier"]["icon_url"], f"https://agent.example.com/assets/icon.png?v={icon['version']}")
            self.assertEqual((data_dir / "assets" / "icon.png").read_bytes(), image)

            asset = run_handler(handler, "GET", f"/assets/icon.png?v={icon['version']}")

            self.assertEqual(asset["code"], 200)
            self.assertEqual(asset["headers"]["Content-Type"], "image/png")
            self.assertEqual(asset["body"], image)

    def test_console_rejects_unsupported_custom_icon_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir)
            boundary = "----AgentWatcherBoundary"
            payload = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="icon_file"; filename="avatar.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
                "hello\r\n"
                f"--{boundary}--\r\n"
            ).encode("utf-8")

            captured = run_handler(
                handler,
                "POST",
                "/console/icon",
                payload=payload,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

            self.assertEqual(captured["code"], 400)
            self.assertFalse((data_dir / "assets").exists())

    def test_console_shows_applied_heartbeat_only_after_mark_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = notify.load_config(data_dir)
            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-12T03:00:00Z")):
                notify.set_reply_heartbeat(config, 15)
            request_id = config["reply_heartbeat"]["pending_request_id"]
            notify.mark_reply_heartbeat_application_applied(config, request_id, automation_id="auto-123")
            notify.save_config(data_dir, config)
            handler = notify.make_http_handler(data_dir)

            captured = run_handler(handler, "GET", "/console")

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn("已应用到 Codex 自动任务", body)
            self.assertIn("auto-123", body)

    def test_agentwatcher_http_handler_action_get_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/action/turn-abc/continue", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_GET()

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn("确认操作", body)
            self.assertIn('method="post"', body)
            self.assertIn('name="confirm"', body)
            self.assertFalse((data_dir / "actions.jsonl").exists())

    def test_agentwatcher_http_handler_records_action_after_post_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            payload = b"confirm=yes"
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/action/turn-abc/continue", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)
                headers = {"Content-Length": str(len(payload))}
                rfile = BytesIO(payload)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_POST()

            self.assertEqual(captured["code"], 200)
            self.assertIn("已记录", captured["body"].decode("utf-8"))
            action = json.loads((data_dir / "actions.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(action["detail_id"], "turn-abc")
            self.assertEqual(action["action"], "continue")

    def test_agentwatcher_http_handler_rejects_action_in_read_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"remote_interaction": {"mode": "read_only"}}),
                encoding="utf-8",
            )
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            payload = b"confirm=yes"
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/action/turn-abc/continue", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)
                headers = {"Content-Length": str(len(payload))}
                rfile = BytesIO(payload)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_POST()

            self.assertEqual(captured["code"], 403)
            self.assertIn("只读", captured["body"].decode("utf-8"))
            self.assertFalse((data_dir / "actions.jsonl").exists())

    def test_agentwatcher_http_handler_reply_post_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            payload = "reply=请继续修复失败的测试".encode("utf-8")
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/reply/turn-abc/confirm", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)
                headers = {"Content-Length": str(len(payload))}
                rfile = BytesIO(payload)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_POST()

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn("确认发送回复", body)
            self.assertIn("请继续修复失败的测试", body)
            self.assertIn('name="reply"', body)
            self.assertIn('name="confirm"', body)
            self.assertFalse((data_dir / "actions.jsonl").exists())

    def test_agentwatcher_http_handler_records_reply_after_post_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            payload = "confirm=yes&reply=请继续修复失败的测试".encode("utf-8")
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/reply/turn-abc/submit", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)
                headers = {"Content-Length": str(len(payload))}
                rfile = BytesIO(payload)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_POST()

            self.assertEqual(captured["code"], 200)
            self.assertIn("已记录", captured["body"].decode("utf-8"))
            action = json.loads((data_dir / "actions.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(action["detail_id"], "turn-abc")
            self.assertEqual(action["action"], "reply")
            self.assertEqual(action["reply"], "请继续修复失败的测试")

    def test_agentwatcher_http_handler_rejects_reply_in_read_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "config.json").write_text(
                json.dumps({"remote_interaction": {"mode": "read_only"}}),
                encoding="utf-8",
            )
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            captured: dict[str, Any] = {}
            payload = "reply=请继续修复失败的测试".encode("utf-8")
            signed_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/reply/turn-abc/confirm", "turn-abc")

            class FakeHandler(handler):
                path = signed_path
                client_address = ("127.0.0.1", 12345)
                headers = {"Content-Length": str(len(payload))}
                rfile = BytesIO(payload)

                def __init__(self):
                    pass

                def send_response(self, code):
                    captured["code"] = code

                def send_header(self, key, value):
                    captured.setdefault("headers", {})[key] = value

                def end_headers(self):
                    captured["ended"] = True

                @property
                def wfile(self):
                    class Writer:
                        def write(self, data):
                            captured["body"] = data

                    return Writer()

            FakeHandler().do_POST()

            self.assertEqual(captured["code"], 403)
            self.assertIn("只读", captured["body"].decode("utf-8"))
            self.assertFalse((data_dir / "actions.jsonl").exists())

    def test_agentwatcher_http_handler_rejects_bad_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            good_path = notify.signed_web_path({"web": {"access_token": "tok123"}}, "/action/turn-abc/continue", "turn-abc")
            bad_path = good_path.replace("signature=", "signature=bad")
            captured = run_handler(handler, "GET", bad_path)

            self.assertEqual(captured["code"], 403)
            self.assertFalse((data_dir / "actions.jsonl").exists())

            audit = json.loads((data_dir / "web_audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(audit["ip"], "127.0.0.1")
            self.assertEqual(audit["detail_id"], "turn-abc")
            self.assertEqual(audit["action"], "continue")
            self.assertEqual(audit["result"], "forbidden")
            self.assertNotIn("token", json.dumps(audit, ensure_ascii=False))
            self.assertNotIn("signature", json.dumps(audit, ensure_ascii=False))

    def test_agentwatcher_http_handler_serves_signed_detail_and_audits_without_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            detail_dir = data_dir / "details"
            detail_dir.mkdir()
            (detail_dir / "turn-abc.html").write_text("<!doctype html><p>signed</p>", encoding="utf-8")
            notify.save_action_context(
                data_dir,
                "turn-abc",
                {
                    "detail_id": "turn-abc",
                    "expires_at": "2026-06-11T10:30:00+00:00",
                },
            )
            config = {"web": {"access_token": "tok123"}}
            path = notify.signed_web_path(config, "/details/turn-abc.html", "turn-abc", expires_at=notify.parse_timestamp("2026-06-11T10:30:00Z"))
            handler = notify.make_http_handler(data_dir, access_token="tok123")

            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T10:00:00Z")):
                captured = run_handler(handler, "GET", path, client="203.0.113.7")

            self.assertEqual(captured["code"], 200)
            self.assertIn("signed", captured["body"].decode("utf-8"))
            audit = json.loads((data_dir / "web_audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(audit["ip"], "203.0.113.7")
            self.assertEqual(audit["detail_id"], "turn-abc")
            self.assertEqual(audit["action"], "view")
            self.assertEqual(audit["result"], "ok")
            self.assertNotIn("tok123", json.dumps(audit, ensure_ascii=False))
            self.assertNotIn("signature", json.dumps(audit, ensure_ascii=False))

    def test_agentwatcher_http_handler_uses_signed_form_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            notify.save_action_context(
                data_dir,
                "turn-abc",
                {
                    "detail_id": "turn-abc",
                    "expires_at": "2026-06-11T10:30:00+00:00",
                },
            )
            config = {"web": {"access_token": "tok123"}}
            path = notify.signed_web_path(config, "/action/turn-abc/continue", "turn-abc", expires_at=notify.parse_timestamp("2026-06-11T10:30:00Z"))
            handler = notify.make_http_handler(data_dir, access_token="tok123")

            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T10:00:00Z")):
                captured = run_handler(handler, "GET", path)

            body = captured["body"].decode("utf-8")
            self.assertEqual(captured["code"], 200)
            self.assertIn("/action/turn-abc/continue?detail_id=turn-abc", body)
            self.assertIn("signature=", body)
            self.assertNotIn("token=", body)

    def test_agentwatcher_http_handler_rejects_expired_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            detail_dir = data_dir / "details"
            detail_dir.mkdir()
            (detail_dir / "turn-expired.html").write_text("<!doctype html><p>old</p>", encoding="utf-8")
            notify.save_action_context(
                data_dir,
                "turn-expired",
                {
                    "detail_id": "turn-expired",
                    "thread_id": "thread-a",
                    "expires_at": "2026-06-11T10:00:00+00:00",
                },
            )
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            path = notify.signed_web_path(
                {"web": {"access_token": "tok123"}},
                "/details/turn-expired.html",
                "turn-expired",
                expires_at=notify.parse_timestamp("2026-06-11T10:00:00Z"),
            )

            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T10:00:01Z")):
                captured = run_handler(handler, "GET", path)

            self.assertEqual(captured["code"], 410)
            self.assertIn("已过期", captured["body"].decode("utf-8"))

    def test_agentwatcher_http_handler_expires_legacy_detail_without_context_expiration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            detail_dir = data_dir / "details"
            detail_dir.mkdir()
            detail_path = detail_dir / "legacy-detail.html"
            detail_path.write_text("<!doctype html><p>legacy</p>", encoding="utf-8")
            old_time = notify.parse_timestamp("2026-06-11T09:00:00Z").timestamp()
            os.utime(detail_path, (old_time, old_time))
            handler = notify.make_http_handler(data_dir, access_token="tok123")
            path = notify.signed_web_path(
                {"web": {"access_token": "tok123"}},
                "/details/legacy-detail.html",
                "legacy-detail",
                expires_at=notify.parse_timestamp("2026-06-11T11:00:00Z"),
            )

            with patch.object(notify, "utc_now", return_value=notify.parse_timestamp("2026-06-11T10:00:01Z")):
                captured = run_handler(handler, "GET", path)

            self.assertEqual(captured["code"], 410)
            self.assertIn("已过期", captured["body"].decode("utf-8"))

    def test_stop_hook_and_watcher_share_completion_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-shared",
                            "last_agent_message": "任务完成",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "mode": "actionable",
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }
            (data_dir / "config.json").parent.mkdir(parents=True)
            (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            payload = json.dumps({"turn_id": "turn-shared", "last_assistant_message": "任务完成"})

            with patch.object(notify.sys, "stdin") as fake_stdin, patch.object(notify, "send_bark") as send:
                fake_stdin.read.return_value = payload
                send.return_value = True
                stdout = StringIO()
                with redirect_stdout(stdout):
                    notify.main(["--data-dir", str(data_dir), "hook", "--event", "Stop"])

            with patch.object(notify, "send_bark") as send:
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 0)
            send.assert_not_called()

    def test_stop_hook_defers_to_running_watcher_without_marking_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-deferred",
                            "last_agent_message": "任务完成",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "mode": "actionable",
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }
            (data_dir / "config.json").parent.mkdir(parents=True)
            (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            payload = json.dumps({"turn_id": "turn-deferred", "last_assistant_message": "任务完成"})

            with (
                patch.object(notify.sys, "stdin") as fake_stdin,
                patch.object(notify, "send_bark") as send,
                patch.object(notify, "watcher_is_running", return_value=True, create=True),
            ):
                fake_stdin.read.return_value = payload
                send.return_value = True
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = notify.main(["--data-dir", str(data_dir), "hook", "--event", "Stop"])

            self.assertEqual(code, 0)
            send.assert_not_called()
            self.assertNotIn("turn-deferred", notify.seen_task_complete_ids(data_dir))
            event = json.loads((data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(event["event_type"], "Stop")
            self.assertFalse(event["sent"])
            self.assertEqual(event["reason"], "deferred_to_watcher")

            with patch.object(notify, "send_bark") as send:
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)
            send.assert_called_once()

    def test_watcher_is_running_uses_windows_process_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "watcher.pid").write_text("43210\n", encoding="ascii")

            with (
                patch.object(notify.os, "name", "nt"),
                patch.object(notify, "windows_process_exists", return_value=True, create=True) as exists,
            ):
                running = notify.watcher_is_running(data_dir)

            self.assertTrue(running)
            exists.assert_called_once_with(43210)

    def test_stop_hook_timeout_still_dedupes_watcher_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-11T11:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-timeout",
                            "last_agent_message": "任务完成",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "mode": "actionable",
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }
            (data_dir / "config.json").parent.mkdir(parents=True)
            (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            payload = json.dumps({"turn_id": "turn-timeout", "last_assistant_message": "任务完成"})

            with patch.object(notify.sys, "stdin") as fake_stdin, patch.object(notify, "send_bark") as send:
                fake_stdin.read.return_value = payload
                send.return_value = False
                stdout = StringIO()
                with redirect_stdout(stdout):
                    notify.main(["--data-dir", str(data_dir), "hook", "--event", "Stop"])

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, 5, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 0)
            send.assert_not_called()

    def test_watcher_and_stop_hook_share_completion_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-11T11:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-shared",
                            "last_agent_message": "任务完成",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "mode": "actionable",
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }
            (data_dir / "config.json").parent.mkdir(parents=True)
            (data_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, 5, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)

            payload = json.dumps({"turn_id": "turn-shared", "last_assistant_message": "任务完成"})
            with patch.object(notify.sys, "stdin") as fake_stdin, patch.object(notify, "send_bark") as send:
                fake_stdin.read.return_value = payload
                send.return_value = True
                stdout = StringIO()
                with redirect_stdout(stdout):
                    notify.main(["--data-dir", str(data_dir), "hook", "--event", "Stop"])

            send.assert_not_called()

    def test_mark_existing_task_completes_seen_baselines_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "08" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-existing",
                            "last_agent_message": "历史任务",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {"default_level": "active"},
                "notification_policy": {"notify_on_stop": True},
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            marked = notify.mark_existing_task_completes_seen(data_dir, sessions_dir)
            with patch.object(notify, "send_bark") as send:
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(marked, 1)
            self.assertEqual(processed, 0)
            send.assert_not_called()

    def test_mark_existing_task_completes_records_file_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "08" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-existing",
                            "last_agent_message": "历史任务",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            marked = notify.mark_existing_task_completes_seen(data_dir, sessions_dir)

            self.assertEqual(marked, 1)
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            offsets = state["session_file_offsets"]
            self.assertEqual(offsets[str(session_file)], session_file.stat().st_size)

    def test_process_new_task_completes_reads_only_appended_session_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}) + "\n",
                encoding="utf-8",
            )
            notify.mark_existing_task_completes_seen(data_dir, sessions_dir)
            with session_file.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": "2026-06-11T11:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "turn_id": "turn-appended",
                                "last_agent_message": "追加任务完成",
                            },
                        }
                    )
                    + "\n"
                )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                    "max_task_complete_age_seconds": 1800,
                    "max_session_file_age_seconds": 86400,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, 5, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)
            send.assert_called_once()
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["session_file_offsets"][str(session_file)], session_file.stat().st_size)

    def test_process_new_task_completes_skips_stale_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-11T10:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-stale",
                            "last_agent_message": "old completed task",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {"default_level": "active"},
                "notification_policy": {
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                    "max_task_complete_age_seconds": 1800,
                    "max_session_file_age_seconds": 86400,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 0)
            send.assert_not_called()
            events = [json.loads(line) for line in (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "task_complete")
            self.assertEqual(events[-1]["reason"], "skipped_stale_task_complete")
            self.assertEqual(events[-1]["producer"], "watcher")
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            self.assertIn("turn-stale", state["seen_task_complete_ids"])
            self.assertEqual(state["session_file_offsets"][str(session_file)], session_file.stat().st_size)

    def test_process_new_task_completes_skips_old_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "05" / "31" / "rollout-old-session.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-11T11:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-old-session",
                            "last_agent_message": "new event appended to old session",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {"default_level": "active"},
                "notification_policy": {
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                    "max_task_complete_age_seconds": 1800,
                    "max_session_file_age_seconds": 604800,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, 5, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 0)
            send.assert_not_called()
            events = [json.loads(line) for line in (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "task_complete")
            self.assertEqual(events[-1]["reason"], "skipped_old_session_file")
            self.assertEqual(events[-1]["producer"], "watcher")
            state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
            self.assertIn("turn-old-session", state["seen_task_complete_ids"])
            self.assertEqual(state["session_file_offsets"][str(session_file)], session_file.stat().st_size)

    def test_process_new_task_completes_allows_previous_day_active_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-previous-day-active.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-12T02:34:55Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-previous-day-active",
                            "last_agent_message": "previous day active session completed",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                    "max_task_complete_age_seconds": 1800,
                    "max_session_file_age_seconds": 604800,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 12, 2, 35, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)
            send.assert_called_once()
            events = [json.loads(line) for line in (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "task_complete")
            self.assertNotIn("reason", events[-1])

    def test_default_policy_allows_previous_day_active_session_files(self):
        event = {
            "source_path": r"C:\Users\tester\.codex\sessions\2026\06\11\rollout-active.jsonl",
            "timestamp": "2026-06-12T02:34:55Z",
        }

        blocked = notify.old_session_file(
            event,
            notify.DEFAULT_CONFIG,
            now=datetime(2026, 6, 12, 2, 35, tzinfo=timezone.utc),
        )

        self.assertFalse(blocked)

    def test_process_new_task_completes_records_watcher_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "06" / "11" / "rollout-test.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-11T10:59:45Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-fresh",
                            "last_agent_message": "fresh completed task",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "notifier": {
                    "type": "bark",
                    "bark_server": "https://api.day.app",
                    "bark_key": "abc123",
                    "group": "Codex",
                    "default_level": "active",
                    "permission_level": "timeSensitive",
                },
                "notification_policy": {
                    "notify_on_stop": True,
                    "cooldown_seconds": 0,
                    "max_task_complete_age_seconds": 1800,
                    "max_session_file_age_seconds": 86400,
                },
                "privacy": {"max_body_chars": 240, "redact_secrets": True},
            }

            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, tzinfo=timezone.utc)),
                patch.object(notify, "send_bark") as send,
            ):
                send.return_value = True
                processed = notify.process_new_task_completes(data_dir, config, sessions_dir)

            self.assertEqual(processed, 1)
            send.assert_called_once()
            events = [json.loads(line) for line in (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "task_complete")
            self.assertEqual(events[-1]["producer"], "watcher")
            self.assertIn("script_sha256", events[-1])

    def test_watch_reloads_config_before_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            sessions_dir = root / "sessions"
            session_file = sessions_dir / "2026" / "05" / "31" / "rollout-old-session.jsonl"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-11T11:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-reload-config",
                            "last_agent_message": "new event in old session",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = notify.load_config(data_dir)
            config["notifier"]["bark_key"] = "abc123"
            config["notification_policy"]["max_task_complete_age_seconds"] = 1800
            config["notification_policy"]["max_session_file_age_seconds"] = 0
            notify.save_config(data_dir, config)
            args = argparse.Namespace(
                data_dir=str(data_dir),
                sessions_dir=str(sessions_dir),
                baseline=False,
                once=True,
                interval=1,
            )

            def update_config_after_initial_load(path):
                loaded = original_load_config(path)
                updated = json.loads(json.dumps(loaded))
                updated["notification_policy"]["max_session_file_age_seconds"] = 86400
                notify.save_config(path, updated)
                return loaded

            original_load_config = notify.load_config
            with (
                patch.object(notify, "utc_now", return_value=datetime(2026, 6, 11, 11, 0, 5, tzinfo=timezone.utc)),
                patch.object(notify, "load_config", side_effect=update_config_after_initial_load),
                patch.object(notify, "send_bark") as send,
            ):
                send.return_value = True
                notify.cmd_watch(args)

            send.assert_not_called()
            events = [json.loads(line) for line in (data_dir / "codex_bark_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["reason"], "skipped_old_session_file")

    def test_script_fingerprint_uses_process_start_snapshot(self):
        original = notify.RUNNING_SCRIPT_SHA256
        try:
            notify.RUNNING_SCRIPT_SHA256 = "process-start-sha"
            with patch.object(Path, "read_bytes", return_value=b"changed-on-disk"):
                self.assertEqual(notify.script_fingerprint(), "process-start-sha")
        finally:
            notify.RUNNING_SCRIPT_SHA256 = original

    def test_save_seen_task_complete_ids_keeps_more_than_500_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            seen = {f"turn-{index:04d}" for index in range(700)}

            notify.save_seen_task_complete_ids(data_dir, seen)

            loaded = notify.seen_task_complete_ids(data_dir)
            self.assertEqual(len(loaded), 700)


if __name__ == "__main__":
    unittest.main()
