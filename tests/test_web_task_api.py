from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    add_agent,
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
    update_agent_config,
    update_task_proxy_config,
    update_task_supervision_config,
    write_task_result,
)
from aha_cli.store.sessions import ensure_session, save_session
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
        self.assertEqual(updated_sub["backend"], "claude")
        self.assertEqual(updated_session["backend"], "claude")
        self.assertIsNone(updated_session["backend_session_id"])
        self.assertEqual(updated_session["history_backend_sessions"][-1]["backend_session_id"], "old-codex-session")
        self.assertEqual(updated_session["history_backend_sessions"][-1]["reason"], "backend_changed")
        self.assertEqual(updated_session["compact_summary"]["reason"], "backend_switch")
        self.assertTrue(any(message.get("coordination") == "backend_switch" and "previous backend: codex" in message.get("message", "") for message in messages))
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
                task = status_snapshot(root, run_id)["tasks"][0]
                main_agent = next(agent for agent in task["agents"] if agent["id"] == "main")

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
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

    def test_web_task_creation_autostarts_dispatched_main_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task create autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running", "started": True}) as start_backend:
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

    def test_task_context_management_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Context management API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/context-management",
                        method="POST",
                        payload={
                            "auto_compact_enabled": True,
                            "auto_compact_threshold_percent": 82,
                        },
                    )
                )
                body = json_response_body(response)
                snapshot = status_snapshot(root, run_id)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["context_management"]["auto_compact_enabled"])
        self.assertEqual(body["task"]["context_management"]["auto_compact_threshold_percent"], 82)
        self.assertEqual(snapshot["tasks"][0]["context_management"], body["task"]["context_management"])


if __name__ == "__main__":
    unittest.main()
