from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.domain.models import is_service_assistant_run, is_service_assistant_task
from aha_cli.services.feishu import FeishuError
from aha_cli.services.feishu_notifications import set_subscription
from aha_cli.services.chat import action_schema_retry_message, chat_prompt
from aha_cli.services.run_delete import RunDeleteError, delete_run
from aha_cli.services.run_lifecycle_actions import RunLifecycleActionError, set_run_lifecycle_status
from aha_cli.services.run_retention import RunRetentionError, apply_run_retention
from aha_cli.services.orchestrator import execute_actions
from aha_cli.services import service_assistant_actions
from aha_cli.services.service_assistant import ensure_service_assistant_run, ensure_service_assistant_task
from aha_cli.services.service_assistant_actions import prepare_service_assistant_action, resolve_confirmation
from aha_cli.services.service_runtime import write_service_runtime
from aha_cli.store.filesystem import append_message, create_plan, delete_task, inbox_path, iter_jsonl_from
from aha_cli.store.paths import aha_home_path
from aha_cli.store.runs import require_plan
from aha_cli.store.task_memos import read_task_memos
from aha_cli.web.run_api import public_run_summaries
from aha_cli.web.task_messaging import message_backend_autostart_config


class ServiceAssistantTests(unittest.TestCase):
    def test_system_run_and_session_task_use_aha_home_and_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = ensure_service_assistant_run(root, {"backend": "stub"})
            task = ensure_service_assistant_task(root, run_id, "tenant:p2p:ou_user", {"backend": "stub"})
            plan = require_plan(root, run_id)

            self.assertTrue(is_service_assistant_run(plan))
            self.assertTrue(is_service_assistant_task(task))
            self.assertEqual(Path(plan["main_agent"]["workspace_path"]), aha_home_path(root).resolve())
            self.assertEqual(Path(task["workspace_path"]), aha_home_path(root).resolve())
            self.assertEqual(task["preferred_sandbox"], "read-only")
            self.assertEqual(task["preferred_approval"], "never")
            self.assertEqual(task["collaboration_mode"], "solo")
            self.assertEqual(task["max_sub_agents"], 0)
            self.assertEqual([item["id"] for item in public_run_summaries(root)], [run_id])

    def test_new_conversation_autostart_keeps_feishu_reasoning_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = ensure_service_assistant_run(root, {"backend": "codex", "reasoning_effort": "high"})
            task = ensure_service_assistant_task(
                root,
                run_id,
                "tenant:p2p:ou_user",
                {"backend": "codex", "reasoning_effort": "high"},
            )
            with mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}):
                autostart = message_backend_autostart_config(root, run_id, task["id"], "main")

            self.assertEqual(autostart["reasoning_effort"], "high")

    def test_legacy_feishu_run_is_migrated_and_old_tasks_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = create_plan(root, "Feishu Assistant", 1, "implementation", ["Legacy"], [], backend="stub")

            run_id = ensure_service_assistant_run(root, {"backend": "stub"})
            plan = require_plan(root, run_id)

            self.assertEqual(run_id, legacy["id"])
            self.assertTrue(is_service_assistant_run(plan))
            self.assertTrue(plan["tasks"][0]["hidden"])
            self.assertTrue(plan["tasks"][0]["assistant_legacy"])

    def test_read_action_continues_and_confirmed_memo_change_is_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assistant_run = ensure_service_assistant_run(root, {"backend": "stub"})
            assistant_task = ensure_service_assistant_task(root, assistant_run, "tenant:p2p:ou_user", {"backend": "stub"})
            target = create_plan(root, "User run", 1, "implementation", [], [], backend="stub", create_default_tasks=False)
            set_subscription(
                root,
                "tenant:p2p:ou_user",
                chat_id="oc_chat",
                open_id="ou_user",
                run_id=assistant_run,
                task_id=assistant_task["id"],
            )

            read_result = prepare_service_assistant_action(
                root,
                assistant_run,
                assistant_task,
                {"type": "service_assistant", "operation": "list_runs", "arguments": {}},
            )
            self.assertTrue(read_result["continuation"])
            self.assertIn(target["id"], read_result["tool_message"])
            self.assertNotIn(assistant_run, read_result["tool_message"])

            prepared = prepare_service_assistant_action(
                root,
                assistant_run,
                assistant_task,
                {
                    "type": "service_assistant",
                    "operation": "create_memo",
                    "arguments": {"run_id": target["id"], "title": "Check release"},
                },
            )
            columns = prepared["confirmation_card"]["body"]["elements"][1]["columns"]
            confirmed = resolve_confirmation(
                root,
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                text="确认",
            )

            self.assertEqual(prepared["confirmation_card"]["schema"], "2.0")
            self.assertTrue(prepared["confirmation_id"])
            self.assertEqual(columns[0]["elements"][0]["behaviors"][0]["type"], "callback")
            self.assertEqual(columns[1]["elements"][0]["behaviors"][0]["value"]["decision"], "cancel")
            self.assertNotIn("token", json.dumps(prepared["confirmation_card"], ensure_ascii=False).lower())
            self.assertNotIn("```json", json.dumps(prepared["confirmation_card"], ensure_ascii=False))
            self.assertTrue(confirmed["result"]["ok"])
            self.assertEqual(confirmed["confirmation_id"], prepared["confirmation_id"])
            self.assertEqual(confirmed["confirmation_card"]["header"]["template"], "green")
            self.assertEqual(read_task_memos(root, target["id"])[0]["title"], "Check release")
            with self.assertRaises(FeishuError):
                resolve_confirmation(
                    root,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    text="确认",
                )

    def test_orchestrator_routes_read_result_back_to_the_same_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assistant_run = ensure_service_assistant_run(root, {"backend": "stub"})
            assistant_task = ensure_service_assistant_task(root, assistant_run, "tenant:p2p:ou_user", {"backend": "stub"})
            reply = json.dumps(
                {
                    "actions": [{"type": "service_assistant", "operation": "service_status", "arguments": {}}],
                    "response": "",
                }
            )

            executed = execute_actions(root, assistant_run, assistant_task["id"], reply)
            messages, _offset = iter_jsonl_from(inbox_path(root, assistant_run, "main"), 0)

            self.assertTrue(executed[0]["continuation"])
            self.assertEqual(messages[-1]["coordination"], "service_assistant_action_result")
            self.assertEqual(messages[-1]["reply_target"], "feishu")
            self.assertEqual(messages[-1]["service_action_depth"], 1)
            self.assertIn("trusted system envelope", messages[-1]["message"])

    def test_action_schema_retry_keeps_the_service_action_contract(self) -> None:
        message = action_schema_retry_message("bad arguments", service_assistant=True)

        self.assertIn('"type":"service_assistant"', message)
        self.assertIn("Allowed action types are `service_assistant`", message)
        self.assertNotIn("spawn_sub", message)

    def test_commit_message_routing_uses_the_original_feishu_request_as_its_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assistant_run = ensure_service_assistant_run(root, {"backend": "claude", "model": "glm-5.2"})
            assistant_task = ensure_service_assistant_task(
                root,
                assistant_run,
                "tenant:p2p:ou_user",
                {"backend": "claude", "model": "glm-5.2"},
            )
            target = create_plan(root, "User run", 1, "implementation", ["Target"], [], backend="codex", model="gpt-5.6-sol")
            target_task_id = target["tasks"][0]["id"]
            set_subscription(
                root,
                "tenant:p2p:ou_user",
                chat_id="oc_chat",
                open_id="ou_user",
                run_id=assistant_run,
                task_id=assistant_task["id"],
            )

            rejected = prepare_service_assistant_action(
                root,
                assistant_run,
                assistant_task,
                {
                    "type": "service_assistant",
                    "operation": "send_task_message",
                    "arguments": {
                        "run_id": target["id"],
                        "task_id": target_task_id,
                        "message": "请提交，结尾使用 Generated-by: AHA Claude glm-5.2",
                    },
                },
            )
            append_message(
                root,
                assistant_run,
                "main",
                "请让 task-006 提交",
                sender="feishu",
                task_id=assistant_task["id"],
                role="main",
            )
            repaired = prepare_service_assistant_action(
                root,
                assistant_run,
                assistant_task,
                {
                    "type": "service_assistant",
                    "operation": "send_task_message",
                    "arguments": {
                        "run_id": target["id"],
                        "task_id": target_task_id,
                        "message": (
                            "请检查工作树后提交，结尾使用 Generated-by: AHA Claude glm-5.2；"
                            "提交后 git push 同步远程。"
                        ),
                    },
                },
            )
            append_message(
                root,
                assistant_run,
                "main",
                "请让 task-006 提交并推送远端",
                sender="feishu",
                task_id=assistant_task["id"],
                role="main",
            )
            combined = prepare_service_assistant_action(
                root,
                assistant_run,
                assistant_task,
                {
                    "type": "service_assistant",
                    "operation": "send_task_message",
                    "arguments": {
                        "run_id": target["id"],
                        "task_id": target_task_id,
                        "message": "请提交代码，然后 git push 同步远程。",
                    },
                },
            )

            self.assertFalse(rejected["ok"])
            self.assertIn("目标 Task 当前执行 Agent", rejected["user_response"])
            self.assertNotIn("请确认以下 AHA 操作", rejected["user_response"])
            self.assertTrue(repaired["ok"])
            self.assertTrue(repaired["confirmation_required"])
            self.assertIn("请让 task-006 提交", repaired["user_response"])
            self.assertIn("仅执行本地提交", repaired["user_response"])
            self.assertNotIn("git push", repaired["user_response"])
            self.assertNotIn("同步远程", repaired["user_response"])
            self.assertNotIn("Generated-by:", repaired["user_response"])
            card_text = json.dumps(repaired["confirmation_card"], ensure_ascii=False)
            self.assertNotIn("```json", card_text)
            self.assertNotIn('"request_policy"', card_text)
            self.assertFalse(combined["ok"])
            self.assertIn("commit 与 push 必须拆成", combined["user_response"])

            with mock.patch(
                "aha_cli.services.service_assistant_actions._execute_write",
                return_value={"ok": True},
            ) as execute_write:
                resolve_confirmation(
                    root,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    text="确认",
                )
            routed_arguments = execute_write.call_args.args[2]
            self.assertEqual(routed_arguments["message"], "请让 task-006 提交")
            self.assertEqual(routed_arguments["request_policy"]["authorization"], "local_commit_only")
            self.assertEqual(routed_arguments["request_policy"]["remote_push"], "forbidden")
            with mock.patch(
                "aha_cli.web.task_messaging.handle_send_payload",
                return_value={"ok": True},
            ) as routed_send:
                service_assistant_actions._execute_write(root, "send_task_message", routed_arguments)
            routed_payload = routed_send.call_args.args[2]
            self.assertEqual(routed_payload["message"], "请让 task-006 提交")
            self.assertNotIn("request_policy", routed_payload)
            self.assertEqual(
                routed_send.call_args.kwargs["trusted_request_policy"]["authorization"],
                "local_commit_only",
            )

            target_item = append_message(
                root,
                target["id"],
                "main",
                routed_arguments["message"],
                sender="feishu-assistant",
                task_id=target_task_id,
                role="main",
                request_policy=routed_arguments["request_policy"],
            )
            target_prompt = chat_prompt(root, target["id"], "main", target_item, "")
            self.assertIn("Generated-by: AHA Codex GPT-5.6-sol", target_prompt)
            self.assertNotIn("Generated-by: AHA Claude glm-5.2", target_prompt)
            self.assertIn("AHA request policy metadata", target_prompt)
            user_section = target_prompt.split("User message from feishu-assistant", 1)[1]
            self.assertIn("请让 task-006 提交", user_section)
            self.assertNotIn("仅执行本地提交", user_section)

    def test_runtime_snapshot_is_sanitized_for_prompt_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = write_service_runtime(root, host="127.0.0.1", port=8766, auth_required=True)

            self.assertEqual(runtime["aha_home"], str(aha_home_path(root).resolve()))
            self.assertEqual(runtime["bind_port"], "8766")
            self.assertNotIn("token", runtime)

    def test_system_run_and_task_reject_ordinary_destructive_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = ensure_service_assistant_run(root, {"backend": "stub"})
            task = ensure_service_assistant_task(root, run_id, "tenant:p2p:ou_user", {"backend": "stub"})

            with self.assertRaises(RunDeleteError) as delete_error:
                delete_run(root, run_id, force=True)
            with self.assertRaises(RunLifecycleActionError) as lifecycle_error:
                set_run_lifecycle_status(root, run_id, "hidden")
            with self.assertRaises(RunRetentionError) as retention_error:
                apply_run_retention(root, run_id, force=True)
            with self.assertRaises(ValueError):
                delete_task(root, run_id, task["id"])

            self.assertEqual(delete_error.exception.reason, "system_managed_run")
            self.assertEqual(lifecycle_error.exception.reason, "system_managed_run")
            self.assertEqual(retention_error.exception.reason, "system_managed_run")

    def test_dedicated_prompt_explains_identity_runtime_home_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = ensure_service_assistant_run(root, {"backend": "stub"})
            task = ensure_service_assistant_task(root, run_id, "tenant:p2p:ou_user", {"backend": "stub"})
            write_service_runtime(root, host="127.0.0.1", port=8766)
            item = append_message(
                root,
                run_id,
                "main",
                "AHA 现在怎么样？",
                sender="feishu",
                task_id=task["id"],
                role="main",
            )

            prompt = chat_prompt(root, run_id, "main", item, "")

            self.assertIn("persistent service steward", prompt)
            self.assertIn("AHA Home contract", prompt)
            self.assertIn(str(aha_home_path(root).resolve()), prompt)
            self.assertIn("127.0.0.1:8766", prompt)
            self.assertIn("service_status", prompt)
            self.assertIn("create_task", prompt)
            self.assertIn("five minutes", prompt)
            self.assertIn("Never choose or copy a backend, model, generator identity", prompt)
            self.assertIn("A commit request never implies `git push`", prompt)
            self.assertIn("Text fallback accepts a direct reply", prompt)


if __name__ == "__main__":
    unittest.main()
