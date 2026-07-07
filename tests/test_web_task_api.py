from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.backends.registry import CODEX_DEFAULT_MODEL
from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    complete_task,
    inbox_path,
    iter_jsonl_from,
    read_json,
    run_dir,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_snapshot,
    update_agent_config,
    update_task_proxy_config,
    update_task_supervision_config,
    write_task_result,
)
from aha_cli.store.sessions import ensure_session, save_session
from aha_cli.store.task_memos import create_task_memo
from aha_cli.store.io import write_json
from aha_cli.store.paths import config_path
from aha_cli.web.server import handle_send_payload, workspace_options
from tests.helpers import fetch_ui_response, json_response_body


class WebTaskApiTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_api_task_create_accepts_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task details", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Detailed task",
                            "description": "Use the attached notes and preserve existing behavior.",
                            "collaboration_mode": "team",
                            "workflow_template": "FAULT-DEBUG",
                            "dispatch": False,
                        },
                    )
                )
                body = json_response_body(response)
                status = status_snapshot(root, run_id)
                context = task_context_snapshot(root, run_id, body["task"]["id"])
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)
                task_created = next(
                    event
                    for event in events
                    if event["type"] == "task_created" and event["data"]["task_id"] == body["task"]["id"]
                )

        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["description"], "Use the attached notes and preserve existing behavior.")
        self.assertEqual(body["task"]["collaboration_mode"], "team")
        self.assertEqual(body["task"]["workflow_template"], "fault-debug")
        self.assertEqual(body["task"]["delegation_policy"], "auto")
        self.assertEqual(body["task"]["max_sub_agents"], 2)
        self.assertEqual(body["task"]["preferred_sandbox"], "danger-full-access")
        self.assertEqual(body["task"]["agents"][0]["sandbox"], "danger-full-access")
        self.assertEqual(body["task"]["supervision"]["max_rounds"], 99)
        self.assertFalse(any(body["task"]["supervision"]["ask_user_gates"].values()))
        self.assertFalse(body["task"]["context_management"]["auto_compact_enabled"])
        self.assertEqual(body["task"]["context_management"]["auto_compact_threshold_percent"], 75)
        self.assertFalse(body["task"]["token_saving"]["enabled"])
        self.assertEqual(body["task"]["token_saving"]["provider"], "nav")
        self.assertEqual(status["tasks"][-1]["description"], "Use the attached notes and preserve existing behavior.")
        self.assertEqual(status["tasks"][-1]["collaboration_mode"], "team")
        self.assertEqual(status["tasks"][-1]["workflow_template"], "fault-debug")
        self.assertEqual(status["tasks"][-1]["max_sub_agents"], 2)
        self.assertEqual(status["tasks"][-1]["preferred_sub_backend"], "codex")
        self.assertIsNone(status["tasks"][-1]["preferred_sub_model"])
        self.assertEqual(task_created["data"]["collaboration_mode"], "team")
        self.assertEqual(task_created["data"]["workflow_template"], "fault-debug")
        self.assertEqual(task_created["data"]["max_sub_agents"], 2)
        self.assertEqual(task_created["data"]["preferred_sub_backend"], "codex")
        self.assertIsNone(task_created["data"]["preferred_sub_model"])
        self.assertIn("Use the attached notes and preserve existing behavior.", context["prompt"])

    def test_api_task_create_accepts_token_saving_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task token saving create config", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Token saving configured task",
                            "dispatch": False,
                            "token_saving": {
                                "enabled": True,
                                "provider": "nav",
                            },
                        },
                    )
                )
                body = json_response_body(response)
                status = status_snapshot(root, run_id)

        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["token_saving"]["enabled"])
        self.assertEqual(body["task"]["token_saving"]["provider"], "nav")
        self.assertEqual(status["tasks"][-1]["token_saving"], body["task"]["token_saving"])

    def test_api_task_create_persists_codex_default_model_for_empty_ui_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg = read_json(config_path(root))
                cfg.setdefault("codex", {})["model"] = "env:kimi-k2.6"
                cfg["codex"]["env"] = [
                    {
                        "name": "kimi-k2.6",
                        "OPENAI_API_KEY": "test-key",
                        "OPENAI_BASE_URL": "https://kimi.test/v1",
                        "OPENAI_MODEL": "kimi-k2.6",
                    }
                ]
                write_json(config_path(root), cfg)
                code, plan_output = self.run_cli("plan", "Task default model create config", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Default model task",
                            "backend": "codex",
                            "model": None,
                            "dispatch": False,
                        },
                    )
                )
                body = json_response_body(response)
                status = status_snapshot(root, run_id)
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)
                task_created = next(
                    event
                    for event in events
                    if event["type"] == "task_created" and event["data"]["task_id"] == body["task"]["id"]
                )
                main_agent = next(agent for agent in body["task"]["agents"] if agent["id"] == "main")

        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["preferred_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(body["task"]["preferred_sub_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(main_agent["model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(status["tasks"][-1]["preferred_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(task_created["data"]["preferred_model"], CODEX_DEFAULT_MODEL)

    def test_api_task_create_accepts_token_saving_without_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task token saving disabled integration", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Token saving without headroom",
                            "dispatch": False,
                            "token_saving": {"enabled": True, "provider": "nav"},
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["token_saving"]["enabled"])
        self.assertEqual(body["task"]["token_saving"]["provider"], "nav")

    def test_api_task_create_and_update_accepts_observe_proxy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Observe proxy config", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Observed task",
                            "dispatch": False,
                            "observe_proxy": {"enabled": True},
                        },
                    )
                )
                body = json_response_body(response)
                task_id = body["task"]["id"]
                update_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task/{task_id}/observe-proxy",
                        method="POST",
                        payload={"enabled": False},
                    )
                )
                update_body = json_response_body(update_response)
                snapshot = status_snapshot(root, run_id)
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(update_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["task"]["observe_proxy"]["enabled"])
        self.assertFalse(update_body["task"]["observe_proxy"]["enabled"])
        self.assertFalse(snapshot["tasks"][-1]["observe_proxy"]["enabled"])
        observe_events = [event for event in events if event["type"] == "task_observe_proxy_config_updated"]
        self.assertEqual(observe_events[-1]["data"]["task_id"], task_id)
        self.assertFalse(observe_events[-1]["data"]["enabled"])

    def test_api_task_create_and_update_accepts_hardware_debug_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Hardware debug config", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Board task",
                            "dispatch": False,
                            "task_skills": {
                                "enabled_paths": ["/repo/.aha/skills/board-debug/SKILL.md"],
                            },
                            "hardware_debug": {
                                "channels": [
                                    {
                                        "type": "uart",
                                        "settings": {
                                            "port": "/dev/ttyUSB0",
                                            "baudrate": "115200",
                                            "username": "board",
                                            "password": "secret",
                                        },
                                        "permissions": {
                                            "read": True,
                                            "write": True,
                                            "reset": True,
                                        },
                                    },
                                    {
                                        "type": "telnet",
                                        "settings": {
                                            "host": "192.168.1.20",
                                            "port": "23",
                                            "username": "admin",
                                            "password": "telnetpw",
                                        },
                                        "permissions": {
                                            "read": True,
                                            "write": False,
                                        },
                                    },
                                ],
                            },
                        },
                    )
                )
                body = json_response_body(response)
                task_id = body["task"]["id"]
                skills_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task/{task_id}/skills",
                        method="POST",
                        payload={
                            "enabled_paths": ["/repo/.aha/skills/board-debug/SKILL.md", "/repo/.aha/skills/log/SKILL.md"],
                        },
                    )
                )
                skills_body = json_response_body(skills_response)
                update_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task/{task_id}/hardware-debug",
                        method="POST",
                        payload={
                            "channels": [
                                {
                                    "type": "nfs",
                                    "settings": {
                                        "server": "192.168.1.10",
                                        "remote_path": "/srv/nfs/rootfs",
                                        "mount_path": "/mnt/rootfs",
                                    },
                                    "permissions": {
                                        "read": True,
                                        "write": False,
                                        "reset": False,
                                    },
                                },
                            ],
                        },
                    )
                )
                update_body = json_response_body(update_response)
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)
                hardware_events = [event for event in events if event["type"] == "task_hardware_debug_config_updated"]
                skills_events = [event for event in events if event["type"] == "task_skills_config_updated"]

        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["task_skills"]["enabled_paths"], ["/repo/.aha/skills/board-debug/SKILL.md"])
        hardware = body["task"]["hardware_debug"]
        self.assertEqual([channel["type"] for channel in hardware["channels"]], ["uart", "telnet"])
        self.assertEqual(
            hardware["channels"][0]["settings"],
            {"port": "/dev/ttyUSB0", "baudrate": 115200, "username": "board", "password": "secret"},
        )
        self.assertNotIn("operation_skill_path", hardware["channels"][0])
        self.assertTrue(hardware["channels"][0]["permissions"]["read"])
        self.assertTrue(hardware["channels"][0]["permissions"]["write"])
        self.assertNotIn("reset", hardware["channels"][0]["permissions"])
        self.assertEqual(
            hardware["channels"][1]["settings"],
            {"host": "192.168.1.20", "port": 23, "username": "admin", "password": "telnetpw"},
        )
        self.assertTrue(hardware["enabled"])
        self.assertNotIn("devices", hardware)
        self.assertTrue(update_body["ok"])
        self.assertTrue(update_body["task"]["hardware_debug"]["enabled"])
        self.assertTrue(skills_body["ok"])
        self.assertEqual(skills_body["task"]["task_skills"]["enabled_paths"], ["/repo/.aha/skills/board-debug/SKILL.md", "/repo/.aha/skills/log/SKILL.md"])
        self.assertEqual(update_body["task"]["hardware_debug"]["channels"][0]["type"], "nfs")
        self.assertNotIn("operation_skill_path", update_body["task"]["hardware_debug"]["channels"][0])
        self.assertTrue(update_body["task"]["hardware_debug"]["channels"][0]["permissions"]["read"])
        self.assertFalse(update_body["task"]["hardware_debug"]["channels"][0]["permissions"]["write"])
        self.assertNotIn("reset", update_body["task"]["hardware_debug"]["channels"][0]["permissions"])
        self.assertEqual(hardware_events[-1]["data"]["task_id"], task_id)
        self.assertEqual(hardware_events[-1]["data"]["channel_count"], 1)
        self.assertEqual(hardware_events[-1]["data"]["channel_types"], ["nfs"])
        self.assertEqual(skills_events[-1]["data"]["task_id"], task_id)
        self.assertEqual(skills_events[-1]["data"]["skill_count"], 2)

    def test_api_task_hardware_io_records_are_persisted_and_streamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Hardware io", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/hardware-io",
                        method="POST",
                        payload={
                            "agent_id": "main",
                            "channel": "uart",
                            "endpoint": "/dev/ttyUSB0@115200",
                            "direction": "tx",
                            "data": "reset\\r",
                        },
                    )
                )
                body = json_response_body(response)
                page_response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/hardware-io?limit=10"))
                page_body = json_response_body(page_response)
                hardware_rows, _ = iter_jsonl_from(run_dir(root, run_id) / "tasks" / "task-001" / "hardware_io.jsonl", 0)
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)

        self.assertTrue(body["ok"])
        self.assertEqual(body["record"]["task_id"], "task-001")
        self.assertEqual(body["record"]["channel"], "uart")
        self.assertEqual(body["record"]["direction"], "tx")
        self.assertEqual(body["record"]["data"], "reset\\r")
        # GET now serves the device-level stream; this task has no device configured.
        self.assertIsNone(page_body["device"])
        self.assertEqual(page_body["events"], [])
        self.assertEqual(hardware_rows[0]["agent_id"], "main")
        hardware_events = [event for event in events if event["type"] == "hardware_io"]
        self.assertEqual(hardware_events[-1]["data"]["task_id"], "task-001")
        self.assertEqual(hardware_events[-1]["data"]["offset"], body["record"]["offset"])

    def test_hardware_console_uses_device_bridge(self) -> None:
        import subprocess

        from aha_cli.services.hardware_bridge import device_bridge_state_path, device_control_path
        from aha_cli.store.filesystem import complete_task, update_task_hardware_debug_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = "/dev/fake-bridge-test"
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                with mock.patch("pathlib.Path.cwd", return_value=root):
                    self.run_cli("init", "--portable", "--backend", "codex")
                    code, plan_output = self.run_cli("plan", "Bridge console", "--agents", "1")
                    self.assertEqual(code, 0)
                    run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                    update_task_hardware_debug_config(
                        root, run_id, "task-001",
                        channels=[{"type": "uart", "settings": {"port": device, "baudrate": 115200}}],
                    )
                    # Pretend a live bridge already owns the device (so the route reuses it,
                    # never spawning a real serial daemon during the test).
                    state_path = device_bridge_state_path(root, device)
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    state_path.write_text(json.dumps({"device": device, "pid": sleeper.pid, "status": "running"}), encoding="utf-8")

                    stream = json_response_body(asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/hardware-io")))
                    send = json_response_body(asyncio.run(fetch_ui_response(
                        root, run_id, "/api/task/task-001/hardware-send",
                        method="POST", payload={"data": "ps\\r"},
                    )))
                    pause = json_response_body(asyncio.run(fetch_ui_response(
                        root, run_id, "/api/task/task-001/hardware-pause", method="POST", payload={},
                    )))
                    resume = json_response_body(asyncio.run(fetch_ui_response(
                        root, run_id, "/api/task/task-001/hardware-resume", method="POST", payload={},
                    )))
                    control_rows, _ = iter_jsonl_from(device_control_path(root, device), 0)

                    # Terminal task -> read-only console, sends rejected.
                    complete_task(root, run_id, "task-001")
                    session = json_response_body(asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/hardware-session")))
                    blocked = asyncio.run(fetch_ui_response(
                        root, run_id, "/api/task/task-001/hardware-send",
                        method="POST", payload={"data": "x\\r"},
                    ))
            finally:
                sleeper.terminate()

        self.assertEqual(stream["device"], device)
        self.assertTrue(stream["bridge"]["alive"])
        self.assertFalse(stream["read_only"])
        self.assertTrue(send["ok"])
        self.assertTrue(pause["ok"])
        self.assertEqual(pause["command"], "pause")
        self.assertTrue(resume["ok"])
        self.assertEqual(resume["command"], "resume")
        cmds = [row["cmd"] for row in control_rows]
        self.assertEqual(cmds, ["send", "pause", "resume"])
        send_row = control_rows[0]
        self.assertEqual(send_row["data"], "ps\\r")
        self.assertTrue(session["read_only"])
        self.assertIn(b"409", blocked.split(b"\r\n", 1)[0])

    def test_task_memo_api_crud_and_task_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task memo run", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                asset_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memo-assets",
                        method="POST",
                        payload={
                            "filename": "clip.png",
                            "content_type": "image/png",
                            "data_url": "data:image/png;base64,iVBORw0KGgo=",
                        },
                    )
                )
                asset = json_response_body(asset_response)["asset"]
                svg_asset_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memo-assets",
                        method="POST",
                        payload={
                            "filename": "diagram.svg",
                            "content_type": "image/svg+xml",
                            "data_url": "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjwvc3ZnPg==",
                        },
                    )
                )
                svg_asset = json_response_body(svg_asset_response)["asset"]
                heic_asset_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memo-assets",
                        method="POST",
                        payload={
                            "filename": "camera.HEIC",
                            "content_type": "",
                            "data_url": "data:application/octet-stream;base64,aGVpYw==",
                        },
                    )
                )
                heic_asset = json_response_body(heic_asset_response)["asset"]
                boundary = "----aha-memo-boundary"
                multipart_body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="filename"\r\n\r\n'
                    "camera.avif\r\n"
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="content_type"\r\n\r\n'
                    "image/avif\r\n"
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="image"; filename="camera.avif"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8") + b"avif-bytes" + f"\r\n--{boundary}--\r\n".encode("utf-8")
                multipart_asset_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memo-assets",
                        method="POST",
                        body=multipart_body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    )
                )
                multipart_asset = json_response_body(multipart_asset_response)["asset"]
                heic_asset_fetch_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/task-memo-assets/{heic_asset['filename']}?run_id={run_id}")
                )
                heic_asset_fetch_headers, heic_asset_fetch_body = heic_asset_fetch_response.split(b"\r\n\r\n", 1)
                multipart_asset_fetch_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/task-memo-assets/{multipart_asset['filename']}?run_id={run_id}")
                )
                multipart_asset_fetch_headers, multipart_asset_fetch_body = multipart_asset_fetch_response.split(b"\r\n\r\n", 1)
                attachment_boundary = "----aha-memo-attachment-boundary"
                attachment_body = (
                    f"--{attachment_boundary}\r\n"
                    'Content-Disposition: form-data; name="filename"\r\n\r\n'
                    "notes.pdf\r\n"
                    f"--{attachment_boundary}\r\n"
                    'Content-Disposition: form-data; name="content_type"\r\n\r\n'
                    "application/pdf\r\n"
                    f"--{attachment_boundary}\r\n"
                    'Content-Disposition: form-data; name="image"; filename="notes.pdf"\r\n'
                    "Content-Type: application/pdf\r\n\r\n"
                ).encode("utf-8") + b"%PDF-1.4\nmemo" + f"\r\n--{attachment_boundary}--\r\n".encode("utf-8")
                attachment_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memo-assets",
                        method="POST",
                        body=attachment_body,
                        headers={"Content-Type": f"multipart/form-data; boundary={attachment_boundary}"},
                    )
                )
                attachment = json_response_body(attachment_response)["asset"]
                attachment_fetch_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/task-memo-assets/{attachment['filename']}?run_id={run_id}")
                )
                attachment_fetch_headers, attachment_fetch_body = attachment_fetch_response.split(b"\r\n\r\n", 1)
                asset_fetch_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/task-memo-assets/{asset['filename']}?run_id={run_id}")
                )
                asset_fetch_headers, asset_fetch_body = asset_fetch_response.split(b"\r\n\r\n", 1)
                svg_asset_fetch_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/task-memo-assets/{svg_asset['filename']}?run_id={run_id}")
                )
                svg_asset_fetch_headers, svg_asset_fetch_body = svg_asset_fetch_response.split(b"\r\n\r\n", 1)
                semantic_svg_path = run_dir(root, run_id) / "task_memo_assets" / "vega_pipeline" / "current_pipeline.svg"
                semantic_svg_path.parent.mkdir(parents=True, exist_ok=True)
                semantic_svg_path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
                semantic_svg_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memo-assets/vega_pipeline%2Fcurrent_pipeline.svg?run_id={run_id}",
                    )
                )
                semantic_svg_headers, semantic_svg_body = semantic_svg_response.split(b"\r\n\r\n", 1)
                create_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memos",
                        method="POST",
                        payload={
                            "title": "Memo title",
                            "description": f"Memo detail\n\n{asset['markdown']}",
                            "scheduled_date": "2026-06-05",
                            "end_date": "2026-06-08",
                            "status": "todo",
                            "backend": "codex",
                            "workflow_template": "feature",
                        },
                    )
                )
                created = json_response_body(create_response)["memo"]
                memo_id = created["id"]
                update_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{memo_id}",
                        method="PATCH",
                        payload={"status": "doing", "description": "Updated detail", "end_date": "2026-06-09"},
                    )
                )
                updated = json_response_body(update_response)["memo"]
                task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": updated["title"],
                            "description": updated["description"],
                            "backend": updated["backend"],
                            "workflow_template": updated["workflow_template"],
                            "source_memo_id": memo_id,
                            "dispatch": False,
                        },
                    )
                )
                task_body = json_response_body(task_response)
                clear_link_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{memo_id}",
                        method="PATCH",
                        payload={"created_task_id": ""},
                    )
                )
                clear_link_body = json_response_body(clear_link_response)["memo"]
                relink_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{memo_id}",
                        method="PATCH",
                        payload={"created_task_id": task_body["task"]["id"]},
                    )
                )
                relinked = json_response_body(relink_response)["memo"]
                unlinked_query_response = asyncio.run(
                    fetch_ui_response(root, run_id, "/api/task-memos?status=active&linked=unlinked&limit=50")
                )
                unlinked_query = json_response_body(unlinked_query_response)
                include_query_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/task-memos?status=active&linked=unlinked&include_id={memo_id}&limit=50")
                )
                include_query = json_response_body(include_query_response)
                search_query_response = asyncio.run(
                    fetch_ui_response(root, run_id, "/api/task-memos?q=2026-06-09&linked=linked&limit=1")
                )
                search_query = json_response_body(search_query_response)
                task_options_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-options?filter=all&include_id={task_body['task']['id']}&limit=5",
                    )
                )
                task_options = json_response_body(task_options_response)
                list_response = asyncio.run(fetch_ui_response(root, run_id, "/api/task-memos"))
                memos = json_response_body(list_response)["memos"]
                done_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{memo_id}",
                        method="PATCH",
                        payload={"status": "done", "completed_at": "2026-06-07"},
                    )
                )
                done_body = json_response_body(done_response)["memo"]
                closed_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{memo_id}",
                        method="PATCH",
                        payload={"status": "closed", "closed_at": "2026-06-10"},
                    )
                )
                closed_body = json_response_body(closed_response)["memo"]
                invalid_date_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memos",
                        method="POST",
                        payload={
                            "title": "Future memo",
                            "scheduled_date": "2099-02-10",
                            "end_date": "2099-02-09",
                            "status": "done",
                            "completed_at": "2099-02-01",
                        },
                    )
                )
                invalid_date_body = json_response_body(invalid_date_response)["memo"]
                invalid_closed_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{invalid_date_body['id']}",
                        method="PATCH",
                        payload={"status": "closed", "closed_at": "2099-02-01"},
                    )
                )
                invalid_closed_body = json_response_body(invalid_closed_response)["memo"]
                delete_response = asyncio.run(fetch_ui_response(root, run_id, f"/api/task-memos/{memo_id}", method="DELETE"))

        self.assertEqual(created["title"], "Memo title")
        self.assertTrue(asset_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertIn("/", asset["filename"])
        self.assertEqual(asset["path"], f"task_memo_assets/{asset['filename']}")
        self.assertIn(asset["path"], asset["markdown"])
        self.assertIn(asset["markdown"], created["description"])
        self.assertTrue(asset_fetch_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"Content-Type: image/png", asset_fetch_headers)
        self.assertEqual(asset_fetch_body, b"\x89PNG\r\n\x1a\n")
        self.assertTrue(svg_asset_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(svg_asset["content_type"], "image/svg+xml")
        self.assertTrue(svg_asset["filename"].endswith(".svg"))
        self.assertIn(b"Content-Type: image/svg+xml", svg_asset_fetch_headers)
        self.assertEqual(svg_asset_fetch_body, b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        self.assertTrue(semantic_svg_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"Content-Type: image/svg+xml", semantic_svg_headers)
        self.assertEqual(semantic_svg_body, b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        self.assertTrue(heic_asset_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(heic_asset["content_type"], "image/heic")
        self.assertTrue(heic_asset["filename"].endswith(".heic"))
        self.assertIn(b"Content-Type: image/heic", heic_asset_fetch_headers)
        self.assertEqual(heic_asset_fetch_body, b"heic")
        self.assertTrue(multipart_asset_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(multipart_asset["content_type"], "image/avif")
        self.assertTrue(multipart_asset["filename"].endswith(".avif"))
        self.assertEqual(multipart_asset["bytes"], len(b"avif-bytes"))
        self.assertIn(b"Content-Type: image/avif", multipart_asset_fetch_headers)
        self.assertEqual(multipart_asset_fetch_body, b"avif-bytes")
        self.assertTrue(attachment_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(attachment["content_type"], "application/pdf")
        self.assertEqual(attachment["kind"], "attachment")
        self.assertIn("/", attachment["filename"])
        self.assertTrue(attachment["filename"].endswith(".pdf"))
        self.assertIn("[Attachment: notes.pdf]", attachment["markdown"])
        self.assertIn(b"Content-Type: application/pdf", attachment_fetch_headers)
        self.assertIn(b'Content-Disposition: attachment; filename="', attachment_fetch_headers)
        self.assertEqual(attachment_fetch_body, b"%PDF-1.4\nmemo")
        self.assertEqual(created["scheduled_date"], "2026-06-05")
        self.assertEqual(created["end_date"], "2026-06-08")
        self.assertEqual(updated["status"], "doing")
        self.assertEqual(updated["description"], "Updated detail")
        self.assertEqual(updated["end_date"], "2026-06-09")
        self.assertEqual(task_body["memo"]["status"], "doing")
        self.assertEqual(task_body["memo"]["created_task_id"], task_body["task"]["id"])
        self.assertEqual(task_body["memo"]["created_task_status"], task_body["task"]["status"])
        self.assertEqual(clear_link_body["created_task_id"], "")
        self.assertEqual(clear_link_body["created_task_status"], "")
        self.assertEqual(clear_link_body["converted_at"], "")
        self.assertEqual(relinked["created_task_id"], task_body["task"]["id"])
        self.assertEqual(relinked["created_task_status"], task_body["task"]["status"])
        self.assertEqual(unlinked_query["memos"], [])
        self.assertEqual(include_query["memos"][0]["id"], memo_id)
        self.assertEqual(search_query["total"], 1)
        self.assertEqual(search_query["memos"][0]["id"], memo_id)
        self.assertTrue(task_options_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(task_options["tasks"][0]["id"], task_body["task"]["id"])
        self.assertEqual(task_options["tasks"][0]["title"], task_body["task"]["title"])
        self.assertEqual(task_options["tasks"][0]["display_status"], task_body["task"]["status"])
        self.assertNotIn("agents", task_options["tasks"][0])
        self.assertEqual(memos[0]["status"], "doing")
        self.assertEqual(memos[0]["created_task_status"], task_body["task"]["status"])
        self.assertEqual(done_body["status"], "done")
        self.assertEqual(done_body["completed_at"], "2026-06-07")
        self.assertEqual(done_body["closed_at"], "")
        self.assertEqual(closed_body["status"], "closed")
        self.assertEqual(closed_body["completed_at"], "")
        self.assertEqual(closed_body["closed_at"], "2026-06-10")
        self.assertEqual(invalid_date_body["scheduled_date"], "2099-02-10")
        self.assertEqual(invalid_date_body["end_date"], "")
        self.assertEqual(invalid_date_body["completed_at"], "2099-02-10")
        self.assertEqual(invalid_closed_body["status"], "closed")
        self.assertEqual(invalid_closed_body["completed_at"], "")
        self.assertEqual(invalid_closed_body["closed_at"], "2099-02-10")
        self.assertTrue(json_response_body(delete_response)["memo"]["deleted"])

    def test_task_memo_attachment_directory_is_added_to_linked_task_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Memo attachment note", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                asset_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memo-assets",
                        method="POST",
                        payload={
                            "filename": "screenshot.png",
                            "content_type": "image/png",
                            "data_url": "data:image/png;base64,iVBORw0KGgo=",
                        },
                    )
                )
                asset = json_response_body(asset_response)["asset"]
                memo_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memos",
                        method="POST",
                        payload={
                            "title": "Attachment memo",
                            "description": f"Please inspect this.\n\n{asset['markdown']}",
                        },
                    )
                )
                memo = json_response_body(memo_response)["memo"]
                task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": memo["title"],
                            "description": memo["description"],
                            "source_memo_id": memo["id"],
                            "dispatch": False,
                        },
                    )
                )
                task = json_response_body(task_response)["task"]
                context = task_context_snapshot(root, run_id, task["id"])

        asset_dir = str((run_dir(root, run_id) / "task_memo_assets").resolve())
        self.assertIn("AHA memo attachment resolution:", task["description"])
        self.assertIn(asset_dir, task["description"])
        self.assertIn("do not search for them relative to the workspace", context["prompt"])

    def test_task_memo_completion_can_complete_linked_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Memo final", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                unlinked_memo_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memos",
                        method="POST",
                        payload={"title": "Unlinked memo", "description": "No task"},
                    )
                )
                unlinked_memo = json_response_body(unlinked_memo_response)["memo"]
                unlinked_done_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{unlinked_memo['id']}",
                        method="PATCH",
                        payload={"status": "done"},
                    )
                )
                memo_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memos",
                        method="POST",
                        payload={"title": "Report memo", "description": "Initial memo"},
                    )
                )
                memo = json_response_body(memo_response)["memo"]
                task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Linked task",
                            "description": memo["description"],
                            "source_memo_id": memo["id"],
                            "dispatch": False,
                        },
                    )
                )
                linked_task = json_response_body(task_response)["task"]
                no_sync_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{memo['id']}",
                        method="PATCH",
                        payload={"status": "done"},
                    )
                )
                sync_memo_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-memos",
                        method="POST",
                        payload={"title": "Sync memo", "description": "Finish linked task"},
                    )
                )
                sync_memo = json_response_body(sync_memo_response)["memo"]
                sync_task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Sync linked task",
                            "description": sync_memo["description"],
                            "source_memo_id": sync_memo["id"],
                            "dispatch": False,
                        },
                    )
                )
                sync_task = json_response_body(sync_task_response)["task"]
                done_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/task-memos/{sync_memo['id']}",
                        method="PATCH",
                        payload={"status": "done", "complete_linked_task": True},
                    )
                )
                done_body = json_response_body(done_response)
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

                memos_response = asyncio.run(fetch_ui_response(root, run_id, "/api/task-memos?status=all&limit=20"))
                memos = json_response_body(memos_response)["memos"]
                completed_memo = next(item for item in memos if item["id"] == sync_memo["id"])
                no_sync_task_detail = task_snapshot(root, run_id, linked_task["id"])
                task_detail = task_snapshot(root, run_id, sync_task["id"])

        self.assertTrue(json_response_body(unlinked_done_response)["ok"])
        self.assertNotIn("linked_task_completion", json_response_body(unlinked_done_response))
        self.assertTrue(json_response_body(no_sync_response)["ok"])
        self.assertNotIn("linked_task_completion", json_response_body(no_sync_response))
        self.assertEqual(no_sync_task_detail["task"]["status"], "pending")
        self.assertEqual(done_body["memo"]["status"], "done")
        self.assertEqual(done_body["linked_task_completion"]["completed"], True)
        self.assertEqual(done_body["linked_task_completion"]["task_id"], sync_task["id"])
        self.assertEqual(messages, [])
        self.assertEqual(completed_memo["status"], "done")
        self.assertEqual(task_detail["task"]["status"], "completed")
        self.assertEqual(task_detail["result"], "")

    def test_ui_state_persists_selected_memo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Memo UI state", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/ui-state",
                        method="PATCH",
                        payload={"last_selected_memo_id": "memo-123"},
                    )
                )
                read_response = asyncio.run(fetch_ui_response(root, run_id, f"/api/ui-state?run_id={run_id}"))

        update_body = json_response_body(update_response)
        read_body = json_response_body(read_response)
        self.assertTrue(update_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(update_body["run_id"], run_id)
        self.assertEqual(update_body["last_selected_memo_id"], "memo-123")
        self.assertEqual(read_body["last_selected_memo_id"], "memo-123")

    def test_task_memo_list_enriches_only_returned_page_without_search(self) -> None:
        from aha_cli.web import task_routes as task_routes_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Memo list page", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(12):
                    create_task_memo(
                        root,
                        run_id,
                        {
                            "title": f"Memo {index}",
                            "scheduled_date": f"2026-06-{index + 1:02d}",
                            "status": "todo",
                            "created_task_id": "task-001",
                        },
                    )

                with mock.patch.object(task_routes_module, "enrich_task_memo", wraps=task_routes_module.enrich_task_memo) as enrich:
                    response = asyncio.run(fetch_ui_response(root, run_id, "/api/task-memos?limit=5"))
                    body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["total"], 12)
        self.assertEqual(len(body["memos"]), 5)
        self.assertEqual(enrich.call_count, 5)
        self.assertTrue(all(item["created_task_status"] for item in body["memos"]))

    def test_api_task_create_rejects_unknown_execution_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Invalid execution fields", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                bad_mode = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={"title": "Bad mode", "collaboration_mode": "crowd", "dispatch": False},
                    )
                )
                bad_workflow = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={"title": "Bad workflow", "workflow_template": "unknown", "dispatch": False},
                    )
                )

        self.assertTrue(bad_mode.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn("unknown collaboration mode: crowd", json_response_body(bad_mode)["error"])
        self.assertTrue(bad_workflow.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn("unknown workflow template: unknown", json_response_body(bad_workflow)["error"])

    def test_api_task_create_accepts_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Create supervised task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Supervised task",
                            "dispatch": False,
                            "supervision": {
                                "mode": "assisted",
                                "host_backend": "codex",
                                "real_agent_enabled": True,
                                "max_rounds": 9,
                                "ask_user_gates": {
                                    "real_ui_validation": False,
                                    "commit_merge_delete": False,
                                },
                            },
                        },
                    )
                )
                body = json_response_body(response)
                task = status_snapshot(root, run_id)["tasks"][-1]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["supervision"]["mode"], "assisted")
        self.assertEqual(body["task"]["supervision"]["host_backend"], "codex")
        self.assertEqual(body["task"]["supervision"]["host_agent_id"], "host")
        self.assertTrue(body["task"]["supervision"]["real_agent_enabled"])
        self.assertEqual(body["task"]["supervision"]["max_rounds"], 9)
        self.assertFalse(body["task"]["supervision"]["ask_user_gates"]["real_ui_validation"])
        self.assertFalse(body["task"]["supervision"]["ask_user_gates"]["commit_merge_delete"])
        self.assertFalse(body["task"]["supervision"]["ask_user_gates"]["scope_change"])
        self.assertEqual(task["supervision"], body["task"]["supervision"])
        self.assertTrue(any(agent["id"] == "host" and agent["role"] == "host" and agent["backend"] == "codex" for agent in task["agents"]))

    def test_api_task_supervision_config_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Supervision API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                initial = status_snapshot(root, run_id)["tasks"][0]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/supervision",
                        method="POST",
                        payload={
                            "mode": "assisted",
                            "max_rounds": 7,
                            "ask_user_gates": {
                                "scope_change": False,
                                "product_preference": False,
                            },
                        },
                    )
                )
                body = json_response_body(response)
                updated = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(initial["supervision"]["mode"], "manual")
        self.assertEqual(initial["supervision"]["channel"], "main_only")
        self.assertFalse(initial["supervision"]["real_agent_enabled"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["supervision"]["mode"], "assisted")
        self.assertEqual(body["task"]["supervision"]["host_backend"], "stub")
        self.assertFalse(body["task"]["supervision"]["real_agent_enabled"])
        self.assertEqual(body["task"]["supervision"]["max_rounds"], 7)
        self.assertFalse(body["task"]["supervision"]["ask_user_gates"]["scope_change"])
        self.assertFalse(body["task"]["supervision"]["ask_user_gates"]["product_preference"])
        self.assertFalse(body["task"]["supervision"]["ask_user_gates"]["commit_merge_delete"])
        self.assertNotIn("allowed_actions", body["task"]["supervision"])
        self.assertEqual(updated["supervision"], body["task"]["supervision"])

    def test_disabling_supervision_clears_main_host_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Disable supervision wait", "--agents", "1")
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
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
                set_agent_status(root, run_id, "task-001", "host", "completed", 0)

                task = update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="manual",
                    host_backend="stub",
                    real_agent_enabled=False,
                )
                rows, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)

        main_agent = next(agent for agent in task["agents"] if agent["id"] == "main")
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(main_agent["status"], "completed")
        self.assertNotIn("waiting_reason", main_agent)
        self.assertTrue(any(row["type"] == "task_supervision_host_wait_cleared" for row in rows))

    def test_task_supervision_host_agent_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Supervision host", "--agents", "1")
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

        host = next(agent for agent in task["agents"] if agent["role"] == "host")
        self.assertEqual(host["backend"], "claude")
        self.assertEqual(host["workspace_path"], task["workspace_path"])
        self.assertEqual(host["sandbox"], "read-only")
        self.assertEqual(host["approval"], "never")
        self.assertEqual(task["supervision"]["host_agent_id"], "host")

    def test_api_agent_config_switches_sub_backend_with_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Switch sub backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub")
                session = ensure_session(root, run_id, "task-001", sub["id"], "codex", model="gpt-5.5")
                session["backend_session_id"] = "old-codex-session"
                save_session(root, session)
                append_event(
                    root,
                    run_id,
                    "agent_usage",
                    {
                        "task_id": "task-001",
                        "target": sub["id"],
                        "usage": {"input_tokens": 200, "cache_creation_input_tokens": 40, "output_tokens": 75},
                    },
                )

                with (
                    mock.patch("aha_cli.services.agent_backend_switch.backend_status", return_value={"status": "running", "pid": 123}),
                    mock.patch("aha_cli.services.agent_backend_switch.stop_backend", return_value={"status": "stopped", "pid": 123}) as stop_backend,
                    mock.patch("aha_cli.services.agent_backend_switch.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/agent-config",
                            method="POST",
                            payload={"task_id": "task-001", "agent_id": sub["id"], "backend": "claude"},
                        )
                    )
                body = json_response_body(response)
                task = status_snapshot(root, run_id)["tasks"][0]
                updated_sub = next(agent for agent in task["agents"] if agent["id"] == sub["id"])
                updated_session = ensure_session(root, run_id, "task-001", sub["id"], "claude")
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub["id"]), 0)
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["agent"]["backend"], "claude")
        self.assertEqual(body["task"]["id"], "task-001")
        self.assertEqual(next(agent for agent in body["task"]["agents"] if agent["id"] == sub["id"])["backend"], "claude")
        self.assertEqual(updated_sub["backend"], "claude")
        self.assertEqual(updated_session["backend"], "claude")
        self.assertIsNone(updated_session["backend_session_id"])
        self.assertEqual(updated_session["history_backend_sessions"][-1]["backend_session_id"], "old-codex-session")
        self.assertEqual(updated_session["history_backend_sessions"][-1]["reason"], "backend_changed")
        self.assertEqual(updated_session["history_backend_sessions"][-1]["token_summary"]["total_tokens"], 275)
        self.assertEqual(updated_session["history_backend_sessions"][-1]["token_summary"]["cached_tokens"], 40)
        self.assertEqual(updated_session["compact_summary"]["reason"], "backend_switch")
        handoff_message = next(message.get("message", "") for message in messages if message.get("coordination") == "backend_switch")
        self.assertIn("previous backend: codex", handoff_message)
        self.assertIn(str(root), handoff_message)
        self.assertIn("/tasks/task-001/compacts/", handoff_message)
        self.assertNotIn("handoff summary: `tasks/", handoff_message)
        self.assertTrue(any(event["type"] == "backend_session_reset" and event["data"].get("agent_id") == sub["id"] for event in events))
        self.assertTrue(any(event["type"] == "agent_backend_switched" and event["data"].get("new_backend") == "claude" for event in events))
        stop_backend.assert_called_once()
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, sub["id"]))
        self.assertEqual(start_backend.call_args.kwargs["backend"], "claude")
        self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")

    def test_agent_backend_switch_for_main_updates_task_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Switch main backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/agent-config",
                        method="POST",
                        payload={"task_id": "task-001", "agent_id": "main", "backend": "claude"},
                    )
                )
                body = json_response_body(response)
                task = status_snapshot(root, run_id)["tasks"][0]
                main_agent = next(agent for agent in task["agents"] if agent["id"] == "main")

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["task"]["preferred_backend"], "claude")
        self.assertEqual(next(agent for agent in body["task"]["agents"] if agent["id"] == "main")["backend"], "claude")
        self.assertEqual(main_agent["backend"], "claude")
        self.assertEqual(task["preferred_backend"], "claude")
        self.assertIsNone(task.get("preferred_model"))

    def test_agent_config_model_change_uses_backend_switch_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Switch model", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/agent-config",
                        method="POST",
                        payload={"task_id": "task-001", "agent_id": "main", "backend": "claude", "model": "env:work"},
                    )
                )
                task = status_snapshot(root, run_id)["tasks"][0]
                main_agent = next(agent for agent in task["agents"] if agent["id"] == "main")

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(main_agent["backend"], "claude")
        self.assertEqual(main_agent["model"], "env:work")
        self.assertEqual(task["preferred_backend"], "claude")
        self.assertEqual(task["preferred_model"], "env:work")

    def test_agent_config_can_restart_backend_after_runtime_setting_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Restart backend config", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with (
                    mock.patch("aha_cli.services.agent_backend_switch.backend_status", return_value={"status": "running", "pid": 456}),
                    mock.patch("aha_cli.services.agent_backend_switch.stop_backend", return_value={"status": "stopped", "pid": 456}) as stop_backend,
                    mock.patch("aha_cli.services.agent_backend_switch.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/agent-config",
                            method="POST",
                            payload={
                                "task_id": "task-001",
                                "agent_id": "main",
                                "sandbox": "read-only",
                                "restart_backend": True,
                            },
                        )
                    )
                task = status_snapshot(root, run_id)["tasks"][0]
                main_agent = next(agent for agent in task["agents"] if agent["id"] == "main")
                events, _ = iter_jsonl_from(run_dir(root, run_id) / "events.jsonl", 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(main_agent["sandbox"], "read-only")
        stop_backend.assert_called_once()
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.kwargs["sandbox"], "read-only")
        self.assertTrue(any(event["type"] == "agent_backend_restarted" and event["data"].get("agent_id") == "main" for event in events))

    def test_supervision_host_backend_switch_uses_handoff_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Switch host backend", "--agents", "1")
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
                session = ensure_session(root, run_id, "task-001", "host", "codex")
                session["backend_session_id"] = "old-host-session"
                save_session(root, session)

                with (
                    mock.patch("aha_cli.services.agent_backend_switch.backend_status", return_value={"status": "running", "pid": 321}),
                    mock.patch("aha_cli.services.agent_backend_switch.stop_backend", return_value={"status": "stopped", "pid": 321}) as stop_backend,
                    mock.patch("aha_cli.services.agent_backend_switch.start_backend", return_value={"status": "running", "started": True}),
                ):
                    update_task_supervision_config(root, run_id, "task-001", host_backend="claude")
                task = status_snapshot(root, run_id)["tasks"][0]
                host = next(agent for agent in task["agents"] if agent["id"] == "host")
                updated_session = ensure_session(root, run_id, "task-001", "host", "claude")

        self.assertEqual(task["supervision"]["host_backend"], "claude")
        self.assertEqual(host["backend"], "claude")
        self.assertIsNone(updated_session["backend_session_id"])
        self.assertEqual(updated_session["history_backend_sessions"][-1]["backend_session_id"], "old-host-session")
        stop_backend.assert_called_once()

    def test_send_to_supervision_host_stores_note_without_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host note", "--agents", "1")
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

                with mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start:
                    response = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "host",
                            "task_id": "task-001",
                            "role": "host",
                            "sender": "browser",
                            "from_agent": "browser",
                            "to_agent": "host",
                            "message": "后续收到测试消息后再决定是否让 main 继续。",
                        },
                    )
                host_inbox = inbox_path(root, run_id, "host")
                host_messages, _ = iter_jsonl_from(host_inbox, 0)
                offset = read_json(chat_offset_path(run_dir(root, run_id), "host", "task-001"))
                host_inbox_size = host_inbox.stat().st_size

        self.assertTrue(response["ok"])
        self.assertNotIn("backend", response)
        start.assert_not_called()
        self.assertEqual(host_messages[-1]["message"], "后续收到测试消息后再决定是否让 main 继续。")
        self.assertEqual(offset["offset"], host_inbox_size)

    def test_web_task_creation_queues_dispatched_main_backend_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task create autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                def fake_queue(_root: Path, _run_id: str, autostart: dict, *, from_start: bool = False) -> dict:
                    return {
                        "queued": True,
                        "target": autostart["target"],
                        "task_id": autostart["task_id"],
                        "backend": autostart["backend"],
                        "from_start": from_start,
                    }

                with mock.patch("aha_cli.web.task_runtime.queue_backend_start", side_effect=fake_queue) as queue_backend_start:
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
        self.assertNotIn("backend", body)
        self.assertTrue(body["backend_start"]["queued"])
        self.assertEqual(body["backend_start"]["target"], "main")
        self.assertEqual(body["backend_start"]["task_id"], body["task"]["id"])
        self.assertTrue(body["backend_start"]["from_start"])
        queue_backend_start.assert_called_once()
        self.assertEqual(queue_backend_start.call_args.args[:2], (root, run_id))
        self.assertTrue(queue_backend_start.call_args.kwargs["from_start"])

    def test_web_send_queues_stopped_backend_start_after_storing_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task send autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                def fake_queue(_root: Path, _run_id: str, autostart: dict, *, from_start: bool = False) -> dict:
                    return {
                        "queued": True,
                        "target": autostart["target"],
                        "task_id": autostart["task_id"],
                        "backend": autostart["backend"],
                        "from_start": from_start,
                    }

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_runtime.queue_backend_start", side_effect=fake_queue) as queue_backend_start,
                    mock.patch("aha_cli.web.task_messaging.start_backend") as start_backend,
                ):
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/send",
                            method="POST",
                            payload={
                                "target": "main",
                                "task_id": "task-001",
                                "role": "main",
                                "sender": "browser",
                                "from_agent": "browser",
                                "to_agent": "main",
                                "message": "continue",
                            },
                        )
                    )
                    body = json_response_body(response)
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertNotIn("backend", body)
        self.assertTrue(body["backend_start"]["queued"])
        self.assertEqual(body["backend_start"]["task_id"], "task-001")
        self.assertFalse(body["backend_start"]["from_start"])
        self.assertEqual(messages[-1]["message"], "continue")
        queue_backend_start.assert_called_once()
        start_backend.assert_not_called()

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

    def test_task_action_complete_marks_complete_without_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Direct complete API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.task_command_actions.stop_task_backends", return_value=[]) as stop_backends:
                    response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/complete", method="POST"))
                    body = json_response_body(response)
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "direct")
        self.assertEqual(body["task"]["status"], "completed")
        self.assertNotIn("message", body)
        self.assertNotIn("backend", body)
        self.assertEqual(main_messages, [])
        stop_backends.assert_called_once()

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

    def test_workspace_options_reads_project_roots_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            aha_home = base / ".aha"
            project_root = base / "projects"
            (project_root / "demo").mkdir(parents=True)
            aha_home.mkdir()
            (aha_home / "config.json").write_text(json.dumps({"workspace_roots": [str(project_root)]}), encoding="utf-8")

            options = workspace_options(aha_home=aha_home)

        self.assertEqual(
            options,
            [
                {
                    "name": "demo",
                    "label": "projects/demo",
                    "path": str(project_root / "demo"),
                    "root": str(project_root),
                },
            ],
        )

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

        self.assertEqual(snapshot["proxy"]["http_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(snapshot["proxy"]["https_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(snapshot["proxy"]["no_proxy"], "localhost,127.0.0.1")
        self.assertTrue(task["run_proxy_configured"])
        self.assertFalse(task["preferred_proxy_enabled"])
        self.assertTrue(agents["main"]["proxy_enabled"])
        self.assertFalse(agents["sub-001"]["proxy_enabled"])

    def test_task_create_uses_selected_backend_proxy_default_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                (root / ".aha" / "config.json").write_text(
                    json.dumps(
                        {
                            "backend": "codex",
                            "codex": {"proxy": {"enabled": False, "http_proxy": "http://codex.proxy:7890"}},
                            "claude": {"proxy": {"enabled": True, "http_proxy": "http://claude.proxy:7890"}},
                        }
                    ),
                    encoding="utf-8",
                )
                code, plan_output = self.run_cli("plan", "Backend proxy defaults", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={"title": "Claude task", "backend": "claude", "dispatch": False},
                    )
                )
                body = json_response_body(response)
                snapshot = status_snapshot(root, run_id)
                created = next(task for task in snapshot["tasks"] if task["id"] == body["task"]["id"])

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertTrue(body["task"]["agents"][0]["proxy_enabled"])
        self.assertTrue(created["run_proxy_configured"])

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
        self.assertEqual(body["proxy"]["http_proxy"], "http://proxy.local:8080")
        self.assertEqual(body["proxy"]["no_proxy"], "localhost,127.0.0.1")

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
        self.assertEqual(body["proxy"]["http_proxy"], "http://proxy.local:8080")

    def test_task_token_saving_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Token saving API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/token-saving",
                        method="POST",
                        payload={
                            "enabled": True,
                            "provider": "nav",
                        },
                    )
                )
                body = json_response_body(response)
                snapshot = status_snapshot(root, run_id)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["token_saving"]["enabled"])
        self.assertEqual(body["task"]["token_saving"]["provider"], "nav")
        self.assertEqual(snapshot["tasks"][0]["token_saving"], body["task"]["token_saving"])

    def test_task_token_saving_api_accepts_enable_without_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Token saving disabled integration API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/token-saving",
                        method="POST",
                        payload={
                            "enabled": True,
                            "provider": "nav",
                        },
                    )
                )
                body = json_response_body(response)
                snapshot = status_snapshot(root, run_id)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(snapshot["tasks"][0]["token_saving"]["enabled"])
        self.assertEqual(snapshot["tasks"][0]["token_saving"]["provider"], "nav")


if __name__ == "__main__":
    unittest.main()
