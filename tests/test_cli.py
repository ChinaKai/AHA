from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from aha_cli.backends.claude import build_claude_exec_command, claude_permission_mode, handle_claude_event, run_claude_exec
from aha_cli.backends.codex import build_codex_exec_command, handle_codex_event, is_context_overflow_message
from aha_cli.backends.registry import agent_backend_names, agent_backends, backend_names, model_options
from aha_cli.cli import append_message, main, task_dashboard_html, task_snapshot
from aha_cli.services.commit_policy import format_commit_message, validate_commit_message
from aha_cli.services.chat import chat_offset_path, chat_prompt, chat_prompt_with_metrics, load_chat_offset, save_chat_offset, status_from_agent_result
from aha_cli.services.backend_runtime import _process_matches_home, backend_status, start_backend, stop_task_backends
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.services.orchestrator import (
    action_response_text,
    execute_actions,
    extract_action_payload,
    monitor_task_coordination,
    record_sub_agent_report,
    task_assignment_prompt,
)
from aha_cli.store.filesystem import (
    add_agent,
    append_jsonl,
    append_event,
    complete_task,
    conversation_events_page,
    delete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    iter_jsonl_reverse,
    list_task_lifecycle_rounds,
    list_task_rounds,
    read_json,
    run_dir,
    mark_task_coordination,
    reopen_task,
    set_agent_status,
    set_task_hidden,
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    update_task_proxy_config,
    update_agent_config,
    update_agent_runtime,
    write_task_result,
)
from aha_cli.web.server import (
    backend_session_jsonl_info,
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    handle_ui_client,
    handle_send_payload,
    handle_slash_command,
    recover_stale_running_agent,
    web_status_snapshot,
    workspace_options,
)
from aha_cli.websocket.server import handle_ws_client, ws_read_text


def write_plan_statuses(root_path: str, run_id: str, task_id: str, agent_id: str, iterations: int) -> None:
    root = Path(root_path)
    for _ in range(iterations):
        set_task_status(root, run_id, task_id, "running")
        set_agent_status(root, run_id, task_id, agent_id, "running")


def append_jsonl_records(path: str, worker_id: int, iterations: int) -> None:
    for index in range(iterations):
        append_jsonl(Path(path), {"worker": worker_id, "index": index})


async def fetch_ui_response(
    root: Path,
    run_id: str,
    target: str,
    timeout: float = 1.0,
    method: str = "GET",
    payload: dict | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    server = await asyncio.start_server(lambda reader, writer: handle_ui_client(root, run_id, reader, writer), "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()
    try:
        reader, writer = await asyncio.open_connection(host, port)
        body_bytes = body if body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
        request_headers = {"Host": "test", "Connection": "close", **(headers or {})}
        if payload is not None and not any(key.lower() == "content-type" for key in request_headers):
            request_headers["Content-Type"] = "application/json"
        header_lines = [f"{method} {target} HTTP/1.1"]
        header_lines.extend(f"{key}: {value}" for key, value in request_headers.items())
        header_lines.append(f"Content-Length: {len(body_bytes)}")
        writer.write(
            ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii")
            + body_bytes
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return response
    finally:
        server.close()
        await server.wait_closed()


async def fetch_initial_ws_messages(root: Path, run_id: str, timeout: float = 0.2) -> list[dict]:
    return await fetch_ws_messages(root, run_id, timeout=timeout)


async def fetch_ws_messages(root: Path, run_id: str, path: str = "/", timeout: float = 0.2, max_messages: int = 2) -> list[dict]:
    server = await asyncio.start_server(lambda reader, writer: handle_ws_client(root, run_id, reader, writer, 0.05), "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()
    writer = None
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                "Host: test\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        messages = []
        while len(messages) < max_messages:
            try:
                next_message = await asyncio.wait_for(ws_read_text(reader), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if next_message:
                messages.append(json.loads(next_message))
        writer.close()
        await writer.wait_closed()
        return messages
    finally:
        if writer and not writer.is_closing():
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


def json_response_body(response: bytes) -> dict:
    return json.loads(response.split(b"\r\n\r\n", 1)[1].decode("utf-8"))


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_command_backend_is_not_an_agent_backend(self) -> None:
        self.assertIn("command", backend_names())
        self.assertNotIn("command", agent_backend_names())
        self.assertIn("codex", agent_backend_names())
        self.assertIn("claude", agent_backend_names())

    def test_model_options_are_bound_to_agent_backend(self) -> None:
        codex_options = model_options("codex")
        claude_options = model_options("claude")
        stub_options = model_options("stub")
        self.assertEqual(codex_options[0]["name"], "")
        self.assertEqual(codex_options[0]["label"], "default")
        self.assertIn("gpt-5.3-codex", {item["name"] for item in codex_options})
        self.assertEqual(claude_options, [{"name": "", "label": "default"}])
        self.assertEqual(stub_options, [{"name": "", "label": "default"}])
        self.assertIn("commands", agent_backends()[0])

    def test_run_export_import_redacts_proxy_and_marks_sessions_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Portable run",
                    "--agents",
                    "1",
                    "--enable-proxy",
                    "--http-proxy",
                    "http://user:secret@example.test:8080",
                    "--workspace-path",
                    str(root),
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-secret"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                runtime_file = run_dir(root, run_id) / "runtime" / "local.cache"
                runtime_file.parent.mkdir(parents=True)
                runtime_file.write_text("local-only", encoding="utf-8")

                archive = root / "portable.tar.gz"
                code, output = self.run_cli("run", "export", run_id, "-o", str(archive))
                self.assertEqual(code, 0)
                self.assertIn(f"Exported run {run_id}", output)

                with tarfile.open(archive, "r:gz") as exported:
                    names = set(exported.getnames())
                    self.assertIn("aha-run-manifest.json", names)
                    self.assertIn("run/plan.json", names)
                    self.assertNotIn("run/runtime/local.cache", names)
                    plan = json.load(exported.extractfile("run/plan.json"))
                    exported_session = json.load(exported.extractfile("run/tasks/task-001/sessions/main.json"))
                self.assertEqual(plan["tasks"][0]["preferred_http_proxy"], "<redacted>")
                self.assertNotIn("secret", json.dumps(plan))
                self.assertIsNone(exported_session["backend_session_id"])
                self.assertEqual(exported_session["imported_backend_session_id"], "backend-secret")

                import_home = root / "imported.aha"
                code, import_output = self.run_cli("--home", str(import_home), "run", "import", str(archive))
                self.assertEqual(code, 0)
                imported_line = [line for line in import_output.splitlines() if line.startswith("Imported run ")][0]
                imported_run_id = imported_line.split(" as ", 1)[1]
                self.assertNotEqual(imported_run_id, run_id)
                imported_plan = read_json(run_dir(import_home, imported_run_id) / "plan.json")
                imported_session = read_json(run_dir(import_home, imported_run_id) / "tasks" / "task-001" / "sessions" / "main.json")
                self.assertEqual(imported_plan["id"], imported_run_id)
                self.assertEqual(imported_session["status"], "imported")
                self.assertIsNone(imported_session["backend_session_id"])
                self.assertEqual(imported_session["imported_from_run_id"], run_id)
                self.assertFalse((run_dir(import_home, imported_run_id) / "runtime").exists())
                snapshot = status_snapshot(import_home, imported_run_id)
                self.assertEqual(snapshot["tasks"][0]["agents"][0]["session_status"], "imported")

    def test_agent_result_status_detects_blocked_reply(self) -> None:
        self.assertEqual(status_from_agent_result(0, "done"), "completed")
        self.assertEqual(status_from_agent_result(1, "done"), "failed")
        self.assertEqual(status_from_agent_result(0, "文件没有落盘，因为 read-only sandbox"), "blocked")
        self.assertEqual(status_from_agent_result(0, "当前沙箱是只读，写入被拦截"), "blocked")
        self.assertEqual(status_from_agent_result(0, "NAS mp4 写入失败，导致状态抖动"), "completed")
        self.assertEqual(status_from_agent_result(0, "不是 NAS 参数写入失败，配置已经生效"), "completed")
        self.assertEqual(status_from_agent_result(0, '`write_task_result()` 写入 `task["output_file"]`'), "completed")

    def test_running_status_keeps_original_task_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Timing", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                with mock.patch(
                    "aha_cli.store.filesystem.utc_now",
                    side_effect=[
                        "2026-05-15T00:00:00+00:00",
                        "2026-05-15T00:00:01+00:00",
                        "2026-05-15T00:05:00+00:00",
                        "2026-05-15T00:05:01+00:00",
                    ],
                ):
                    set_task_status(root, run_id, "task-001", "running")
                    set_task_status(root, run_id, "task-001", "running")

                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["started_at"], "2026-05-15T00:00:00+00:00")

    def test_running_status_does_not_reopen_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No reopen", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                completed = task_snapshot(root, run_id, "task-001")["task"]

                set_task_status(root, run_id, "task-001", "running")
                detail = task_snapshot(root, run_id, "task-001")

        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["exit_code"], 0)
        self.assertEqual(detail["task"]["finished_at"], completed["finished_at"])

    def test_awaiting_user_status_does_not_reopen_terminal_task_without_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No implicit reopen", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                completed = task_snapshot(root, run_id, "task-001")["task"]

                set_task_status(root, run_id, "task-001", "awaiting_user")
                detail = task_snapshot(root, run_id, "task-001")

                reopened = reopen_task(root, run_id, "task-001")

        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["exit_code"], 0)
        self.assertEqual(detail["task"]["finished_at"], completed["finished_at"])
        self.assertEqual(reopened["status"], "awaiting_user")

    def test_agent_status_started_at_tracks_status_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent timing", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                with mock.patch(
                    "aha_cli.store.filesystem.utc_now",
                    side_effect=[
                        "2026-05-15T00:00:00+00:00",
                        "2026-05-15T00:00:01+00:00",
                        "2026-05-15T00:00:10+00:00",
                        "2026-05-15T00:00:11+00:00",
                        "2026-05-15T00:01:00+00:00",
                        "2026-05-15T00:01:01+00:00",
                    ],
                ):
                    set_agent_status(root, run_id, "task-001", "main", "running")
                    set_agent_status(root, run_id, "task-001", "main", "running")
                    set_agent_status(root, run_id, "task-001", "main", "waiting")

                agent = task_snapshot(root, run_id, "task-001")["task"]["agents"][0]

        self.assertEqual(agent["status"], "waiting")
        self.assertEqual(agent["status_started_at"], "2026-05-15T00:01:00+00:00")
        self.assertEqual(agent["last_active_at"], "2026-05-15T00:01:00+00:00")
        self.assertEqual(agent["started_at"], "2026-05-15T00:00:10+00:00")

    def test_parallel_plan_writers_do_not_collide_on_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Parallel writers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")

            workers = [
                multiprocessing.Process(
                    target=write_plan_statuses,
                    args=(str(root), run_id, "task-001", agent_id, 40),
                )
                for agent_id in ("main", "sub-001")
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            for worker in workers:
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            snapshot = status_snapshot(root, run_id)
            agents = {agent["id"]: agent["status"] for agent in snapshot["tasks"][0]["agents"]}
            self.assertEqual(agents["main"], "running")
            self.assertEqual(agents["sub-001"], "running")

    def test_jsonl_appends_are_valid_under_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.jsonl")
            workers = [
                multiprocessing.Process(target=append_jsonl_records, args=(path, worker_id, 50))
                for worker_id in range(4)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            for worker in workers:
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            events, _ = iter_jsonl_from(Path(path), 0)

        self.assertEqual(len(events), 200)
        self.assertFalse(any(event.get("type") == "malformed_event" for event in events))
        self.assertEqual(len({(event["worker"], event["index"]) for event in events}), 200)

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
        self.assertIn("'preferred_http_proxy': '<set>'", prompt)

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
        self.assertGreater(metrics["components"]["status_snapshot"]["chars"], 0)
        self.assertGreater(metrics["components"]["recent_events"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)
        self.assertNotIn("super-secret-user-text", metrics_json)
        self.assertNotIn("old-event", metrics_json)

    def test_codex_resume_command_keeps_workspace_write_scope(self) -> None:
        cmd = build_codex_exec_command(
            codex_bin="codex",
            model=None,
            approval="never",
            sandbox="workspace-write",
            cwd=Path("/tmp/project"),
            output_file=Path("/tmp/out.md"),
            json_events=True,
            session_id="session-123",
        )
        self.assertEqual(cmd[:9], ["codex", "-a", "never", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", "/tmp/project"])
        self.assertIn("resume", cmd)
        self.assertIn("session-123", cmd)

    def test_codex_command_events_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_codex_event(
                json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "pwd", "status": "in_progress"}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="sub-001",
            )
            handle_codex_event(
                json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pwd", "status": "completed", "exit_code": 0, "aggregated_output": "x" * 1300}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="sub-001",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["type"], "agent_command_started")
            self.assertEqual(rows[1]["type"], "agent_command_finished")
            self.assertEqual(rows[1]["data"]["command"], "pwd")
            self.assertEqual(rows[1]["data"]["target"], "sub-001")
            self.assertEqual(len(rows[1]["data"]["output_tail"]), 1200)

    def test_codex_thread_started_reactivates_reset_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            session = {"status": "reset", "backend_session_id": None}
            handle_codex_event(
                json.dumps({"type": "thread.started", "thread_id": "new-codex-session"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="main",
                session=session,
            )

        self.assertEqual(session["backend_session_id"], "new-codex-session")
        self.assertEqual(session["status"], "active")

    def test_codex_context_overflow_event_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_codex_event(
                json.dumps({"type": "error", "message": "Codex ran out of room in the model's context window."}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="main",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(is_context_overflow_message("prompt is too long: context length exceeded"))
        self.assertFalse(is_context_overflow_message("authentication failed"))
        self.assertEqual([row["type"] for row in rows], ["agent_error", "agent_context_overflow"])
        self.assertEqual(rows[1]["data"]["reason"], "context_window")

    def test_claude_permission_mode_maps_sandbox(self) -> None:
        self.assertEqual(claude_permission_mode("research", "read-only"), "plan")
        self.assertEqual(claude_permission_mode("research", "workspace-write"), "acceptEdits")
        self.assertEqual(claude_permission_mode("research", "danger-full-access"), "bypassPermissions")
        self.assertEqual(claude_permission_mode("research", "auto"), "plan")
        self.assertEqual(claude_permission_mode("implementation", "auto"), "acceptEdits")

    def test_claude_resume_command_uses_stream_json(self) -> None:
        cmd = build_claude_exec_command(
            claude_bin="claude",
            model="sonnet",
            permission_mode="acceptEdits",
            session_id="session-123",
        )
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("--resume", cmd)
        self.assertIn("session-123", cmd)

    def test_claude_stream_events_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            session: dict = {"status": "reset"}
            handle_claude_event(
                json.dumps({"type": "system", "subtype": "init", "session_id": "claude-session"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 1}, "session_id": "claude-session"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(session["backend_session_id"], "claude-session")
        self.assertEqual(session["status"], "active")
        self.assertEqual([row["type"] for row in rows], ["agent_thread", "agent_message", "agent_command_started", "agent_command_finished", "agent_usage"])
        self.assertEqual(rows[1]["data"]["text"], "hello")
        self.assertEqual(rows[2]["data"]["command"], "pwd")
        self.assertEqual(rows[3]["data"]["output_tail"], "ok")
        self.assertEqual(rows[4]["data"]["usage"]["input_tokens"], 1)

    def test_claude_context_overflow_event_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_claude_event(
                json.dumps({"type": "error", "message": "prompt is too long: context length exceeded"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["type"] for row in rows], ["agent_error", "agent_context_overflow"])
        self.assertEqual(rows[1]["data"]["reason"], "context_window")

    def test_plan_run_merge_with_stub_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                code, _ = self.run_cli("init", "--portable")
                self.assertEqual(code, 0)

                code, plan_output = self.run_cli("plan", "Study a repo", "--agents", "2")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, _ = self.run_cli("run", run_id, "--parallel", "2")
                self.assertEqual(code, 0)

                code, status = self.run_cli("status", run_id)
                self.assertEqual(code, 0)
                self.assertIn("[completed]", status)

                code, merge_output = self.run_cli("merge", run_id)
                self.assertEqual(code, 0)
                self.assertIn("merged-report.md", merge_output)
                self.assertTrue((root / ".aha" / "runs" / run_id / "merged-report.md").exists())

    def test_plan_uses_aha_home_env_without_local_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            home = Path(tmp) / "aha-home"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd), mock.patch.dict(os.environ, {"AHA_HOME": str(home)}):
                code, plan_output = self.run_cli("plan", "Home env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = next(line.split(": ", 1)[1] for line in plan_output.splitlines() if line.startswith("Created run:"))

                self.assertTrue((home / "config.json").exists())
                self.assertTrue((home / "runs" / run_id / "plan.json").exists())
                self.assertFalse((cwd / ".aha").exists())
                plan = read_json(home / "runs" / run_id / "plan.json")
                self.assertEqual(plan["tasks"][0]["workspace_path"], str(cwd))

                code, status = self.run_cli("status", run_id)
                self.assertEqual(code, 0)
                self.assertIn("Goal: Home env", status)

    def test_global_home_option_uses_custom_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            home = Path(tmp) / "custom-home"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd):
                code, plan_output = self.run_cli("--home", str(home), "plan", "Custom home", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = next(line.split(": ", 1)[1] for line in plan_output.splitlines() if line.startswith("Created run:"))

                self.assertTrue((home / "config.json").exists())
                self.assertTrue((home / "runs" / run_id / "plan.json").exists())
                self.assertFalse((cwd / ".aha").exists())

                code, status = self.run_cli("--home", str(home), "status", run_id)
                self.assertEqual(code, 0)
                self.assertIn("Goal: Custom home", status)

    def test_init_uses_aha_home_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            home = Path(tmp) / "env-home"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd), mock.patch.dict(os.environ, {"AHA_HOME": str(home)}):
                code, output = self.run_cli("init")

                self.assertEqual(code, 0)
                self.assertIn(f"Initialized AHA home: {home}", output)
                self.assertTrue((home / "config.json").exists())
                self.assertFalse((cwd / ".aha").exists())

    def test_init_defaults_to_user_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            user_home = Path(tmp) / "user-home"
            expected_home = user_home / ".aha"
            cwd.mkdir()
            user_home.mkdir()
            with (
                mock.patch("pathlib.Path.cwd", return_value=cwd),
                mock.patch("pathlib.Path.home", return_value=user_home),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                code, output = self.run_cli("init")

                self.assertEqual(code, 0)
                self.assertIn(f"Initialized AHA home: {expected_home}", output)
                self.assertTrue((expected_home / "config.json").exists())
                self.assertFalse((cwd / ".aha").exists())

    def test_init_portable_uses_local_dot_aha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd), mock.patch.dict(os.environ, {}, clear=True):
                code, output = self.run_cli("init", "--portable")

                self.assertEqual(code, 0)
                self.assertIn(f"Initialized AHA home: {cwd / '.aha'}", output)
                self.assertTrue((cwd / ".aha" / "config.json").exists())

    def test_workspace_registry_can_drive_plan_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "launcher"
            home = Path(tmp) / "aha-home"
            workspace = Path(tmp) / "repo"
            cwd.mkdir()
            workspace.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd):
                code, add_output = self.run_cli("--home", str(home), "workspace", "add", str(workspace), "--name", "demo")
                self.assertEqual(code, 0)
                self.assertIn(f"ws-001 demo {workspace}", add_output)
                self.assertTrue((home / "workspaces" / "ws-001.json").exists())

                code, list_output = self.run_cli("--home", str(home), "workspace", "list")
                self.assertEqual(code, 0)
                self.assertIn(f"ws-001 demo {workspace}", list_output)

                code, plan_output = self.run_cli("--home", str(home), "plan", "Workspace plan", "--agents", "1", "--workspace", "ws-001")
                self.assertEqual(code, 0)
                run_id = next(line.split(": ", 1)[1] for line in plan_output.splitlines() if line.startswith("Created run:"))
                plan = read_json(home / "runs" / run_id / "plan.json")
                self.assertEqual(plan["tasks"][0]["workspace_id"], "ws-001")
                self.assertEqual(plan["tasks"][0]["workspace_path"], str(workspace))

    def test_ui_can_start_without_existing_run(self) -> None:
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int) -> None:
            calls.append((root, run_id, host, port, poll_interval))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            calls: list[tuple[Path, str, str, int, int]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli("--home", str(home), "ui", "--host", "127.0.0.1", "--port", "0")

            self.assertEqual(code, 0)
            self.assertTrue((home / "config.json").exists())
            self.assertEqual(calls, [(home, "", "127.0.0.1", 0, 1000)])

    def test_empty_command_defaults_to_ui(self) -> None:
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int) -> None:
            calls.append((root, run_id, host, port, poll_interval))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            calls: list[tuple[Path, str, str, int, int]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli("--home", str(home))

            self.assertEqual(code, 0)
            self.assertTrue((home / "config.json").exists())
            self.assertEqual(calls, [(home, "", "0.0.0.0", 8766, 1000)])

    def test_explicit_tasks_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, output = self.run_cli(
                    "plan",
                    "Goal",
                    "--task",
                    "Task A",
                    "--task",
                    "Task B",
                )
                self.assertEqual(code, 0)
                self.assertIn("Task A", output)
                self.assertIn("Task B", output)

    def test_send_and_watch_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Observe agents", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, send_output = self.run_cli("send", run_id, "task-001", "hello", "agent")
                self.assertEqual(code, 0)
                self.assertIn("hello agent", send_output)

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("Observe agents", watch_output)
                self.assertIn("message main -> task-001: hello agent", watch_output)

    def test_auto_reply_writes_response_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Reply demo", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, _ = self.run_cli("send", run_id, "main", "你好", "--sender", "browser")
                self.assertEqual(code, 0)

                code, reply_output = self.run_cli("auto-reply", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 收到：你好", reply_output)

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("message browser -> main: 你好", watch_output)
                self.assertIn("message main -> browser: 收到：你好", watch_output)

    def test_codex_backend_dry_run_uses_codex_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Codex backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, output = self.run_cli("run", run_id, "--backend", "codex", "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn("aha_cli codex-runner", output)

    def test_claude_backend_dry_run_uses_claude_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Claude backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, output = self.run_cli("run", run_id, "--backend", "claude", "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn("aha_cli claude-runner", output)

    def test_codex_chat_does_not_auto_write_final_or_complete_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "你好", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "真实回复", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 真实回复", output)
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "awaiting_user")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("message task=task-001 main -> browser: 真实回复", watch_output)
                self.assertIn("task_status_changed", watch_output)
                self.assertNotIn("task_result_written", watch_output)

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
        self.assertEqual(code, 0)
        self.assertEqual(len(metrics_events), 1)
        metrics = metrics_events[0]["data"]
        self.assertEqual(metrics["source"], "codex-chat")
        self.assertEqual(metrics["task_id"], "task-001")
        self.assertGreater(metrics["total"]["chars"], 0)
        self.assertGreater(metrics["components"]["status_snapshot"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)

    def test_agent_command_does_not_write_task_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent command", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "main",
                    "/status",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    command_namespace="agent",
                    original_command="/agent status",
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "状态：完成", None)) as run_agent:
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 状态：完成", output)
                self.assertEqual(run_agent.call_args.kwargs["session"]["agent_id"], "main")
                self.assertIsNone(run_agent.call_args.kwargs["session"].get("backend_session_id"))

                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["result"], "")
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_agent_command_prompt_does_not_replay_task_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent command prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "browser",
                    "状态：已完成，旧任务结果",
                    sender="main",
                    task_id="task-001",
                    role="main",
                    from_agent="main",
                    to_agent="browser",
                )
                command = append_message(
                    root,
                    run_id,
                    "main",
                    "/status",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    command_namespace="agent",
                    original_command="/agent status",
                )

                prompt = chat_prompt(root, run_id, "main", command, "")
                self.assertIn("The user sent an agent command", prompt)
                self.assertIn("/status: report this agent's runtime/session metadata only", prompt)
                self.assertNotIn("Recent events:", prompt)
                self.assertNotIn("旧任务结果", prompt)

    def test_prompts_include_commit_ownership_policy(self) -> None:
        assignment_prompt = task_assignment_prompt(
            {
                "id": "task-001",
                "title": "Commit work",
                "workspace_path": "/tmp/project",
                "max_sub_agents": 2,
                "delegation_policy": "auto",
                "preferred_backend": "codex",
            }
        )
        self.assertIn("Commit ownership policy:", assignment_prompt)
        self.assertIn("route it to that sub-agent with `route_to_agent`", assignment_prompt)
        self.assertIn("Never ask a sub-agent to commit files outside its assignment", assignment_prompt)
        self.assertIn("Commit message policy:", assignment_prompt)
        self.assertIn("AHA-Task: task-001", assignment_prompt)
        self.assertIn("return ONLY one JSON object", assignment_prompt)
        self.assertIn('"actions"', assignment_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Commit routing", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_runtime(root, run_id, "task-001", "sub-001", assignment="UI routing changes")
                main_message = append_message(root, run_id, "main", "提交代码", sender="browser", task_id="task-001", role="main")
                main_prompt = chat_prompt(root, run_id, "main", main_message, "")

                self.assertIn("Commit ownership policy:", main_prompt)
                self.assertIn("Route format:", main_prompt)
                self.assertIn('"type": "route_to_agent"', main_prompt)
                self.assertIn('"type": "record_task_update"', main_prompt)
                self.assertNotIn("Return a JSON action `route_to_agent`", main_prompt)
                self.assertIn("route commit work to the sub-agent that owns the changed scope", main_prompt)
                self.assertIn("Commit message policy:", main_prompt)
                self.assertIn("AHA-Task: task-001", main_prompt)
                self.assertIn("AHA-Agent: main", main_prompt)
                self.assertIn("aha commit --type <type>", main_prompt)
                self.assertIn("UI routing changes", main_prompt)

                sub_message = append_message(root, run_id, "sub-001", "提交你负责的部分", sender="main", task_id="task-001", role="sub")
                sub_prompt = chat_prompt(root, run_id, "sub-001", sub_message, "")

                self.assertIn("commit only files covered by your `assignment` / `created_reason`", sub_prompt)
                self.assertIn("report back to `task-main`", sub_prompt)
                self.assertIn("AHA-Task: task-001", sub_prompt)
                self.assertIn("AHA-Agent: sub-001", sub_prompt)

    def test_commit_policy_formats_validates_and_prints_dry_run_messages(self) -> None:
        message = format_commit_message("feat", "add lazy loading", "task-001", "main", scope="web", aha_scope="lazy-log")

        self.assertEqual(validate_commit_message(message), [])
        self.assertIn("feat(web): add lazy loading", message)
        self.assertIn("AHA-Task: task-001", message)
        self.assertIn("AHA-Agent: main", message)
        self.assertIn("AHA-Scope: lazy-log", message)
        self.assertTrue(validate_commit_message("update stuff\n\nAHA-Task: task-001\n"))

        code, output = self.run_cli(
            "commit",
            "--type",
            "fix",
            "--scope",
            "web",
            "--summary",
            "keep logs scroll stable",
            "--task-id",
            "task-005",
            "--agent",
            "main",
            "--aha-scope",
            "log-scroll",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("fix(web): keep logs scroll stable", output)
        self.assertIn("AHA-Task: task-005", output)
        self.assertIn("AHA-Agent: main", output)
        with tempfile.TemporaryDirectory() as tmp:
            message_file = Path(tmp) / "COMMIT_EDITMSG"
            message_file.write_text(message, encoding="utf-8")
            code, output = self.run_cli("commit-check", str(message_file))
        self.assertEqual(code, 0)
        self.assertIn("Commit message OK", output)

    def test_raw_result_file_without_final_metadata_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale result", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                path = run_dir(root, run_id) / "results/task-001.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old auto result\n", encoding="utf-8")

                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

    def test_finalize_policy_updates_existing_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Finalize chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                write_task_result(root, run_id, "task-001", "old final")
                set_task_status(root, run_id, "task-001", "completed", 0)
                append_message(
                    root,
                    run_id,
                    "main",
                    "Generate the Final",
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    result_policy="finalize",
                    original_command="/aha finalize",
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "new final", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> aha: new final", output)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"].strip(), "new final")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "completed")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["exit_code"], 0)

    def test_final_driven_completion_reopen_preserves_round_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final loop", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                append_message(root, run_id, "main", "先做第一轮", sender="browser", task_id="task-001", role="main")
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "第一轮已处理", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "awaiting_user")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "agent_finished" for event in events))
                self.assertFalse(any(event["type"] == "task_completed" for event in events))

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Finalization requested", payload["message"]["message"])

                first_final = "第一轮 Final"
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, first_final, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")

                self.assertEqual(code, 0)
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(detail["result"].strip(), first_final)
                first_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(first_rounds), 1)
                self.assertEqual(first_rounds[0]["round_id"], "round-001")
                self.assertEqual(first_rounds[0]["status"], "finalized")
                self.assertEqual((run_dir(root, run_id) / first_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha reopen",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("reopened", payload["message"]["message"])
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "awaiting_user")
                self.assertEqual(detail["result"].strip(), first_final)
                reopened_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(reopened_rounds), 2)
                self.assertEqual(reopened_rounds[0]["status"], "finalized")
                self.assertEqual(reopened_rounds[1]["round_id"], "round-002")
                self.assertEqual(reopened_rounds[1]["status"], "active")
                self.assertEqual(reopened_rounds[1]["reopened_from_round_id"], "round-001")
                self.assertEqual((run_dir(root, run_id) / reopened_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)

                append_message(root, run_id, "main", "继续第二轮", sender="browser", task_id="task-001", role="main")
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "第二轮已处理", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")
                self.assertEqual(code, 0)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "awaiting_user")

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Finalization requested", payload["message"]["message"])

                second_final = "第二轮 Final"
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, second_final, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")

                self.assertEqual(code, 0)
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(detail["result"].strip(), second_final)
                final_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(final_rounds), 2)
                self.assertEqual(final_rounds[0]["status"], "finalized")
                self.assertEqual(final_rounds[1]["status"], "finalized")
                self.assertEqual((run_dir(root, run_id) / final_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)
                self.assertEqual((run_dir(root, run_id) / final_rounds[1]["final_path"]).read_text(encoding="utf-8").strip(), second_final)

    def test_final_api_returns_task_overview_and_preserves_round_finals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final overview", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                first_final = "#### Final\n## 阶段总览\n第一轮 raw final 正文只应保留在 round 快照文件中"
                second_final = "#### Final\n## 阶段总览\n第二轮 raw final 正文只应保留在 round 快照文件中"

                write_task_result(root, run_id, "task-001", first_final)
                complete_task(root, run_id, "task-001", 0)
                first_round = list_task_lifecycle_rounds(root, run_id, "task-001")[0]
                first_round_final = run_dir(root, run_id) / first_round["final_path"]

                reopen_task(root, run_id, "task-001")
                write_task_result(root, run_id, "task-001", second_final)
                complete_task(root, run_id, "task-001", 0)
                second_round = list_task_lifecycle_rounds(root, run_id, "task-001")[1]
                second_round_final = run_dir(root, run_id) / second_round["final_path"]
                reopen_task(root, run_id, "task-001")

                snapshot = task_final_snapshot(root, run_id, "task-001")
                api_response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/final"))
                api_body = json_response_body(api_response)
                overview_text = (run_dir(root, run_id) / "results/task-001.md").read_text(encoding="utf-8")

                self.assertTrue(api_response.startswith(b"HTTP/1.1 200 OK"))
                self.assertEqual(api_body["result"], overview_text)
                self.assertNotEqual(api_body["result"].strip(), second_final)
                self.assertIn("task-001", api_body["result"])
                self.assertIn(task["title"], api_body["result"])
                for expected in ("round-001", "round-002", "round-003"):
                    self.assertIn(expected, api_body["result"])
                self.assertIn("Raw final:", api_body["result"])
                self.assertNotIn(first_final, api_body["result"])
                self.assertNotIn(second_final, api_body["result"])
                self.assertNotIn("#### Final", api_body["result"])
                self.assertNotIn("## 阶段总览", api_body["result"])
                self.assertIn(first_round["final_path"], api_body["result"])
                self.assertIn(second_round["final_path"], api_body["result"])
                self.assertRegex(api_body["result"].lower(), r"reopen|reopened|复开|重开|重新打开|继续")
                self.assertEqual(first_round_final.read_text(encoding="utf-8").strip(), first_final)
                self.assertEqual(second_round_final.read_text(encoding="utf-8").strip(), second_final)
                self.assertEqual([item["round_id"] for item in snapshot["finals"]], ["round-001", "round-002"])

    def test_record_task_update_uses_current_lifecycle_round_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Journal lifecycle", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                write_task_result(root, run_id, "task-001", "第一轮 Final")
                complete_task(root, run_id, "task-001", 0)
                reopen_task(root, run_id, "task-001")

                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "第二轮修复 Round/Journal 关联",
                                "changed_files": ["src/aha_cli/store/filesystem.py"],
                            }
                        ],
                        "response": "已记录",
                    },
                    ensure_ascii=False,
                )

                executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [{"type": "record_task_update", "round_id": "round-002"}])
                journal = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(journal[0]["round_id"], "round-002")
                self.assertEqual(journal[0]["round_sequence"], 2)
                self.assertEqual(journal[0]["sequence"], 2)
                self.assertEqual(journal[0]["journal_id"], "journal-001")
                self.assertEqual(journal[0]["journal_sequence"], 1)
                journal_text = (run_dir(root, run_id) / "results/task-001.md").read_text(encoding="utf-8")
                self.assertIn("轮次：`round-002`", journal_text)
                self.assertNotIn("轮次：`round-001`", journal_text)
                prompt = finalization_prompt("task-001", "Journal lifecycle", journal)
                self.assertIn("round-002", prompt)
                self.assertNotIn("round-001", prompt)
                final = task_final_snapshot(root, run_id, "task-001")
                self.assertIn("round-001", final["result"])
                self.assertEqual(final["finals"][0]["round_id"], "round-001")
                self.assertEqual((run_dir(root, run_id) / final["finals"][0]["final_path"]).read_text(encoding="utf-8").strip(), "第一轮 Final")

    def test_final_summary_stops_task_scoped_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final cleanup", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                append_message(
                    root,
                    run_id,
                    "main",
                    "Produce final summary",
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    result_policy="finalize",
                    reply_target="browser",
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "final summary", None)),
                    mock.patch("aha_cli.services.chat.stop_task_backends", return_value=[]) as stop_backends,
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                stop_backends.assert_called_once()
                self.assertEqual(stop_backends.call_args.args[:3], (root / ".aha", run_id, "task-001"))
                self.assertIn("exclude_pid", stop_backends.call_args.kwargs)
                mark_stopped.assert_called_once()
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "completed")

    def test_codex_chat_executes_spawn_sub_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Spawn sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = '{"complexity":"medium","actions":[{"type":"spawn_sub","title":"Inspect one slice","backend":"stub","reason":"parallel research"}],"response":"delegating"}'

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("delegating", output)

                code, agents = self.run_cli("agent", "list", run_id, "task-001")
                self.assertEqual(code, 0)
                self.assertIn("sub-001 role=sub backend=stub", agents)
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "running")
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertIn("等待子 agent 完成", browser_messages[-1]["message"])

    def test_action_parser_ignores_embedded_json_examples(self) -> None:
        reply = textwrap.dedent(
            """\
            我建议使用这个格式：

            ```json
            {"actions":[{"type":"route_to_agent","agent_id":"...","message":"..."}],"response":"..."}
            ```
            """
        ).strip()

        self.assertIsNone(extract_action_payload(reply))
        self.assertEqual(action_response_text(reply), reply)

    def test_invalid_top_level_action_schema_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Bad route schema", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                reply = json.dumps(
                    {
                        "action": "route_to_agent",
                        "agent_id": "sub-001",
                        "message": "This should not route",
                        "response": "sent",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [])
                self.assertIn("Invalid AHA action schema", action_response_text(reply))
                start_backend_mock.assert_not_called()
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages, [])
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "invalid_action_schema" for event in events))

    def test_execute_actions_records_task_update_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Journal task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "修复 action schema 误解析",
                                "changed_files": ["src/aha_cli/services/orchestrator.py"],
                                "verification": ["tests OK"],
                                "risks": [],
                            }
                        ],
                        "response": "已记录",
                    },
                    ensure_ascii=False,
                )

                executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [{"type": "record_task_update", "round_id": "round-001"}])
                rounds = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(rounds[0]["summary"], "修复 action schema 误解析")
                self.assertEqual(rounds[0]["changed_files"], ["src/aha_cli/services/orchestrator.py"])
                snapshot = task_final_snapshot(root, run_id, "task-001")
                self.assertEqual(snapshot["result_meta"]["policy"], "journal")
                self.assertIn("修复 action schema 误解析", snapshot["result"])
                self.assertIn("## 任务轮次", snapshot["result"])
                self.assertIn("1. `round-001` 修复 action schema 误解析", snapshot["result"])
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "task_round_recorded" for event in events))

    def test_codex_chat_record_task_update_does_not_wait_for_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Record round", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "完成了一个小修复", sender="browser", task_id="task-001", role="main")
                reply = json.dumps(
                    {
                        "actions": [{"type": "record_task_update", "summary": "完成小修复"}],
                        "response": "完成",
                    },
                    ensure_ascii=False,
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertIn("完成", output)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertNotIn("等待子 agent 完成", "\n".join(item["message"] for item in browser_messages))
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "awaiting_user")

    def test_finalization_prompt_includes_task_journal(self) -> None:
        prompt = finalization_prompt(
            "task-001",
            "Journal task",
            [
                {
                    "round_id": "round-001",
                    "trigger": "main_turn",
                    "summary": "完成小修复",
                    "changed_files": ["src/app.py"],
                    "verification": ["unit tests"],
                    "risks": [],
                }
            ],
        )

        self.assertIn("Task journal (chronological ordered list):", prompt)
        self.assertIn("1. 完成小修复", prompt)
        self.assertIn("round-001", prompt)
        self.assertIn("完成小修复", prompt)
        self.assertIn("Use the Task journal as the primary source", prompt)
        self.assertIn("chronological ordered list", prompt)

    def test_aha_checkpoint_records_task_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Checkpoint task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                handled, forwarded, response = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha checkpoint 完成第一轮清理",
                    "task-001",
                )

                self.assertTrue(handled)
                self.assertIsNone(forwarded)
                self.assertIn("Checkpoint recorded", response["message"]["message"])
                rounds = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(rounds[0]["trigger"], "manual")
                self.assertEqual(rounds[0]["summary"], "完成第一轮清理")

    def test_codex_chat_autostarts_codex_sub_agent_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Spawn codex sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = '{"complexity":"complex","actions":[{"type":"spawn_sub","title":"Inspect one slice","backend":"codex","reason":"parallel research"}],"response":"delegating"}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                start_backend.assert_called_once()
                self.assertEqual(start_backend.call_args.args[:3], (root / ".aha", run_id, "sub-001"))
                self.assertTrue(start_backend.call_args.kwargs["from_start"])
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(messages[-1]["message"], "Inspect one slice")
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(sub_agent["assignment"], "Inspect one slice")
                self.assertTrue(detail["task"]["coordination"]["followup_started_at"])

    def test_main_routes_followup_to_responsible_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Owned follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                mark_task_coordination(
                    root,
                    run_id,
                    "task-001",
                    final_summary_requested_at="2026-05-15T00:00:00+00:00",
                    final_summary_completed_at="2026-05-15T00:01:00+00:00",
                )
                set_task_status(root, run_id, "task-001", "completed", 0)
                reopen_task(root, run_id, "task-001")
                append_message(root, run_id, "main", "这个范围继续调整", sender="browser", task_id="task-001", role="main")
                reply = json.dumps(
                    {
                        "complexity": "simple",
                        "actions": [
                            {
                                "type": "route_to_agent",
                                "agent_id": "sub-001",
                                "message": "请继续调整你负责的范围",
                                "reason": "sub-001 owns this scope",
                            }
                        ],
                        "response": "已转给 sub-001 处理。",
                    }
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 已转给 sub-001 处理。", output)
                self.assertNotIn("route_to_agent", output)
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages[-1]["message"], "请继续调整你负责的范围")
                self.assertEqual(sub_messages[-1]["coordination"], "routed_by_main")
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "running")
                self.assertIsNone(detail["task"]["exit_code"])
                self.assertEqual(detail["task"]["coordination"]["final_summary_requested_at"], "")
                self.assertEqual(detail["task"]["coordination"]["final_summary_completed_at"], "")
                self.assertTrue(detail["task"]["coordination"]["followup_started_at"])
                start_backend.assert_called_once()

                report = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub follow-up done")
                self.assertTrue(report["round_summary_requested"])
                self.assertFalse(report["final_requested"])

    def test_sub_agent_skips_completed_task_even_with_old_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "pending")
                set_task_status(root, run_id, "task-001", "completed", 0)
                mark_task_coordination(root, run_id, "task-001", followup_started_at="2026-05-15T00:00:00+00:00")
                append_message(root, run_id, "sub-001", "继续处理你负责的范围", sender="main", task_id="task-001", role="sub")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, output = self.run_cli(
                        "codex-chat",
                        run_id,
                        "sub-001",
                        "--task-id",
                        "task-001",
                        "--sender",
                        "sub-001",
                        "--from-start",
                        "--once",
                )

                self.assertEqual(code, 0)
                self.assertNotIn("sub-001 -> main", output)
                run_agent.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(sub_agent["status"], "pending")
                self.assertNotIn("final_summary_requested_at", detail["task"].get("coordination") or {})
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "agent_message_skipped" for event in events))

    def test_sub_agent_reports_wait_then_request_main_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub reports", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_task_status(root, run_id, "task-001", "running")

                first = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub-001 done")
                self.assertTrue(first["handled"])
                self.assertFalse(first.get("round_summary_requested"))
                detail = task_snapshot(root, run_id, "task-001")
                statuses = {agent["id"]: agent["status"] for agent in detail["task"]["agents"]}
                self.assertEqual(statuses["sub-001"], "completed")
                self.assertEqual(statuses["sub-002"], "pending")

                second = record_sub_agent_report(root, run_id, "task-001", "sub-002", "sub-002 done")
                self.assertTrue(second["round_summary_requested"])
                self.assertFalse(second["final_requested"])
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                self.assertEqual(main_messages[-1]["sender"], "aha")
                self.assertEqual(main_messages[-1]["coordination"], "subagents_complete")
                self.assertNotIn("result_policy", main_messages[-1])
                self.assertEqual(main_messages[-1]["reply_target"], "browser")
                self.assertIn("round summary", main_messages[-1]["message"])

    def test_coordination_watchdog_recovers_stopped_pending_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Watchdog", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_task_status(root, run_id, "task-001", "running")

                with mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend:
                    actions = monitor_task_coordination(root, run_id)

                self.assertIn({"type": "agent_recovered", "task_id": "task-001", "agent_id": "sub-001"}, actions)
                start_backend.assert_called_once()
                self.assertFalse(start_backend.call_args.kwargs["from_start"])
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(sub_agent["recovery_attempts"], 1)

    def test_main_does_not_reply_to_sub_agent_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No loops", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                append_message(root, run_id, "main", "done", sender="sub-001", task_id="task-001", role="sub", from_agent="sub-001", to_agent="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                run_agent.assert_not_called()
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages, [])

    def test_sub_agent_skips_messages_after_final_summary_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                append_message(root, run_id, "sub-001", "no-op ack", sender="main", task_id="task-001", role="sub", from_agent="main", to_agent="sub-001")

                mark_task_coordination(root, run_id, "task-001", final_summary_requested_at="2026-05-15T00:00:00+00:00")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "sub-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                run_agent.assert_not_called()

    def test_task_dashboard_and_metadata_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Task UI", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                payload = append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                self.assertEqual(payload["task_id"], "task-001")
                self.assertEqual(payload["role"], "main")

                detail = task_snapshot(root, run_id, "task-001")
                self.assertIn("prompt", detail)
                self.assertEqual(detail["task"]["id"], "task-001")

                html = task_dashboard_html(run_id, 1000)
                self.assertIn("task-list", html)
                self.assertIn("agent-target", html)
                self.assertIn("workspace-select", html)
                self.assertIn("workspace-custom", html)
                self.assertIn("show-hidden", html)
                self.assertIn('id="task-model"', html)
                self.assertIn('id="task-sandbox"', html)
                self.assertIn('id="task-approval"', html)
                self.assertIn('id="task-http-proxy"', html)
                self.assertIn('id="task-https-proxy"', html)
                self.assertIn('id="task-no-proxy"', html)
                self.assertIn('id="task-proxy-editor"', html)
                self.assertIn("selected-task-meta", html)
                self.assertIn("selected-agent-info", html)
                self.assertIn("backend-status", html)
                self.assertIn("pending-messages", html)
                self.assertIn("command-menu", html)
                self.assertIn("conversation-filters", html)
                self.assertIn('data-tab="final"', html)

    def test_package_onebin_builds_executable_with_ui_static(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "aha"
            code, output = self.run_cli("package", "onebin", "--output", str(artifact))
            self.assertEqual(code, 0, output)
            self.assertTrue(artifact.is_file())
            self.assertTrue(os.access(artifact, os.X_OK))

            help_run = subprocess.run([str(artifact), "--help"], capture_output=True, text=True, timeout=10)
            self.assertEqual(help_run.returncode, 0, help_run.stderr)
            self.assertIn("Agent-help-agent", help_run.stdout)

            aha_home = root / ".aha"
            workspace = root / "workspace"
            workspace.mkdir()
            init_run = subprocess.run(
                [str(artifact), "--home", str(aha_home), "init", "--backend", "stub"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(init_run.returncode, 0, init_run.stderr)
            plan_run = subprocess.run(
                [str(artifact), "--home", str(aha_home), "plan", "One-bin run", "--agents", "1", "--workspace-path", str(workspace)],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(plan_run.returncode, 0, plan_run.stderr)
            run_id = plan_run.stdout.splitlines()[0].split(": ", 1)[1]

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            proc = subprocess.Popen(
                [str(artifact), "--home", str(aha_home), "ui", run_id, "--host", "127.0.0.1", "--port", str(port)],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                html = ""
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                            html = response.read().decode("utf-8")
                        break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(0.1)
                if not html:
                    stdout, stderr = proc.communicate(timeout=1)
                    self.fail(f"one-bin UI did not start\nstdout={stdout}\nstderr={stderr}")
                self.assertIn('id="run-export"', html)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.js", timeout=1) as response:
                    script = response.read().decode("utf-8")
                self.assertIn("runExportEl", script)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=5)
                else:
                    proc.communicate(timeout=1)

    def test_web_status_snapshot_includes_agent_backend_process_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend badges", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                mark_task_coordination(root, run_id, "task-001", followup_started_at="2026-05-15T00:00:00+00:00")

                def fake_backend_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {
                        "target": target,
                        "task_id": task_id,
                        "status": "running" if target == "main" else "stopped",
                        "pid": 1234 if target == "main" else None,
                        "last_reply_at": "2026-05-15T00:00:00+00:00" if target == "main" else None,
                    }

                with mock.patch("aha_cli.web.server.backend_status", side_effect=fake_backend_status):
                    snapshot = web_status_snapshot(root, run_id)

        self.assertEqual(snapshot["tasks"][0]["coordination"]["followup_started_at"], "2026-05-15T00:00:00+00:00")
        agents = {agent["id"]: agent for agent in snapshot["tasks"][0]["agents"]}
        self.assertEqual(snapshot["tasks"][0]["activity_status"], "idle")
        self.assertEqual(agents["main"]["backend_process_status"], "running")
        self.assertEqual(agents["main"]["backend_process_pid"], 1234)
        self.assertEqual(agents["sub-001"]["backend_process_status"], "stopped")
        self.assertIsNone(agents["sub-001"]["backend_process_pid"])

    def test_web_status_snapshot_recovers_stale_running_agent_after_backend_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Interrupted service restart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped", "pid": None}):
                    snapshot = web_status_snapshot(root, run_id)
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        task = snapshot["tasks"][0]
        agent = task["agents"][0]
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(task["current_status"], "awaiting_user")
        self.assertEqual(task["display_status"], "awaiting_user")
        self.assertEqual(task["activity_status"], "idle")
        self.assertEqual(agent["status"], "interrupted")
        self.assertEqual(agent["backend_process_status"], "stopped")

        self.assertEqual(persisted["status"], "awaiting_user")
        self.assertEqual(persisted["agents"][0]["status"], "interrupted")
        self.assertIn("agent_status_recovered", event_log)

    def test_stale_recovery_does_not_reopen_terminal_task_from_old_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final race", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                stale_task = status_snapshot(root, run_id)["tasks"][0]
                stale_agent = stale_task["agents"][0]

                set_task_status(root, run_id, "task-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                recovered = recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_agent,
                    {"status": "stopped", "pid": None},
                )
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertFalse(recovered)
        self.assertEqual(persisted["status"], "completed")
        self.assertEqual(persisted["agents"][0]["status"], "completed")
        self.assertNotIn("agent_status_recovered", event_log)

    def test_web_status_snapshot_keeps_outcome_during_active_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Follow-up state", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.server.backend_status", return_value={"status": "busy", "pid": 1234}):
                    snapshot = web_status_snapshot(root, run_id)

        task = snapshot["tasks"][0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["current_status"], "completed")
        self.assertEqual(task["outcome_status"], "completed")
        self.assertEqual(task["display_status"], "completed")
        self.assertEqual(task["activity_status"], "busy")

    def test_web_send_autostarts_stopped_task_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Web follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                append_message(root, run_id, "main", "old already-seen message", sender="browser", task_id="task-001", role="main")

                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertFalse(offset_file.exists())

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "new follow-up",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["backend"]["status"], "running")
                start_backend.assert_called_once()
                self.assertFalse(start_backend.call_args.kwargs["from_start"])
                self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
                offset = json.loads(offset_file.read_text(encoding="utf-8"))["offset"]
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), offset)
                self.assertEqual([item["message"] for item in messages], ["new follow-up"])

    def test_web_send_autostarts_claude_task_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude web follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_agent_runtime(root, run_id, "task-001", "main", backend="claude")
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "sender": "browser",
                            "message": "new follow-up",
                        },
                    )

        self.assertTrue(result["ok"])
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.kwargs["backend"], "claude")

    def test_web_send_blocks_completed_task_until_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Locked task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                payload = {
                    "target": "main",
                    "role": "main",
                    "task_id": "task-001",
                    "from_agent": "browser",
                    "to_agent": "main",
                    "sender": "browser",
                    "message": "should be blocked",
                }
                with self.assertRaisesRegex(ValueError, "use /aha reopen"):
                    handle_send_payload(root, run_id, payload)

                reopened = handle_send_payload(root, run_id, {**payload, "message": "/aha reopen"})
                self.assertTrue(reopened["ok"])
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["coordination"]["final_summary_requested_at"], "")

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    sent = handle_send_payload(root, run_id, {**payload, "message": "follow-up"})

                self.assertTrue(sent["ok"])
                start_backend.assert_called_once()

    def test_aha_interrupt_stops_backend_and_marks_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Interrupt turn", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                append_message(root, run_id, "main", "in-flight", sender="browser", task_id="task-001", role="main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                save_chat_offset(offset_file, 0)

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "busy", "pid": 1234}),
                    mock.patch("aha_cli.web.server.stop_backend", return_value={"status": "stopped", "pid": None, "target": "main"}) as stop_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "/aha interrupt",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertTrue(result["interrupt"]["interrupted"])
                stop_backend.assert_called_once()
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["agents"][0]["status"], "interrupted")
                self.assertIsNotNone(detail["agents"][0]["finished_at"])
                self.assertEqual(json.loads(offset_file.read_text(encoding="utf-8"))["offset"], inbox_path(root, run_id, "main").stat().st_size)

    def test_aha_interrupt_ignores_idle_backend_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Idle listener", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "running", "pid": 1234}),
                    mock.patch("aha_cli.web.server.stop_backend") as stop_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "/aha interrupt",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertFalse(result["interrupt"]["interrupted"])
                self.assertEqual(result["interrupt"]["reason"], "not_busy")
                stop_backend.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["agents"][0]["status"], "completed")

    def test_task_action_resume_alias_reopens_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Resume alias", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/resume", method="POST"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["status"], "awaiting_user")

    def test_web_task_creation_autostarts_dispatched_main_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task create autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend:
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/tasks",
                            method="POST",
                            payload={
                                "title": "Autostart task",
                                "backend": "codex",
                                "sandbox": "danger-full-access",
                                "approval": "never",
                                "dispatch": True,
                            },
                        )
                    )
                    body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"]["status"], "running")
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["task_id"], body["task"]["id"])
        self.assertTrue(start_backend.call_args.kwargs["from_start"])

    def test_conversation_events_page_filters_and_pages_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": "one"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "two"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "sub-001", "text": "sub"})
            append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})

            latest = conversation_events_page(root, run_id, "task-001", "main", limit=1)
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "new realtime"})
            realtime, _ = iter_jsonl_from(event_path(root, run_id), latest["after_offset"])
            older = conversation_events_page(root, run_id, "task-001", "main", limit=1, before=latest["next_before_offset"])

        self.assertEqual(latest["count"], 1)
        self.assertTrue(latest["has_more"])
        self.assertEqual(latest["events"][0]["data"]["text"], "two")
        self.assertEqual(realtime[0]["data"]["text"], "new realtime")
        self.assertFalse(older["has_more"])
        self.assertEqual(older["events"][0]["data"]["message"], "one")

    def test_conversation_events_api_hides_action_envelope_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation action envelope", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                user_facing_response = "只展示投影后的 response"
                action_envelope = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "raw envelope should stay out of timeline",
                                "changed_files": [],
                                "verification": [],
                                "risks": [],
                            }
                        ],
                        "response": user_facing_response,
                    },
                    ensure_ascii=False,
                )
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": action_envelope})
                append_event(
                    root,
                    run_id,
                    "message",
                    {"task_id": "task-001", "sender": "main", "target": "browser", "message": user_facing_response},
                )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        timeline_texts = [str(event["data"].get("text") or event["data"].get("message") or "") for event in body["events"]]
        self.assertEqual(timeline_texts, [user_facing_response])
        self.assertNotIn(action_envelope, timeline_texts)
        self.assertFalse(any('"actions"' in text and '"response"' in text for text in timeline_texts))

    def test_conversation_events_api_restores_latest_turn_metrics_outside_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_started", {"task_id": "task-001", "target": "main", "sender": "browser"})
                append_event(
                    root,
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"chars": 1234, "bytes": 1234, "lines": 12},
                        "components": {"status_snapshot": {"chars": 1000, "bytes": 1000, "lines": 1}},
                    },
                )
                append_event(root, run_id, "agent_thread", {"task_id": "task-001", "target": "main", "thread_id": "thread-1"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 10}})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                for index in range(10):
                    append_event(
                        root,
                        run_id,
                        "agent_command_finished",
                        {"task_id": "task-001", "target": "main", "command": f"cmd-{index}", "exit_code": 0},
                    )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=5"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertNotIn("agent_prompt_metrics", [event["type"] for event in body["events"]])
        turn_event_types = [event["type"] for event in body["turn_events"]]
        self.assertEqual(turn_event_types, ["agent_started", "agent_prompt_metrics", "agent_thread", "agent_usage", "agent_finished"])
        metrics = next(event for event in body["turn_events"] if event["type"] == "agent_prompt_metrics")
        self.assertEqual(metrics["data"]["total"]["chars"], 1234)

    def test_backend_session_jsonl_info_analyzes_aha_prompt_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "session-analysis-1"
            session_file = home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl"
            full_prompt = textwrap.dedent(
                """\
                You are connected to AHA as the real backend agent.

                Current status:
                {'task': 'task-001'}

                User message from browser at 2026-05-21T00:00:00+00:00:
                first request
                """
            )
            delta_prompt = textwrap.dedent(
                """\
                You are connected to AHA as the real backend agent.

                Current delta status:
                {'task': 'task-001'}

                User message from browser at 2026-05-21T00:01:00+00:00:
                second request
                """
            )
            append_jsonl(session_file, {"type": "session_meta", "payload": {"id": session_id}})
            append_jsonl(
                session_file,
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": full_prompt}]}},
            )
            append_jsonl(session_file, {"type": "event_msg", "payload": {"type": "user_message", "message": full_prompt}})
            append_jsonl(
                session_file,
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": delta_prompt}]}},
            )
            append_jsonl(session_file, {"type": "response_item", "payload": {"type": "function_call_output", "output": "tool-output-text"}})
            append_jsonl(
                session_file,
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "reply"}]}},
            )

            with mock.patch("aha_cli.web.server.Path.home", return_value=home):
                info = backend_session_jsonl_info({"backend": "codex", "backend_session_id": session_id})

        analysis = info["analysis"]
        self.assertTrue(info["exists"])
        self.assertGreater(info["size_bytes"], 0)
        self.assertEqual(analysis["line_count"], 6)
        self.assertEqual(analysis["aha_prompt_counts"]["full"], 1)
        self.assertEqual(analysis["aha_prompt_counts"]["sticky_delta"], 1)
        self.assertEqual(analysis["event_msg_prompt_mirror_counts"]["full"], 1)
        self.assertEqual(analysis["aha_prompt_total_count"], 2)
        self.assertEqual(analysis["latest_prompt_mode"], "sticky_delta")
        self.assertGreater(analysis["tool_output_chars"], 0)
        self.assertGreater(analysis["assistant_message_chars"], 0)

    def test_backend_session_jsonl_info_analyzes_claude_session_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "claude-session-analysis-1"
            session_file = home / ".claude" / "projects" / "project-a" / f"{session_id}.jsonl"
            full_prompt = textwrap.dedent(
                """\
                You are connected to AHA as the real backend agent.

                Current status:
                {'task': 'task-001'}

                User message from browser at 2026-05-21T00:00:00+00:00:
                first request
                """
            )
            append_jsonl(session_file, {"type": "queue-operation", "operation": "enqueue", "sessionId": session_id, "content": full_prompt})
            append_jsonl(session_file, {"type": "user", "sessionId": session_id, "message": {"role": "user", "content": full_prompt}})
            append_jsonl(
                session_file,
                {"type": "assistant", "sessionId": session_id, "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}]}},
            )
            append_jsonl(
                session_file,
                {
                    "type": "user",
                    "sessionId": session_id,
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool-output"}]},
                },
            )

            with mock.patch("aha_cli.web.server.Path.home", return_value=home):
                info = backend_session_jsonl_info({"backend": "claude", "backend_session_id": session_id})

        analysis = info["analysis"]
        self.assertTrue(info["exists"])
        self.assertEqual(analysis["backend"], "claude")
        self.assertEqual(analysis["type_counts"]["user"], 2)
        self.assertEqual(analysis["aha_prompt_counts"]["full"], 1)
        self.assertEqual(analysis["event_msg_prompt_mirror_counts"]["full"], 1)
        self.assertEqual(analysis["response_item_counts"]["message:user"], 1)
        self.assertEqual(analysis["response_item_counts"]["message:assistant"], 1)
        self.assertEqual(analysis["response_item_counts"]["tool_result:user"], 1)
        self.assertGreater(analysis["tool_output_chars"], 0)
        self.assertGreater(analysis["assistant_message_chars"], 0)

    def test_compact_reset_archives_backend_session_and_injects_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            root = Path(tmp)
            home = Path(home_tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Compact reset", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_id = "compact-reset-session-1"
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = session_id
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl",
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "old prompt"}]}},
                )
                append_message(root, run_id, "main", "previous request", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.session_compact.Path.home", return_value=home):
                    payload = compact_reset_backend_session(root, run_id, "task-001", "main", reason="manual")

                updated = read_json(session_file)
                summary_exists = (run_dir(root, run_id) / payload["summary_path"]).exists()
                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "next request", "task_id": "task-001", "role": "main"},
                    "prefix",
                )

        self.assertEqual(payload["old_backend_session_id"], session_id)
        self.assertIsNone(updated["backend_session_id"])
        self.assertEqual(updated["history_backend_sessions"][0]["backend_session_id"], session_id)
        self.assertEqual(updated["compact_summary"]["archived_backend_session_id"], session_id)
        self.assertTrue(summary_exists)
        self.assertIn("Backend compact summary from previous session", prompt)
        self.assertIn("previous request", prompt)

    def test_compact_reset_api_uses_selected_agent_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            root = Path(tmp)
            home = Path(home_tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Compact reset API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_id = "compact-reset-api-session-1"
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = session_id
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl",
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "old prompt"}]}},
                )

                with mock.patch("aha_cli.services.session_compact.Path.home", return_value=home):
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/task/task-001/session/compact-reset",
                            method="POST",
                            payload={"target": "main", "reason": "manual", "restart": False},
                        )
                    )
                body = json_response_body(response)
                updated = read_json(session_file)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["compact_reset"]["old_backend_session_id"], session_id)
        self.assertIsNone(updated["backend_session_id"])
        self.assertEqual(updated["history_backend_sessions"][0]["backend_session_id"], session_id)

    def test_events_api_replays_from_saved_offset_after_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Replay events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                initial = json_response_body(asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=-1")))
                last_event_id = initial["offset"]

                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "missed-1"})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})

                first_page = json_response_body(
                    asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=1"))
                )
                replay = json_response_body(asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=10")))

            self.assertEqual(first_page["events"][0]["data"]["text"], "missed-1")
            self.assertTrue(first_page["has_more"])
            self.assertGreater(first_page["offset"], last_event_id)
            self.assertEqual([event["type"] for event in replay["events"]], ["agent_message", "agent_finished"])
            self.assertEqual(replay["events"][1]["data"]["exit_code"], 0)
            self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_reverse_jsonl_reader_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            for index in range(5):
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": f"line-{index}-" + ("x" * 40)})

            path = event_path(root, run_id)
            newest = list(iter_jsonl_reverse(path, chunk_size=32))
            older = list(iter_jsonl_reverse(path, before=newest[0][0], chunk_size=32))

        self.assertEqual(newest[0][1]["data"]["message"].split("-", 2)[:2], ["line", "4"])
        self.assertEqual(older[0][1]["data"]["message"].split("-", 2)[:2], ["line", "3"])
        self.assertGreater(newest[0][0], older[0][0])

    def test_task_log_page_tails_and_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                log_path = run_dir(root, run_id) / task["log_file"]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("\n".join(f"line-{index}" for index in range(5)) + "\n", encoding="utf-8")

                latest = task_log_page(root, run_id, "task-001", limit=2)
                older = task_log_page(root, run_id, "task-001", limit=2, before=latest["next_before_offset"])

        self.assertEqual(latest["text"], "line-3\nline-4")
        self.assertTrue(latest["has_more"])
        self.assertEqual(older["text"], "line-1\nline-2")
        self.assertTrue(older["has_more"])

    def test_task_log_page_falls_back_to_event_log_when_task_log_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Event logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "first"})
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "output_tail": "second"})

                latest = task_log_page(root, run_id, "task-001", limit=1)
                older = task_log_page(root, run_id, "task-001", limit=1, before=latest["next_before_offset"], source=latest["source"])

        self.assertEqual(latest["source"], "events")
        self.assertIn("agent_command_finished", latest["text"])
        self.assertIn("second", latest["text"])
        self.assertNotIn("other task", latest["text"])
        self.assertEqual(older["source"], "events")
        self.assertIn("first", older["text"])

    def test_task_lightweight_snapshots_exclude_heavy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Lightweight", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "large message", sender="browser", task_id="task-001")
                write_task_result(root, run_id, "task-001", "final text")

                final = task_final_snapshot(root, run_id, "task-001")
                context = task_context_snapshot(root, run_id, "task-001")

        self.assertEqual(final["result"].strip(), "final text")
        self.assertNotIn("messages", final)
        self.assertNotIn("log", final)
        self.assertIn("prompt", context)
        self.assertNotIn("messages", context)
        self.assertNotIn("log", context)

    def test_workspace_options_include_multiple_project_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hl_root = base / "hl_project"
            my_root = base / "my_project"
            (hl_root / "fw_omni_builder").mkdir(parents=True)
            (my_root / "aha").mkdir(parents=True)

            options = workspace_options([hl_root, my_root])

        self.assertEqual(
            options,
            [
                {
                    "name": "fw_omni_builder",
                    "label": "hl_project/fw_omni_builder",
                    "path": str(hl_root / "fw_omni_builder"),
                    "root": str(hl_root),
                },
                {
                    "name": "aha",
                    "label": "my_project/aha",
                    "path": str(my_root / "aha"),
                    "root": str(my_root),
                },
            ],
        )

    def test_aha_status_command_formats_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Command help", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                output = format_aha_command(root, run_id, "task-001", "/aha status")
                self.assertIn("Task: task-001", output)
                self.assertIn("Status: pending", output)

                backend_output = format_aha_command(root, run_id, "task-001", "/aha backend status")
                self.assertIn("Unknown AHA command", backend_output)

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha status",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertEqual(payload["message"]["sender"], "AHA")
                self.assertEqual(payload["message"]["agent_id"], "main")
                page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
                messages = [event["data"] for event in page["events"] if event["type"] == "message"]
                self.assertTrue(any(message.get("message") == "/aha status" and message.get("agent_id") == "main" for message in messages))
                self.assertTrue(any(message.get("sender") == "AHA" and "Task: task-001" in message.get("message", "") for message in messages))

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("AHA commands:", payload["message"]["message"])

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("message", payload)
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                self.assertEqual(main_messages[-1]["sender"], "aha")
                self.assertEqual(main_messages[-1]["result_policy"], "finalize")
                self.assertEqual(main_messages[-1]["original_command"], "/aha final")
                self.assertIn("Generate or update the task Final", main_messages[-1]["message"])

                handled, agent_message, reply = format_agent_command(root, run_id, "task-001", "main", "/agent help")
                self.assertFalse(handled)
                self.assertEqual(agent_message, "/help")
                self.assertIsNone(reply)

                handled, agent_message, reply = format_agent_command(root, run_id, "task-001", "main", "/agent status")
                self.assertFalse(handled)
                self.assertEqual(agent_message, "/status")
                self.assertIsNone(reply)

                handled, agent_message, reply = format_agent_command(root, run_id, "task-001", "main", "/agent")
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Usage: /agent <command>", reply or "")

    def test_backend_cli_command_is_not_exposed(self) -> None:
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit) as raised:
            main(["backend"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice: 'backend'", err.getvalue())

    def test_api_bootstrap_works_without_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            response = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap"))
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["aha_home"], str(root))
        self.assertFalse(body["initialized"])
        self.assertIn("default_workspace_path", body)
        self.assertEqual(body["default_run_id"], "")
        self.assertEqual(body["runs"], [])

    def test_api_workspace_registration_can_create_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "repo"
            root.mkdir()
            workspace.mkdir()
            add_response = asyncio.run(
                fetch_ui_response(root, "", "/api/workspaces", method="POST", payload={"path": str(workspace), "name": "demo"})
            )
            add_body = json_response_body(add_response)
            create_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={"goal": "Web setup", "mode": "research", "workspace_id": add_body["workspace"]["id"]},
                )
            )
            create_body = json_response_body(create_response)
            plan = read_json(root / "runs" / create_body["run"]["id"] / "plan.json")

        self.assertTrue(add_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["workspace_id"], "ws-001")
        self.assertEqual(plan["tasks"][0]["workspace_path"], str(workspace))

    def test_api_run_creation_accepts_proxy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={
                        "goal": "Proxy setup",
                        "mode": "research",
                        "backend": "codex",
                        "proxy_enabled": True,
                        "http_proxy": "http://127.0.0.1:7890",
                        "https_proxy": "http://127.0.0.1:7890",
                        "no_proxy": "localhost,127.0.0.1",
                    },
                )
            )
            body = json_response_body(response)
            plan = read_json(root / "runs" / body["run"]["id"] / "plan.json")
            task = plan["tasks"][0]

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(task["preferred_proxy_enabled"])
        self.assertEqual(task["preferred_http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(task["preferred_https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(task["preferred_no_proxy"], "localhost,127.0.0.1")
        self.assertTrue(task["agents"][0]["proxy_enabled"])

    def test_api_run_creation_can_dispatch_initial_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            with mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend:
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        "",
                        "/api/runs",
                        method="POST",
                        payload={
                            "goal": "Web setup",
                            "mode": "research",
                            "backend": "codex",
                            "task_titles": ["Web setup"],
                            "dispatch": True,
                        },
                    )
                )
                body = json_response_body(response)
            run_id = body["run"]["id"]
            plan = read_json(root / "runs" / run_id / "plan.json")
            events, _ = iter_jsonl_from(root / "runs" / run_id / "events.jsonl", 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["title"], "Web setup")
        self.assertTrue(any(event["type"] == "task_dispatched" and event["data"]["task_id"] == "task-001" for event in events))
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
        self.assertTrue(start_backend.call_args.kwargs["from_start"])

    def test_api_runs_lists_and_creates_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Default session", "--agents", "1")
                self.assertEqual(code, 0)
                default_run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                runs_response = asyncio.run(fetch_ui_response(root, default_run_id, "/api/runs"))
                runs_body = json_response_body(runs_response)
                create_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        default_run_id,
                        "/api/runs",
                        method="POST",
                        payload={"goal": "Second session", "agents": 1, "mode": "research"},
                    )
                )
                create_body = json_response_body(create_response)
                updated_response = asyncio.run(fetch_ui_response(root, default_run_id, "/api/runs"))
                updated_body = json_response_body(updated_response)

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(runs_body["default_run_id"], default_run_id)
        self.assertIn(default_run_id, {item["id"] for item in runs_body["runs"]})
        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(create_body["ok"])
        self.assertEqual(create_body["run"]["goal"], "Second session")
        self.assertIn(create_body["run"]["id"], {item["id"] for item in updated_body["runs"]})

    def test_api_run_archive_exports_and_imports_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "UI archive",
                    "--agents",
                    "1",
                    "--enable-proxy",
                    "--http-proxy",
                    "http://user:secret@example.test:8080",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "ui-backend-secret"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                log_file = run_dir(root, run_id) / "logs" / "backend.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text("backend log", encoding="utf-8")

                export_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/run/export?run_id={run_id}&no_logs=1", timeout=2.0)
                )
                export_headers, archive_bytes = export_response.split(b"\r\n\r\n", 1)
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as exported:
                    names = set(exported.getnames())
                    plan = json.load(exported.extractfile("run/plan.json"))

                boundary = "----aha-test-boundary"
                multipart_body = (
                    (
                        f"--{boundary}\r\n"
                        'Content-Disposition: form-data; name="archive"; filename="run.tar.gz"\r\n'
                        "Content-Type: application/gzip\r\n"
                        "\r\n"
                    ).encode("ascii")
                    + archive_bytes
                    + f"\r\n--{boundary}--\r\n".encode("ascii")
                )
                import_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/run/import",
                        timeout=3.0,
                        method="POST",
                        body=multipart_body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    )
                )
                import_body = json_response_body(import_response)
                imported_run_id = import_body["imported_run_id"]
                imported_status = status_snapshot(root, imported_run_id)

        self.assertTrue(export_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'Content-Disposition: attachment; filename="aha-run-', export_headers)
        self.assertIn("aha-run-manifest.json", names)
        self.assertIn("run/plan.json", names)
        self.assertNotIn("run/logs/backend.log", names)
        self.assertEqual(plan["tasks"][0]["preferred_http_proxy"], "<redacted>")
        self.assertNotIn("secret", json.dumps(plan))
        self.assertTrue(import_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(import_body["source_run_id"], run_id)
        self.assertNotEqual(imported_run_id, run_id)
        self.assertIn(imported_run_id, {item["id"] for item in import_body["runs"]})
        self.assertEqual(imported_status["tasks"][0]["agents"][0]["session_status"], "imported")
        self.assertIsNone(imported_status["tasks"][0]["agents"][0]["backend_session_id"])

    def test_api_routes_can_target_non_default_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, first_output = self.run_cli("plan", "First session", "--agents", "1")
                self.assertEqual(code, 0)
                first_run_id = first_output.splitlines()[0].split(": ", 1)[1]
                code, second_output = self.run_cli("plan", "Second session", "--agents", "1")
                self.assertEqual(code, 0)
                second_run_id = second_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, second_run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "second-only-event"})
                append_message(root, second_run_id, "main", "second conversation", sender="browser", task_id="task-001", role="main")

                default_status = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, "/api/status")))
                selected_status = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/status?run_id={second_run_id}")))
                events = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/events?run_id={second_run_id}&offset=0&limit=50")))
                conversation = json_response_body(
                    asyncio.run(fetch_ui_response(root, first_run_id, f"/api/conversation-events?run_id={second_run_id}&task_id=task-001&target=main"))
                )
                send_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        first_run_id,
                        "/api/send",
                        method="POST",
                        payload={"run_id": second_run_id, "target": "manual-target", "message": "sent to second"},
                    )
                )
                task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        first_run_id,
                        "/api/tasks",
                        method="POST",
                        payload={"run_id": second_run_id, "title": "Second extra task", "dispatch": False},
                    )
                )
                first_after = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, "/api/status")))
                second_after = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/status?run_id={second_run_id}")))
                first_manual, _ = iter_jsonl_from(inbox_path(root, first_run_id, "manual-target"), 0)
                second_manual, _ = iter_jsonl_from(inbox_path(root, second_run_id, "manual-target"), 0)

        self.assertEqual(default_status["run_id"], first_run_id)
        self.assertEqual(default_status["goal"], "First session")
        self.assertEqual(selected_status["run_id"], second_run_id)
        self.assertEqual(selected_status["goal"], "Second session")
        self.assertEqual(events["run_id"], second_run_id)
        self.assertTrue(any(event.get("data", {}).get("text") == "second-only-event" for event in events["events"]))
        self.assertTrue(any(event.get("data", {}).get("message") == "second conversation" for event in conversation["events"]))
        self.assertTrue(send_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(task_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(first_after["tasks"][0]["title"], "Map the relevant files, concepts, and terminology for the goal.")
        self.assertEqual(len(first_after["tasks"]), 1)
        self.assertEqual(len(second_after["tasks"]), 2)
        self.assertEqual(first_manual, [])
        self.assertEqual(second_manual[-1]["message"], "sent to second")

    def test_api_routes_fallback_to_latest_run_without_server_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Default fallback", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                runs_response = asyncio.run(fetch_ui_response(root, "", "/api/runs"))
                status_response = asyncio.run(fetch_ui_response(root, "", "/api/status"))

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(status_response.startswith(b"HTTP/1.1 200 OK"))
        runs_body = json_response_body(runs_response)
        status_body = json_response_body(status_response)
        self.assertEqual(runs_body["default_run_id"], run_id)
        self.assertEqual(status_body["run_id"], run_id)
        self.assertEqual(status_body["goal"], "Default fallback")

    def test_ui_core_endpoints_return_without_full_event_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Fast UI endpoints", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(3000):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"event-{index}"})
                set_task_status(root, run_id, "task-001", "completed", exit_code=0)

                responses = {
                    target: asyncio.run(fetch_ui_response(root, run_id, target))
                    for target in ("/", "/static/app.js", "/api/status", "/api/events?offset=-1")
                }

        for response in responses.values():
            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        events_body = json_response_body(responses["/api/events?offset=-1"])
        self.assertEqual(events_body["events"], [])
        self.assertGreater(events_body["offset"], 0)
        status_body = json_response_body(responses["/api/status"])
        self.assertEqual(status_body["tasks"][0]["display_status"], "completed")

    def test_api_events_uses_snapshot_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Paged events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(10):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"event-{index}"})

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=0&limit=3"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(len(body["events"]), 3)
        self.assertEqual(body["limit"], 3)
        self.assertTrue(body["has_more"])
        self.assertLess(body["offset"], body["snapshot_offset"])

    def test_chat_prompt_uses_recent_events_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recent prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(30):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"prompt-event-{index}"})

                prompt = chat_prompt(root, run_id, "main", {"sender": "browser", "message": "status"}, "")

        self.assertIn("prompt-event-29", prompt)
        self.assertNotIn("prompt-event-0", prompt)

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
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "foreign-event"})
                append_event(root, run_id, "agent_message", {"task_id": "task-003", "target": "main", "text": "current-event"})

                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "status", "task_id": "task-003", "role": "main"},
                    "",
                )

        self.assertIn("Current compact prompt title", prompt)
        self.assertIn("current-event", prompt)
        self.assertIn("task_counts", prompt)
        self.assertNotIn("Foreign verbose task title that should stay out", prompt)
        self.assertNotIn("foreign-event", prompt)

    def test_chat_prompt_uses_delta_for_existing_sticky_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sticky prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_event(
                    root,
                    run_id,
                    "agent_message",
                    {"task_id": "task-001", "target": "main", "text": "already-in-backend-session"},
                )
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                append_event(root, run_id, "task_hidden", {"task_id": "task-001"})

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
                    "prefix",
                )

        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertIn("Current task delta:", prompt)
        self.assertIn("backend-session-1", prompt)
        self.assertIn("next request", prompt)
        self.assertIn("task_hidden", prompt)
        self.assertNotIn("Ownership and routing policy", prompt)
        self.assertNotIn("already-in-backend-session", prompt)
        self.assertIn("sticky_context", metrics["components"])
        self.assertIn("delta_status", metrics["components"])
        self.assertNotIn("task_context", metrics["components"])

    def test_finalization_prompt_omits_recent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(
                    root,
                    run_id,
                    "message",
                    {
                        "task_id": "task-001",
                        "sender": "browser",
                        "target": "main",
                        "message": "NOISY_RECENT_EVENT" * 1000,
                    },
                )

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "aha",
                        "message": finalization_prompt("task-001", "Final prompt", []),
                        "task_id": "task-001",
                        "role": "main",
                        "result_policy": "finalize",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
                )

        self.assertTrue(metrics["is_finalization"])
        self.assertEqual(metrics["event_limit"], 0)
        self.assertIn("omitted for finalization", prompt)
        self.assertNotIn("NOISY_RECENT_EVENT", prompt)
        self.assertLess(metrics["components"]["recent_events"]["chars"], 120)

    def test_watch_tail_starts_at_current_event_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Tail watch", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "old-event"})

                code, watch_output = self.run_cli("watch", run_id, "--once", "--tail")

        self.assertEqual(code, 0)
        self.assertIn("Tail watch", watch_output)
        self.assertNotIn("old-event", watch_output)

    def test_frontend_prefers_websocket_with_cursor_and_polling_fallback(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "src" / "aha_cli" / "web" / "static" / "app.js"
        script = script_path.read_text(encoding="utf-8")
        websocket_index = script.find("new WebSocket")

        self.assertGreaterEqual(websocket_index, 0, "frontend should open a WebSocket transport")
        websocket_block = script[max(0, websocket_index - 2000) : websocket_index + 3000]
        self.assertRegex(script, r"last_event_id|after_event_id")
        self.assertRegex(websocket_block, r"onclose|onerror|catch")
        self.assertIn("pollEvents", script)
        self.assertIn("catchUpRealtimeEvents", script)
        self.assertIn("forcePoll", script)
        self.assertIn("allowStalePoll", script)
        self.assertIn("lastRealtimeMessageAt", script)
        self.assertIn("eventSocketStaleMs", script)
        self.assertIn("closeStaleEventWebSocket", script)
        self.assertIn("ws.stale_close", script)
        self.assertIn("visibilitychange", script)
        self.assertIn('"online"', script)
        self.assertRegex(script, r"typeof\s+WebSocket|WebSocket\s+in\s+window|window\.WebSocket")

    def test_frontend_uses_bootstrap_for_empty_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "src" / "aha_cli" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "src" / "aha_cli" / "web" / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("/api/bootstrap", script)
        self.assertIn("renderFirstRunState", script)
        self.assertIn("data-bootstrap-run-form", script)
        self.assertIn("createRunFromBootstrapForm", script)
        self.assertIn("data-bootstrap-proxy-enabled", script)
        self.assertIn("data-bootstrap-http-proxy", script)
        self.assertIn("data-bootstrap-https-proxy", script)
        self.assertIn("data-bootstrap-no-proxy", script)
        self.assertIn("Start Run and Initial Task", script)
        self.assertIn("renderBootstrapError", script)
        self.assertIn("body.empty-run .sidebar", styles)
        self.assertIn("bootstrap-panel", styles)
        self.assertIn("bootstrap-proxy", styles)

    def test_frontend_uses_modal_for_task_create(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "src" / "aha_cli" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "src" / "aha_cli" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "src" / "aha_cli" / "web" / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("open-task-create", html)
        self.assertIn("task-create-dialog", html)
        self.assertIn('form id="task-form"', html)
        self.assertIn("openTaskCreateDialog", script)
        self.assertIn("closeTaskCreateDialog", script)
        self.assertIn('action === "add-task"', script)
        self.assertIn("task-dialog", styles)
        self.assertIn("task-create-trigger", styles)

    def test_frontend_renders_prompt_metrics_visualization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "src" / "aha_cli" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "src" / "aha_cli" / "web" / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("agent_prompt_metrics", script)
        self.assertIn("agent_context_overflow", script)
        self.assertIn("renderPromptMetricsPanel", script)
        self.assertIn("AHA Input", script)
        self.assertIn("Backend Usage", script)
        self.assertIn("Backend Session", script)
        self.assertIn("/aha session compact-reset", script)
        self.assertIn("compactResetStates", script)
        self.assertIn("Compacting", script)
        self.assertIn("Restarting", script)
        self.assertIn("COMPACT_RESET_TIMEOUT_MS", script)
        self.assertIn("Checking", script)
        self.assertIn("verifyCompactResetAfterTimeout", script)
        self.assertIn("isRequestTimeoutError", script)
        self.assertIn("compactResetSelectedSession", script)
        self.assertIn("ahaInputMetricsStatus", script)
        self.assertIn("backendSessionStatus", script)
        self.assertIn("BACKEND_SESSION_LARGE_BYTES", script)
        self.assertIn("sessionAhaCounts", script)
        self.assertIn("Session breakdown", script)
        self.assertIn("renderSessionBreakdown", script)
        self.assertIn("AHA input breakdown", script)
        self.assertIn("Backend usage breakdown", script)
        self.assertIn("closePromptMetricsBreakdowns", script)
        self.assertIn("sessionBreakdownOpen", script)
        self.assertIn("breakdownOpen", script)
        self.assertIn("cache_read_input_tokens", script)
        self.assertIn("cache_creation_input_tokens", script)
        self.assertIn("total_cost_usd", script)
        self.assertIn("usageMetricsStatus", script)
        self.assertIn("prompt-component-bars", script)
        self.assertIn("backend-metrics-section", styles)
        self.assertIn("session-metrics-section", styles)
        self.assertIn("prompt-current", styles)
        self.assertIn("prompt-latest", styles)
        self.assertIn("session-large", styles)
        self.assertIn("session-overflow", styles)
        self.assertIn("session-pending", styles)
        self.assertIn("session-done", styles)
        self.assertIn("prompt-metrics-head-actions", styles)
        self.assertIn("session-breakdown-row", styles)
        self.assertIn("metrics-breakdown-row-wide", styles)
        self.assertIn("usage-previous", styles)
        self.assertIn("session-ok", styles)
        self.assertIn("prompt-metrics", styles)
        self.assertIn("prompt-component-track", styles)

    def test_prompt_templates_are_packaged_and_renderable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        prompt = render_prompt_template(
            "backend_chat_delta.md",
            prefix="prefix",
            target="main",
            mode_instruction="reply",
            run_goal="goal",
            status="status",
            sticky_context="context",
            recent_events="events",
            sender="browser",
            ts="2026-01-01T00:00:00+00:00",
            message="hello",
        )

        self.assertIn('"aha_cli.prompts" = ["*.md"]', pyproject)
        self.assertIn("You are the AHA backend agent for `main`.", prompt)
        self.assertIn("User message from browser", prompt)

    def test_websocket_starts_from_tail_for_large_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket tail", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(1000):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"old-ws-event-{index}"})

                messages = asyncio.run(fetch_initial_ws_messages(root, run_id))

        self.assertEqual([message["type"] for message in messages], ["status"])

    def test_websocket_sends_heartbeat_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket heartbeat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.websocket.server.WS_HEARTBEAT_INTERVAL", 0.01):
                    messages = asyncio.run(fetch_ws_messages(root, run_id, timeout=0.3, max_messages=2))

        self.assertEqual(messages[0]["type"], "status")
        self.assertEqual(messages[1]["type"], "heartbeat")
        self.assertIn("last_event_id", messages[1])

    def test_websocket_replays_from_last_event_id_after_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket replay", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                baseline = append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "before-disconnect"})
                missed_one = append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "missed-ws-1"})
                missed_two = append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})

                messages = asyncio.run(
                    fetch_ws_messages(root, run_id, f"/?last_event_id={baseline['event_id']}", max_messages=3)
                )

        self.assertEqual([message["type"] for message in messages], ["status", "event", "event"])
        replayed = [message["data"] for message in messages if message["type"] == "event"]
        self.assertEqual([event["event_id"] for event in replayed], [missed_one["event_id"], missed_two["event_id"]])
        self.assertEqual(replayed[0]["data"]["text"], "missed-ws-1")
        self.assertEqual(replayed[1]["type"], "agent_finished")

    def test_websocket_same_cursor_replays_same_events_to_multiple_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket multi replay", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                baseline = append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "before-clients"})
                expected = [
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "client-replay-1"})[
                        "event_id"
                    ],
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "client-replay-2"})[
                        "event_id"
                    ],
                ]

                async def fetch_both() -> tuple[list[dict], list[dict]]:
                    first_messages, second_messages = await asyncio.gather(
                        fetch_ws_messages(root, run_id, f"/?after_event_id={baseline['event_id']}", max_messages=3),
                        fetch_ws_messages(root, run_id, f"/?after_event_id={baseline['event_id']}", max_messages=3),
                    )
                    return first_messages, second_messages

                first, second = asyncio.run(fetch_both())

        first_events = [message["data"]["event_id"] for message in first if message["type"] == "event"]
        second_events = [message["data"]["event_id"] for message in second if message["type"] == "event"]
        self.assertEqual(first_events, expected)
        self.assertEqual(second_events, expected)

    def test_start_backend_serializes_concurrent_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend start lock", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                    concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    futures = [
                        pool.submit(start_backend, root, run_id, "main", task_id="task-001")
                        for _ in range(2)
                    ]
                    results = [future.result(timeout=10) for future in futures]

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(sum(1 for result in results if result.get("started")), 1)
        self.assertEqual(sum(1 for result in results if result.get("already_running")), 1)

    def test_start_backend_preserves_home_and_absolute_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {"PYTHONPATH": "src"}, clear=False),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertIn("--home", command)
        self.assertEqual(command[command.index("--home") + 1], str(root / ".aha"))
        self.assertTrue(Path(env["PYTHONPATH"].split(os.pathsep)[0]).is_absolute())

    def test_start_backend_uses_zipapp_invocation_for_onebin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "aha"
            code, output = self.run_cli("package", "onebin", "--output", str(artifact))
            self.assertEqual(code, 0, output)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "One-bin backend start", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.sys.argv", [str(artifact)]),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, str(artifact.resolve())])
        self.assertIn("codex-chat", command)
        self.assertIn("--home", command)
        self.assertEqual(command[command.index("--home") + 1], str(root / ".aha"))

    def test_start_backend_uses_claude_chat_command_for_claude_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude backend start", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001", claude_bin="claude-dev")

        command = popen.call_args.args[0]
        self.assertIn("claude-chat", command)
        self.assertIn("--claude-bin", command)
        self.assertEqual(command[command.index("--claude-bin") + 1], "claude-dev")

    def test_backend_status_reports_discovered_claude_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude backend discovery", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with (
                    mock.patch(
                        "aha_cli.services.backend_runtime._discover_backend_process",
                        return_value=(4242, "claude-chat"),
                    ),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = backend_status(root / ".aha", run_id, "main", task_id="task-001")

        self.assertEqual(status["backend"], "claude-chat")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["pid"], 4242)

    def test_backend_process_home_matching_rejects_other_aha_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_home = root / "current" / ".aha"
            other_home = root / "other" / ".aha"
            current_home.mkdir(parents=True)
            other_home.mkdir(parents=True)
            parts = [
                sys.executable,
                "-m",
                "aha_cli",
                "--home",
                str(other_home),
                "claude-chat",
                "run-001",
                "main",
                "--task-id",
                "task-024",
            ]

            self.assertFalse(_process_matches_home(parts, current_home))
            parts[parts.index("--home") + 1] = str(current_home)
            self.assertTrue(_process_matches_home(parts, current_home))

    def test_start_backend_injects_claude_env_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["claude"]["env"] = {
                    "ANTHROPIC_API_KEY": "test-key",
                    "base_url": "https://claude.test",
                }
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Claude env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "test-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://claude.test")

    def test_claude_exec_reports_missing_auth_env_before_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("aha_cli.backends.claude.subprocess.Popen") as popen,
            ):
                code, reply, _ = run_claude_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    events_file=events,
                    run_id="run-001",
                    task_id="task-001",
                    source="claude-chat",
                    target="main",
                )

            self.assertEqual(code, 1)
            self.assertIn("Claude authentication is not configured", reply)
            self.assertEqual(output.read_text(encoding="utf-8"), reply)
            popen.assert_not_called()

    def test_start_backend_applies_task_proxy_env_for_enabled_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Backend proxy env",
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

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {"HTTP_PROXY": "http://outer", "NO_PROXY": "outer"}, clear=False),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1")

    def test_start_backend_clears_inherited_proxy_env_for_disabled_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Backend no proxy env",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_agent_config(root / ".aha", run_id, "task-001", "main", proxy_enabled=False)

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(
                        os.environ,
                        {"HTTP_PROXY": "http://outer", "HTTPS_PROXY": "http://outer", "NO_PROXY": "outer"},
                        clear=False,
                    ),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("NO_PROXY", env)
        self.assertNotIn("http_proxy", env)

    def test_backend_activity_can_be_filtered_by_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Scoped backend activity", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                self.run_cli("task", "add", run_id, "Second task", "--no-dispatch")
                append_event(root, run_id, "agent_started", {"target": "main", "task_id": "task-002"})

                task_one = backend_status(root, run_id, "main", task_id="task-001")
                task_two = backend_status(root, run_id, "main", task_id="task-002")

        self.assertFalse(task_one["busy"])
        self.assertTrue(task_two["busy"])

    def test_stop_task_backends_skips_current_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stop task workers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")

                def fake_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {
                        "target": target,
                        "task_id": task_id,
                        "status": "running",
                        "pid": 111 if target == "main" else 222,
                    }

                with (
                    mock.patch("aha_cli.services.backend_runtime.backend_status", side_effect=fake_status),
                    mock.patch("aha_cli.services.backend_runtime.stop_backend", side_effect=lambda _root, _run_id, target, **_kwargs: {"target": target, "stopped": True}) as stop_backend,
                ):
                    stopped = stop_task_backends(root, run_id, "task-001", exclude_pid=111)

        self.assertEqual(stopped, [{"target": "sub-001", "stopped": True}])
        stop_backend.assert_called_once()
        self.assertEqual(stop_backend.call_args.args[:3], (root, run_id, "sub-001"))

    def test_agent_permission_update_is_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Permissions", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                agent = update_agent_config(root, run_id, "task-001", "main", sandbox="workspace-write", approval="never")
                self.assertEqual(agent["sandbox"], "workspace-write")

                snapshot = status_snapshot(root, run_id)
                task = snapshot["tasks"][0]
                self.assertEqual(task["preferred_sandbox"], "workspace-write")
                self.assertEqual(task["agents"][0]["sandbox"], "workspace-write")
                self.assertEqual(task["agents"][0]["approval"], "never")

    def test_task_proxy_config_and_agent_toggle_are_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Proxy defaults",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_config(root, run_id, "task-001", "sub-001", proxy_enabled=False)
                task = update_task_proxy_config(
                    root,
                    run_id,
                    "task-001",
                    proxy_enabled=False,
                    http_proxy="http://127.0.0.1:8888",
                    https_proxy="http://127.0.0.1:8888",
                    no_proxy="localhost,127.0.0.1",
                )
                self.assertFalse(task["preferred_proxy_enabled"])

                snapshot = status_snapshot(root, run_id)
                task = snapshot["tasks"][0]
                agents = {agent["id"]: agent for agent in task["agents"]}

        self.assertEqual(task["preferred_http_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(task["preferred_https_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(task["preferred_no_proxy"], "localhost,127.0.0.1")
        self.assertFalse(task["preferred_proxy_enabled"])
        self.assertTrue(agents["main"]["proxy_enabled"])
        self.assertFalse(agents["sub-001"]["proxy_enabled"])

    def test_task_proxy_config_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Proxy API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-config",
                        method="POST",
                        payload={
                            "task_id": "task-001",
                            "proxy_enabled": True,
                            "http_proxy": "http://proxy.local:8080",
                            "https_proxy": "http://proxy.local:8080",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertEqual(body["task"]["preferred_http_proxy"], "http://proxy.local:8080")
        self.assertEqual(body["task"]["preferred_no_proxy"], "localhost,127.0.0.1")

    def test_task_proxy_action_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Proxy action API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/proxy",
                        method="POST",
                        payload={
                            "proxy_enabled": True,
                            "http_proxy": "http://proxy.local:8080",
                            "https_proxy": "http://proxy.local:8080",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertEqual(body["task"]["preferred_http_proxy"], "http://proxy.local:8080")

    def test_task_hide_restore_and_soft_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task visibility", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                hidden = set_task_hidden(root, run_id, "task-001", True)
                self.assertTrue(hidden["hidden"])
                snapshot = status_snapshot(root, run_id)
                self.assertTrue(snapshot["tasks"][0]["hidden"])

                restored = set_task_hidden(root, run_id, "task-001", False)
                self.assertFalse(restored["hidden"])

                deleted = delete_task(root, run_id, "task-001")
                self.assertIsNotNone(deleted["deleted_at"])
                snapshot = status_snapshot(root, run_id)
                self.assertEqual(snapshot["tasks"], [])

    def test_task_agent_and_session_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Manage agents", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, task_output = self.run_cli(
                    "task",
                    "add",
                    run_id,
                    "Extra task",
                    "--backend",
                    "codex",
                    "--workspace-path",
                    str(root),
                    "--max-sub-agents",
                    "2",
                )
                self.assertEqual(code, 0)
                self.assertIn("Extra task", task_output)
                self.assertIn('"workspace_path"', task_output)
                self.assertIn('"delegation_policy": "auto"', task_output)
                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("task_dispatched", watch_output)
                self.assertIn("You are now running in AHA mode", watch_output)

                code, agent_output = self.run_cli("agent", "add", run_id, "task-001", "--backend", "stub")
                self.assertEqual(code, 0)
                self.assertIn("sub-001", agent_output)

                code, list_output = self.run_cli("agent", "list", run_id, "task-001")
                self.assertEqual(code, 0)
                self.assertIn("main role=task-main", list_output)
                self.assertIn("sub-001 role=sub backend=stub", list_output)

                code, sessions = self.run_cli("session", "list", run_id, "--task-id", "task-001")
                self.assertEqual(code, 0)
                self.assertIn('"agent_id": "main"', sessions)
                self.assertIn('"agent_id": "sub-001"', sessions)


if __name__ == "__main__":
    unittest.main()
