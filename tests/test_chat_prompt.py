from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path, chat_prompt, chat_prompt_with_metrics, load_chat_offset, save_chat_offset
from aha_cli.store.filesystem import (
    append_event,
    event_path,
    inbox_path,
    iter_jsonl_from,
    read_json,
    run_dir,
    status_snapshot,
    update_task_hardware_debug_config,
    update_task_proxy_config,
    update_task_skills_config,
    update_task_supervision_config,
)
from aha_cli.store.paths import config_path


class ChatPromptTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_chat_offset_persists_unprocessed_messages_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Offsets", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                inbox = inbox_path(root, run_id, "main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main")
                initial_offset = load_chat_offset(inbox, offset_file, from_start=False)
                append_message(root, run_id, "main", "queued while stopped", sender="browser", task_id="task-001", role="main")
                save_chat_offset(offset_file, initial_offset)

                self.assertEqual(load_chat_offset(inbox, offset_file, from_start=False), initial_offset)
                self.assertGreater(inbox.stat().st_size, initial_offset)

    def test_task_scoped_chat_offsets_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)

            global_offset = chat_offset_path(run, "main")
            task_one_offset = chat_offset_path(run, "main", "task-001")
            task_two_offset = chat_offset_path(run, "main", "task-002")

        self.assertNotEqual(global_offset, task_one_offset)
        self.assertNotEqual(task_one_offset, task_two_offset)
        self.assertEqual(task_one_offset.name, "chat-offset-task-001-main.json")

    def test_task_scoped_codex_chat_skips_other_task_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task scoped workers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                self.run_cli("task", "add", run_id, "Second task", "--no-dispatch")
                append_message(root, run_id, "main", "task two", sender="browser", task_id="task-002", role="main")
                append_message(root, run_id, "main", "task one", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "task one reply", None)) as run_agent:
                    code, output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertEqual(run_agent.call_count, 1)
                self.assertIn("task-001", run_agent.call_args.args[0])
                self.assertNotIn("User message from browser", output)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertEqual(browser_messages[-1]["message"], "task one reply")
                self.assertEqual(browser_messages[-1]["task_id"], "task-001")
                scoped_offset = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertTrue(scoped_offset.exists())
                self.assertFalse(chat_offset_path(run_dir(root, run_id), "main", "task-002").exists())

    def test_codex_chat_once_saves_offset_after_processed_message_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task scoped offsets", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "first", sender="browser", task_id="task-001", role="main")
                append_message(root, run_id, "main", "second", sender="browser", task_id="task-001", role="main")

                with mock.patch(
                    "aha_cli.services.chat.run_codex_exec",
                    side_effect=[(0, "reply one", None), (0, "reply two", None)],
                ) as run_agent:
                    code, first_output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")
                    self.assertEqual(code, 0)
                    code, second_output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--once")
                    self.assertEqual(code, 0)

                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)

        self.assertEqual(run_agent.call_count, 2)
        self.assertIn("main -> browser: reply one", first_output)
        self.assertIn("main -> browser: reply two", second_output)
        self.assertIn("User message from browser", run_agent.call_args_list[0].args[0])
        self.assertIn("first", run_agent.call_args_list[0].args[0])
        self.assertIn("second", run_agent.call_args_list[1].args[0])
        self.assertEqual([item["message"] for item in browser_messages[-2:]], ["reply one", "reply two"])

    def test_codex_chat_runs_turn_end_auto_context_compact_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Turn end compact hook", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(aha_root, run_id, "main", "trigger hook", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)),
                    mock.patch("aha_cli.services.chat.auto_compact_agent_context_after_turn", return_value=None) as auto_compact,
                ):
                    code, _ = self.run_cli(
                        "--home",
                        str(aha_root),
                        "codex-chat",
                        run_id,
                        "main",
                        "--task-id",
                        "task-001",
                        "--from-start",
                        "--once",
                    )

        self.assertEqual(code, 0)
        auto_compact.assert_called_once_with(aha_root, run_id, "task-001", "main")

    def test_codex_chat_records_delivered_context_fingerprints_after_successful_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Context fingerprint delivery", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_hardware_debug_config(
                    root,
                    run_id,
                    "task-001",
                    channels=[{"type": "uart", "settings": {"port": "/dev/ttyUSB0"}}],
                )
                update_task_skills_config(root, run_id, "task-001", enabled_paths=["/repo/.aha/skills/board-debug/SKILL.md"])
                append_message(root, run_id, "main", "first turn", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                session = read_json(run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json")

        self.assertEqual(code, 0)
        delivered = session["delivered_context_fingerprints"]
        self.assertTrue(delivered["hardware_debug"])
        self.assertTrue(delivered["task_skills"])
        self.assertEqual(delivered["knowledge_enabled"], "disabled")

    def test_codex_chat_surfaces_backend_error_to_browser_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend error chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "trigger failure", sender="browser", task_id="task-001", role="main")

                with mock.patch(
                    "aha_cli.services.chat.run_codex_exec",
                    return_value=(127, "Failed to start Codex backend command: codex", None),
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertEqual(code, 127)
        self.assertEqual(browser_messages[-1]["sender"], "system")
        self.assertEqual(browser_messages[-1]["coordination"], "agent_error")
        self.assertEqual(browser_messages[-1]["agent_id"], "main")
        self.assertIn("AHA runtime 检测到 `main` agent 后端异常退出", browser_messages[-1]["message"])
        self.assertIn("Failed to start Codex backend command: codex", browser_messages[-1]["message"])
        agent_errors = [event for event in events if event["type"] == "agent_error"]
        self.assertIn("Failed to start Codex backend command: codex", agent_errors[-1]["data"]["message"])

    def test_codex_chat_catches_backend_runner_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend crash chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "trigger crash", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.traceback.print_exc") as print_traceback, mock.patch(
                    "aha_cli.services.chat.run_codex_exec",
                    side_effect=AttributeError("'str' object has no attribute 'get'"),
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(code, 1)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["agents"][0]["status"], "failed")
        self.assertEqual(browser_messages[-1]["sender"], "system")
        self.assertEqual(browser_messages[-1]["coordination"], "agent_error")
        self.assertEqual(browser_messages[-1]["agent_id"], "main")
        self.assertIn("AHA runtime 检测到 `main` agent 后端异常退出", browser_messages[-1]["message"])
        self.assertIn("Codex backend crashed while handling agent turn", browser_messages[-1]["message"])
        self.assertIn("AttributeError", browser_messages[-1]["message"])
        agent_errors = [event for event in events if event["type"] == "agent_error"]
        self.assertIn("AttributeError", agent_errors[-1]["data"]["message"])
        self.assertTrue(any(event["type"] == "agent_finished" for event in events))
        print_traceback.assert_called_once()

    def test_codex_chat_passes_latest_task_proxy_env_to_codex_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Chat proxy env",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                    "--https-proxy",
                    "http://127.0.0.1:7890",
                    "--no-proxy",
                    "localhost,127.0.0.1",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_proxy_config(root, run_id, "task-001", http_proxy="http://127.0.0.1:8888")
                append_message(root, run_id, "main", "use proxy", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)) as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

        self.assertEqual(code, 0)
        proxy_env = run_agent.call_args.kwargs["proxy_env"]
        self.assertEqual(proxy_env["HTTP_PROXY"], "http://127.0.0.1:8888")
        self.assertEqual(proxy_env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(proxy_env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(proxy_env["http_proxy"], "http://127.0.0.1:8888")

    def test_chat_prompt_includes_enabled_hardware_debug_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Hardware prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_hardware_debug_config(
                    root,
                    run_id,
                    "task-001",
                    channels=[
                        {
                            "type": "uart",
                            "settings": {
                                "id": "dev-board-1",
                                "port": "/dev/ttyUSB0",
                                "baudrate": 115200,
                                "prompt": "Sgs #",
                            },
                            "operation_skill_path": "/repo/.aha/skills/uboot-uart/SKILL.md",
                            "permissions": {"write": True, "reset": True, "flash": False},
                        },
                        {
                            "type": "telnet",
                            "settings": {
                                "host": "192.168.1.20",
                                "port": 23,
                            },
                            "operation_skill_path": "/repo/.aha/skills/telnet-console/SKILL.md",
                            "permissions": {"read": True, "write": False},
                        },
                    ],
                )
                update_task_skills_config(
                    root,
                    run_id,
                    "task-001",
                    enabled_paths=["/repo/.aha/skills/board-debug/SKILL.md"],
                )
                item = append_message(root, run_id, "main", "use hardware", sender="browser", task_id="task-001", role="main")
                prompt, full_metrics = chat_prompt_with_metrics(root, run_id, "main", item, "")
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session["delivered_context_fingerprints"] = full_metrics["context_fingerprint_updates"]
                session_file.write_text(json.dumps(session), encoding="utf-8")
                sticky_item = append_message(
                    root,
                    run_id,
                    "main",
                    "use hardware again",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                )
                sticky_prompt, sticky_metrics = chat_prompt_with_metrics(root, run_id, "main", sticky_item, "")

        self.assertIn("Task skills context:", prompt)
        self.assertIn("Hardware debug context:", prompt)
        self.assertIn("/repo/.aha/skills/board-debug/SKILL.md", prompt)
        self.assertIn("channel 1: type=uart", prompt)
        self.assertIn("channel 2: type=telnet", prompt)
        self.assertIn("operation skill path: /repo/.aha/skills/uboot-uart/SKILL.md", prompt)
        self.assertIn("operation skill path: /repo/.aha/skills/telnet-console/SKILL.md", prompt)
        self.assertIn("port=/dev/ttyUSB0", prompt)
        self.assertIn("baudrate=115200", prompt)
        self.assertIn("write=True", prompt)
        self.assertNotIn("id=dev-board-1", prompt)
        self.assertNotIn("prompt=Sgs #", prompt)
        self.assertNotIn("flash=False", prompt)
        self.assertEqual(sticky_metrics["prompt_mode"], "sticky_delta")
        self.assertEqual(sticky_prompt, "use hardware again")
        self.assertEqual(set(sticky_metrics["components"]), {"user_message"})
        self.assertNotIn("Task skills context:", sticky_prompt)
        self.assertNotIn("Hardware debug context:", sticky_prompt)
        self.assertNotIn("/repo/.aha/skills/board-debug/SKILL.md", sticky_prompt)
        self.assertNotIn("operation skill path: /repo/.aha/skills/uboot-uart/SKILL.md", sticky_prompt)
        self.assertNotIn("port=/dev/ttyUSB0", sticky_prompt)
        self.assertNotIn("channel 2: type=telnet", sticky_prompt)
        self.assertNotIn("write=True", sticky_prompt)

    def test_chat_prompt_redacts_proxy_values_from_status_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Prompt redaction",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://user:secret@proxy.local:7890",
                    "--https-proxy",
                    "http://user:secret@proxy.local:7890",
                    "--no-proxy",
                    "internal.local",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "hello",
                        "task_id": "task-001",
                        "role": "main",
                    },
                    "prefix",
                )

        self.assertNotIn("secret", prompt)
        self.assertNotIn("proxy.local:7890", prompt)
        self.assertNotIn("internal.local", prompt)
        self.assertNotIn("preferred_http_proxy", prompt)

    def test_chat_prompt_with_metrics_reports_sizes_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "old-event"})

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "super-secret-user-text",
                        "task_id": "task-001",
                        "role": "main",
                    },
                    "prefix",
                )

        metrics_json = json.dumps(metrics, ensure_ascii=False)
        self.assertEqual(metrics["total"]["chars"], len(prompt))
        self.assertEqual(metrics["components"]["user_message"]["chars"], len("super-secret-user-text"))
        self.assertGreater(metrics["components"]["recent_conversation"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)
        self.assertNotIn("super-secret-user-text", metrics_json)
        self.assertNotIn("old-event", metrics_json)

    def test_chat_prompt_filters_private_host_notes_from_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host note isolation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "host",
                    "让 main 下一轮只回复数字2",
                    sender="browser",
                    task_id="task-001",
                    role="host",
                    from_agent="browser",
                    to_agent="host",
                )
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "请直接回复数字1",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertIn("请直接回复数字1", prompt)
        self.assertNotIn("让 main 下一轮只回复数字2", prompt)

    def test_chat_prompt_keeps_recovery_context_out_of_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recovery prompt context", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "继续",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                    recovery_context="上一轮 agent `main` 工作异常中断。",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertIn("AHA recovery context for this backend turn", prompt)
        self.assertIn("上一轮 agent `main` 工作异常中断。", prompt)
        self.assertIn("User message from browser", prompt)
        self.assertIn("继续", prompt)
        self.assertNotIn("用户当前发送的新消息", prompt)

    def test_chat_prompt_hides_supervision_host_from_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host prompt isolation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                )
                task = status_snapshot(root, run_id)["tasks"][0]
                append_message(
                    root,
                    run_id,
                    "browser",
                    "Agents:\n- host role=host backend=claude assignment=task supervision host agent",
                    sender="AHA",
                    task_id="task-001",
                    role="aha",
                    from_agent="aha",
                    to_agent="browser",
                    agent_id="main",
                )
                append_event(
                    root,
                    run_id,
                    "agent_message",
                    {
                        "source": "claude-chat",
                        "task_id": "task-001",
                        "target": "main",
                        "item_type": "agent_message",
                        "text": "我处理 03 号，让host agent处理02号重启问题。",
                    },
                )
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "你可以和 sub agent 一人一个问题",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertTrue(any(agent["role"] == "host" for agent in task["agents"]))
        self.assertIn("你可以和 sub agent 一人一个问题", prompt)
        self.assertNotIn("host role=host", prompt)
        self.assertNotIn("'id': 'host'", prompt)
        self.assertNotIn("'role': 'host'", prompt)
        self.assertNotIn("让host agent处理02号", prompt)
        self.assertNotIn("task supervision host agent", prompt)

    def test_chat_prompt_labels_current_host_to_main_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host current message label", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                )
                append_message(
                    root,
                    run_id,
                    "host",
                    "Supervision exchange to evaluate:\n- main_latest_reply:\n内部评审上下文",
                    sender="main",
                    task_id="task-001",
                    role="host",
                    from_agent="main",
                    to_agent="host",
                    display_sender="main",
                    display_target="host",
                    agent_id="host",
                )
                host_message = append_message(
                    root,
                    run_id,
                    "main",
                    "继续按 MI 驱动方向排查。",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                    display_sender="host",
                    display_target="main",
                    agent_id="host",
                )

                prompt = chat_prompt(root, run_id, "main", host_message, "")

        self.assertIn("User message from host -> main", prompt)
        self.assertNotIn("User message from browser", prompt)
        self.assertIn("继续按 MI 驱动方向排查。", prompt)
        self.assertNotIn("Supervision exchange to evaluate", prompt)
        self.assertNotIn("内部评审上下文", prompt)

    def test_codex_chat_records_prompt_metrics_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "measure prompt", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                metrics_events = [row for row in rows if row["type"] == "agent_prompt_metrics"]
                self.assertEqual(len(metrics_events), 1)
                metrics = metrics_events[0]["data"]
                artifact_path = run_dir(root, run_id) / metrics["prompt_ref"]["path"]
                artifact_text = artifact_path.read_text(encoding="utf-8")
                metrics_json = json.dumps(metrics, ensure_ascii=False)

        self.assertEqual(code, 0)
        self.assertEqual(metrics["source"], "codex-chat")
        self.assertEqual(metrics["task_id"], "task-001")
        self.assertGreater(metrics["total"]["chars"], 0)
        self.assertEqual(metrics["total"]["tokens"], max(1, metrics["total"]["chars"] // 4))
        self.assertGreater(metrics["components"]["recent_conversation"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)
        self.assertIn("prompt_ref", metrics)
        self.assertTrue(metrics["prompt_ref"]["path"].startswith("tasks/task-001/prompts/main-"))
        self.assertNotIn("model_guidance", metrics["components"])
        self.assertIn("User message from browser", artifact_text)
        self.assertIn("measure prompt", artifact_text)
        self.assertNotIn("User message from browser", metrics_json)
        self.assertNotIn("measure prompt", metrics_json)

    def test_claude_kimi_prompt_uses_unified_guidance_without_model_specific_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Kimi prompt guidance", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                item = {
                    "sender": "browser",
                    "message": "continue",
                    "task_id": "task-001",
                    "role": "main",
                    "ts": "2026-01-01T00:00:00+00:00",
                }

                kimi_prompt, kimi_metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    item,
                    "",
                    backend="claude",
                    requested_model="env:kimi",
                    resolved_model="kimi-k2.6",
                )
                codex_prompt, codex_metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    item,
                    "",
                    backend="codex",
                    requested_model="gpt5.5",
                    resolved_model="gpt-5.5",
                )

        self.assertIn("continue", kimi_prompt)
        self.assertNotIn("AHA model-specific operating guidance", kimi_prompt)
        self.assertNotIn("keep/drop/redo", kimi_prompt)
        self.assertNotIn("long-running test, or commit", kimi_prompt)
        self.assertNotIn("Re-audit the root cause", kimi_prompt)
        self.assertNotIn("model_guidance", kimi_metrics["components"])
        self.assertNotIn("AHA model-specific operating guidance", codex_prompt)
        self.assertNotIn("keep/drop/redo", codex_prompt)
        self.assertNotIn("long-running test, or commit", codex_prompt)
        self.assertNotIn("Re-audit the root cause", codex_prompt)
        self.assertNotIn("model_guidance", codex_metrics["components"])

    def test_minimax_sticky_delta_passes_plain_user_message_like_other_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "MiniMax sticky prompt guidance", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session_file.write_text(json.dumps(session), encoding="utf-8")

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "next request",
                        "task_id": "task-001",
                        "role": "main",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "",
                    backend="claude",
                    requested_model="env:MiniMax-M2.7-highspeed",
                    resolved_model="MiniMax-M2.7-highspeed",
                )

        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertEqual(prompt, "next request")
        self.assertNotIn("AHA model-specific operating guidance", prompt)
        self.assertNotIn("model_guidance", metrics["components"])
        self.assertEqual(set(metrics["components"]), {"user_message"})
        self.assertNotIn("task_context", metrics["components"])

    def test_kimi_followup_prompt_does_not_add_model_specific_root_cause_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Kimi follow-up gate", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "target": "main", "message": "first bug report"})
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "main", "target": "browser", "message": "first fix"})
                item = {
                    "sender": "browser",
                    "message": "same issue still fails",
                    "task_id": "task-001",
                    "role": "main",
                    "ts": "2026-01-01T00:00:00+00:00",
                }

                kimi_prompt, kimi_metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    item,
                    "",
                    backend="claude",
                    requested_model="env:kimi",
                    resolved_model="kimi-k2.6",
                )
                codex_prompt, codex_metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    item,
                    "",
                    backend="codex",
                    requested_model="gpt5.5",
                    resolved_model="gpt-5.5",
                )

        self.assertNotIn("AHA runtime root-cause re-audit gate", kimi_prompt)
        self.assertNotIn("re-read the relevant code/logs/tests", kimi_prompt)
        self.assertNotIn("root_cause_reaudit_gate", kimi_metrics["components"])
        self.assertNotIn("AHA runtime root-cause re-audit gate", codex_prompt)
        self.assertNotIn("root_cause_reaudit_gate", codex_metrics["components"])

    def test_claude_chat_passes_kimi_model_context_to_prompt_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "claude")
                config_file = root / ".aha" / "config.json"
                config = read_json(config_file)
                config["claude"] = {
                    "model": "env:kimi",
                    "env": [{"name": "kimi", "ANTHROPIC_MODEL": "kimi-k2.6"}],
                }
                config_file.write_text(json.dumps(config), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Kimi chat prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "check prompt", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, "reply", None)) as run_agent:
                    code, _ = self.run_cli(
                        "claude-chat",
                        run_id,
                        "main",
                        "--task-id",
                        "task-001",
                        "--from-start",
                        "--once",
                        "--model",
                        "env:kimi",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                metrics_events = [row for row in rows if row["type"] == "agent_prompt_metrics"]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)

        self.assertEqual(code, 0)
        prompt = run_agent.call_args.args[0]
        self.assertIn("check prompt", prompt)
        self.assertNotIn("AHA model-specific operating guidance", prompt)
        self.assertNotIn("model_guidance", metrics_events[-1]["data"]["components"])
        self.assertEqual(session["requested_model"], "env:kimi")
        self.assertEqual(session["resolved_model"], "kimi-k2.6")
        self.assertEqual(session["model"], "kimi-k2.6")
        self.assertIsNone(run_agent.call_args.kwargs["model"])

    def test_commit_policy_uses_current_claude_model_over_stale_session_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale session model", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend"] = "codex"
                session["requested_model"] = "gpt-5.5"
                session["resolved_model"] = "gpt-5.5"
                session["model"] = "gpt-5.5"
                session_file.write_text(json.dumps(session), encoding="utf-8")

                prompt, _metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "提交代码",
                        "task_id": "task-001",
                        "role": "main",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "",
                    backend="claude",
                    requested_model="env:kimi-k2.7",
                    resolved_model="kimi-k2.7",
                )

        self.assertIn("Generated-by: AHA Claude kimi-k2.7", prompt)
        self.assertNotIn("Generated-by: AHA Claude GPT-5.5", prompt)

    def test_chat_prompt_uses_recent_conversation_chains_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recent prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(5):
                    append_event(
                        root,
                        run_id,
                        "message",
                        {"task_id": "task-001", "sender": "browser", "target": "main", "message": f"request-{index}"},
                    )
                    append_event(
                        root,
                        run_id,
                        "message",
                        {"task_id": "task-001", "sender": "main", "target": "browser", "message": f"reply-{index}"},
                    )
                append_event(root, run_id, "agent_status_changed", {"task_id": "task-001", "agent_id": "main", "status": "running"})

                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "status", "task_id": "task-001", "role": "main"},
                    "",
                )

        self.assertIn("request-4", prompt)
        self.assertIn("reply-4", prompt)
        self.assertIn("request-2", prompt)
        self.assertNotIn("request-1", prompt)
        self.assertNotIn("agent_status_changed", prompt)

    def test_chat_prompt_filters_internal_supervision_conversation_and_caps_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recent prompt budget", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "target": "main", "message": "older request"})
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "main", "target": "browser", "message": "older reply"})
                append_event(
                    root,
                    run_id,
                    "message",
                    {
                        "task_id": "task-001",
                        "sender": "main",
                        "target": "host",
                        "message": "Supervision exchange to evaluate:\n"
                        "- source: browser_main_reply\n"
                        "- browser_latest_request:\n"
                        f"{'internal-browser-request ' * 80}\n\n"
                        "- main_latest_reply:\n"
                        f"{'duplicated-main-reply ' * 80}",
                    },
                )
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "target": "main", "message": "latest browser request"})
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "main", "target": "sub-001", "message": "delegate useful sub work"})
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "sub-001", "target": "main", "message": "sub useful result"})
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "main", "target": "browser", "message": "latest main reply"})

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "next", "task_id": "task-001", "role": "main"},
                    "",
                )

        self.assertLessEqual(metrics["components"]["recent_conversation"]["chars"], 1800)
        self.assertIn("latest browser request", prompt)
        self.assertIn("delegate useful sub work", prompt)
        self.assertIn("sub useful result", prompt)
        self.assertIn("latest main reply", prompt)
        self.assertNotIn("Supervision exchange to evaluate", prompt)
        self.assertNotIn("internal-browser-request", prompt)
        self.assertNotIn("duplicated-main-reply", prompt)

    def test_chat_prompt_scopes_status_and_events_to_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Prompt scoping", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                code, _ = self.run_cli("task", "add", run_id, "Foreign verbose task title that should stay out", "--no-dispatch")
                self.assertEqual(code, 0)
                code, _ = self.run_cli("task", "add", run_id, "Current compact prompt title", "--no-dispatch")
                self.assertEqual(code, 0)
                append_event(root, run_id, "message", {"task_id": "task-002", "sender": "browser", "target": "main", "message": "foreign-event"})
                append_event(root, run_id, "message", {"task_id": "task-003", "sender": "browser", "target": "main", "message": "current-event"})

                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "status", "task_id": "task-003", "role": "main"},
                    "",
                )

        self.assertIn("Current compact prompt title", prompt)
        self.assertIn("current-event", prompt)
        self.assertIn("Task context:", prompt)
        self.assertIn("Intent priority:", prompt)
        self.assertNotIn("Foreign verbose task title that should stay out", prompt)
        self.assertNotIn("foreign-event", prompt)

    def test_full_prompt_uses_minimal_policy_reminders_without_coordination_or_commit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Compact policy reminder", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                prompt, metrics = chat_prompt_with_metrics(
                    aha_root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "hello", "task_id": "task-001", "role": "main"},
                    "",
        )

        self.assertEqual(metrics["prompt_mode"], "full")
        self.assertIn("Repository guard:", prompt)
        self.assertNotIn("AHA action contract reminder:", prompt)
        self.assertNotIn("AHA action output:", prompt)
        self.assertNotIn('"type": "spawn_sub"', prompt)
        self.assertNotIn("For a brand-new sub-agent", prompt)
        self.assertNotIn("Commit policy reminder:", prompt)
        self.assertNotIn("Run goal:", prompt)
        self.assertNotIn("- workflow_template:", prompt)
        self.assertNotIn("- delegation_policy:", prompt)
        self.assertNotIn("- preferred_sub_backend:", prompt)
        self.assertNotIn("Ownership and routing policy:", prompt)
        self.assertNotIn("Commit ownership policy:", prompt)
        self.assertNotIn("Commit message policy:", prompt)
        self.assertNotIn("Generated-by:", prompt)
        self.assertEqual(metrics["components"]["action_contract"]["chars"], 0)
        self.assertEqual(metrics["components"]["coordination_policy"]["chars"], 0)
        self.assertEqual(metrics["components"]["commit_policy"]["chars"], 0)

    def test_full_prompt_expands_coordination_policy_on_delegation_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Coordination policy", "--agents", "2")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                prompt, metrics = chat_prompt_with_metrics(
                    aha_root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "请并行拆给子 agent", "task_id": "task-001", "role": "main"},
                    "",
        )

        self.assertIn("AHA coordination policy:", prompt)
        self.assertIn("AHA action output:", prompt)
        self.assertIn("Spawn only for independent parallel work", prompt)
        self.assertIn("Action formats:", prompt)
        self.assertIn('"type":"route_to_agent"', prompt)
        self.assertNotIn("Commit message policy:", prompt)
        self.assertGreater(metrics["components"]["action_contract"]["chars"], 0)
        self.assertGreater(metrics["components"]["coordination_policy"]["chars"], 0)
        self.assertEqual(metrics["components"]["commit_policy"]["chars"], 0)

    def test_full_prompt_expands_commit_policy_on_commit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Commit policy", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                prompt, metrics = chat_prompt_with_metrics(
                    aha_root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "请提交代码", "task_id": "task-001", "role": "main"},
                    "",
                )

        self.assertIn("Commit ownership policy:", prompt)
        self.assertIn("Commit message policy:", prompt)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", prompt)
        self.assertIn("aha commit --type <type>", prompt)
        self.assertGreater(metrics["components"]["commit_policy"]["chars"], 0)

    def test_chat_prompt_uses_delta_for_existing_sticky_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Sticky prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(aha_root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_event(
                    aha_root,
                    run_id,
                    "agent_message",
                    {"task_id": "task-001", "target": "main", "text": "already-in-backend-session"},
                )
                append_event(aha_root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                append_event(aha_root, run_id, "task_hidden", {"task_id": "task-001"})

                prompt, metrics = chat_prompt_with_metrics(
                    aha_root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "next request",
                        "task_id": "task-001",
                        "role": "main",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
        )

        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertEqual(prompt, "next request")
        self.assertNotIn("prefix", prompt)
        self.assertNotIn("Current task constraints:", prompt)
        self.assertNotIn("backend-session-1", prompt)
        self.assertNotIn("Intent priority policy:", prompt)
        self.assertNotIn("task.description as the original request / historical background", prompt)
        self.assertNotIn("Recent conversation:", prompt)
        self.assertNotIn("task_hidden", prompt)
        self.assertNotIn("AHA coordination policy", prompt)
        self.assertNotIn("already-in-backend-session", prompt)
        self.assertEqual(set(metrics["components"]), {"user_message"})
        self.assertNotIn("recent_conversation", metrics["components"])
        self.assertNotIn("run_goal", metrics["components"])
        self.assertEqual(metrics["components"]["user_message"]["chars"], len("next request"))
        self.assertNotIn("delta_status", metrics["components"])
        self.assertNotIn("task_context", metrics["components"])
        self.assertNotIn("Commit message policy:", prompt)
        self.assertNotIn("commit_policy", metrics["components"])

    def test_sticky_delta_injects_runtime_context_delta_once_for_task_capability_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sticky task capability delta", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session["delivered_context_fingerprints"] = {
                    "hardware_debug": "",
                    "task_skills": "",
                    "knowledge_enabled": "disabled",
                }
                session_file.write_text(json.dumps(session), encoding="utf-8")
                update_task_hardware_debug_config(
                    root,
                    run_id,
                    "task-001",
                    channels=[
                        {
                            "type": "uart",
                            "settings": {"port": "/dev/ttyUSB0", "baudrate": 115200},
                            "operation_skill_path": "/repo/.aha/skills/uboot-uart/SKILL.md",
                            "permissions": {"read": True, "write": True},
                        }
                    ],
                )
                update_task_skills_config(
                    root,
                    run_id,
                    "task-001",
                    enabled_paths=["/repo/.aha/skills/board-debug/SKILL.md"],
                )

                item = append_message(root, run_id, "main", "continue", sender="browser", task_id="task-001", role="main")
                prompt, metrics = chat_prompt_with_metrics(root, run_id, "main", item, "")
                session["delivered_context_fingerprints"] = metrics["context_fingerprint_updates"]
                session_file.write_text(json.dumps(session), encoding="utf-8")
                next_item = append_message(root, run_id, "main", "next", sender="browser", task_id="task-001", role="main")
                next_prompt, next_metrics = chat_prompt_with_metrics(root, run_id, "main", next_item, "")

        self.assertIn("AHA runtime context update", prompt)
        self.assertIn("Hardware debug context:", prompt)
        self.assertIn("Task skills context:", prompt)
        self.assertIn("operation skill path: /repo/.aha/skills/uboot-uart/SKILL.md", prompt)
        self.assertIn("/repo/.aha/skills/board-debug/SKILL.md", prompt)
        self.assertIn("continue", prompt)
        self.assertIn("context_delta", metrics["components"])
        self.assertTrue(metrics["context_fingerprint_updates"]["hardware_debug"])
        self.assertTrue(metrics["context_fingerprint_updates"]["task_skills"])
        self.assertEqual(next_prompt, "next")
        self.assertEqual(set(next_metrics["components"]), {"user_message"})

    def test_sticky_delta_injects_knowledge_delta_once_when_knowledge_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sticky knowledge delta", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session["delivered_context_fingerprints"] = {
                    "hardware_debug": "",
                    "task_skills": "",
                    "knowledge_enabled": "disabled",
                }
                session_file.write_text(json.dumps(session), encoding="utf-8")
                cfg_path = config_path(root)
                cfg = read_json(cfg_path)
                cfg["knowledge"]["enabled"] = True
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

                item = append_message(root, run_id, "main", "continue", sender="browser", task_id="task-001", role="main")
                prompt, metrics = chat_prompt_with_metrics(root, run_id, "main", item, "")
                session["delivered_context_fingerprints"] = metrics["context_fingerprint_updates"]
                session_file.write_text(json.dumps(session), encoding="utf-8")
                next_item = append_message(root, run_id, "main", "next", sender="browser", task_id="task-001", role="main")
                next_prompt, next_metrics = chat_prompt_with_metrics(root, run_id, "main", next_item, "")

        self.assertIn("AHA runtime context update", prompt)
        self.assertIn("Knowledge base context:", prompt)
        self.assertIn("Knowledge base is now enabled for this task", prompt)
        self.assertEqual(metrics["context_fingerprint_updates"]["knowledge_enabled"], "enabled")
        self.assertEqual(next_prompt, "next")
        self.assertEqual(set(next_metrics["components"]), {"user_message"})

    def test_plain_sticky_message_skips_runtime_context_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Plain sticky queued", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session["delivered_context_fingerprints"] = {
                    "hardware_debug": "",
                    "task_skills": "",
                    "knowledge_enabled": "disabled",
                }
                session_file.write_text(json.dumps(session), encoding="utf-8")
                cfg_path = config_path(root)
                cfg = read_json(cfg_path)
                cfg["knowledge"]["enabled"] = True
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

                item = append_message(
                    root,
                    run_id,
                    "main",
                    "继续刚才的问题",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    plain_sticky=True,
                )
                prompt, metrics = chat_prompt_with_metrics(root, run_id, "main", item, "")

        self.assertEqual(prompt, "继续刚才的问题")
        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertEqual(set(metrics["components"]), {"user_message"})
        self.assertNotIn("AHA runtime context update", prompt)
        self.assertNotIn("context_delta", metrics["components"])
        self.assertNotIn("context_fingerprint_updates", metrics)

    def test_sticky_delta_expands_commit_policy_on_commit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "claude")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Sticky commit policy", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(aha_root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend"] = "claude"
                session["backend_session_id"] = "claude-session-1"
                session["requested_model"] = "env:MiniMax-M2.7-highspeed"
                session["resolved_model"] = "MiniMax-M2.7-highspeed"
                session["model"] = "MiniMax-M2.7-highspeed"
                session_file.write_text(json.dumps(session), encoding="utf-8")

                prompt, metrics = chat_prompt_with_metrics(
                    aha_root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "在写个文件提交",
                        "task_id": "task-001",
                        "role": "main",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
                    backend="claude",
                    requested_model="env:MiniMax-M2.7-highspeed",
                    resolved_model="MiniMax-M2.7-highspeed",
                )

        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertIn("Commit ownership policy:", prompt)
        self.assertIn("Commit message policy:", prompt)
        self.assertIn("Generated-by: AHA Claude MiniMax-M2.7-highspeed", prompt)
        self.assertGreater(metrics["components"]["commit_policy"]["chars"], 0)
        self.assertIn("sticky_context", metrics["components"])
        self.assertNotIn("task_context", metrics["components"])

    def test_host_sticky_delta_uses_compact_supervision_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host prompt budget", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                item = {
                    "sender": "main",
                    "message": "Supervision exchange to evaluate:\n- source: browser_main_reply\n- browser_latest_request:\ncheck\n\n- main_latest_reply:\ndone",
                    "task_id": "task-001",
                    "role": "host",
                    "ts": "2026-01-01T00:00:00+00:00",
                }
                full_prompt, full_metrics = chat_prompt_with_metrics(root, run_id, "host", item, "")
                host_session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "host.json"
                host_session = read_json(host_session_file)
                host_session["backend_session_id"] = "host-backend-session"
                host_session_file.write_text(json.dumps(host_session), encoding="utf-8")
                sticky_prompt, sticky_metrics = chat_prompt_with_metrics(root, run_id, "host", item, "")

        self.assertEqual(sticky_metrics["prompt_mode"], "sticky_delta")
        self.assertIn("AHA host instructions:", full_prompt)
        self.assertIn("AHA host sticky summary:", sticky_prompt)
        self.assertIn("Output only one JSON object", sticky_prompt)
        self.assertIn("JSON response field is the only natural-language message", sticky_prompt)
        self.assertIn("Return exactly one JSON object", sticky_prompt)
        self.assertIn("Do not call Claude native tools such as AskUserQuestion or ExitPlanMode", sticky_prompt)
        self.assertIn("AHA JSON decision ask_user only", sticky_prompt)
        self.assertIn('browser -> host: 让其直接回复测试111', sticky_prompt)
        self.assertIn('"response":"请直接回复测试111"', sticky_prompt)
        self.assertIn("commit, merge, delete", sticky_prompt)
        self.assertIn("route executable work to task-main", sticky_prompt)
        self.assertNotIn("Use your read-only project access", sticky_prompt)
        self.assertIn("supervision_host_context", full_metrics["components"])
        self.assertNotIn("supervision_host_context", sticky_metrics["components"])
        self.assertIn("supervision_host_delta_context", sticky_metrics["components"])
        self.assertLess(
            sticky_metrics["components"]["supervision_host_delta_context"]["chars"],
            full_metrics["components"]["supervision_host_context"]["chars"] // 2,
        )
        self.assertLess(sticky_metrics["total"]["chars"], full_metrics["total"]["chars"])

    def test_host_prompt_does_not_duplicate_inlined_browser_to_host_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host notes dedupe", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                append_message(
                    root,
                    run_id,
                    "host",
                    "让 host 回复测试消息3",
                    sender="browser",
                    task_id="task-001",
                    role="host",
                    from_agent="browser",
                    to_agent="host",
                )
                item = {
                    "sender": "main",
                    "message": "Supervision exchange to evaluate:\n"
                    "- source: browser_main_reply\n"
                    "- browser_latest_request:\n直接回复测试消息2\n\n"
                    "- browser_to_host_notes:\nbrowser -> host: 让 host 回复测试消息3\n\n"
                    "- main_latest_reply:\n测试消息2",
                    "task_id": "task-001",
                    "role": "host",
                    "ts": "2026-01-01T00:00:00+00:00",
                }

                prompt = chat_prompt(root, run_id, "host", item, "")

        self.assertEqual(prompt.count("browser -> host: 让 host 回复测试消息3"), 1)
        self.assertIn("Recent browser-to-host notes:\n(none)", prompt)
