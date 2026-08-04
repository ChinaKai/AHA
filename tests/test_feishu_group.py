from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from aha_cli.domain.models import is_feishu_group_run, is_feishu_group_task
from aha_cli.services.chat import chat_prompt
from aha_cli.services.feishu_group import (
    FEISHU_GROUP_HANDOFF_ACK,
    archive_inactive_feishu_group_tasks,
    ensure_feishu_group_run,
    ensure_feishu_group_task,
    mark_feishu_group_task_interaction,
    session_task_title,
)
from aha_cli.services.feishu_group_handoffs import (
    feishu_group_handoffs_path,
    get_group_handoff,
    mark_group_handoff,
    pending_group_handoffs_for_steward_reply,
    register_group_handoff,
)
from aha_cli.services.feishu_notifications import load_subscription_state
from aha_cli.services.feishu_owner import remember_owner_private_chat
from aha_cli.services.orchestrator import execute_actions
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import append_message
from aha_cli.store.knowledge import write_entry
from aha_cli.store.paths import aha_home_path
from aha_cli.store.runs import require_plan


class FeishuGroupTests(unittest.TestCase):
    def test_group_run_and_user_task_use_dedicated_service_state_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            task = ensure_feishu_group_task(root, run_id, "tenant:feishu-group-user:ou_user", {"backend": "stub"})
            plan = require_plan(root, run_id)

        self.assertTrue(is_feishu_group_run(plan))
        self.assertTrue(is_feishu_group_task(task))
        self.assertEqual(plan["goal"], "feishu-group")
        self.assertEqual(Path(plan["main_agent"]["workspace_path"]), aha_home_path(root).resolve() / "feishu_group_state")
        self.assertEqual(Path(task["workspace_path"]), aha_home_path(root).resolve() / "feishu_group_state")
        self.assertEqual(task["preferred_sandbox"], "read-only")
        self.assertEqual(task["preferred_approval"], "never")
        self.assertEqual(task["collaboration_mode"], "solo")
        self.assertEqual(task["max_sub_agents"], 0)

    def test_group_user_task_title_includes_display_name_and_upgrades_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_key = "tenant:feishu-group-user:ou_user"
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            first = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            renamed = ensure_feishu_group_task(
                root,
                run_id,
                session_key,
                {"backend": "stub"},
                display_name="张 三",
            )
            plan = require_plan(root, run_id)
            stored = next(item for item in plan["tasks"] if item["id"] == first["id"])

        self.assertEqual(first["id"], renamed["id"])
        self.assertRegex(first["title"], r"^Feishu Digital Human · User · [0-9a-f]{6}$")
        self.assertEqual(renamed["title"], session_task_title(session_key, display_name="张 三"))
        self.assertEqual(stored["title"], renamed["title"])
        self.assertEqual(stored["feishu_display_name"], "张 三")

    def test_group_digital_human_prompt_includes_source_index_without_file_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace-roots"
            project = workspace_root / "demo-project"
            (project / "docs").mkdir(parents=True)
            (project / "README.md").write_text("README BODY SHOULD NOT BE IN PROMPT\n", encoding="utf-8")
            (project / "docs" / "guide.md").write_text("GUIDE BODY SHOULD NOT BE IN PROMPT\n", encoding="utf-8")
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "workspace_roots": [str(workspace_root)],
                        "knowledge": {"enabled": True},
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(root)
            write_entry(
                root,
                config=config,
                scope="general",
                kind="wiki",
                title="Digital Human FAQ",
                body="KB BODY SHOULD NOT BE IN PROMPT",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n上一轮问过 pipeline",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group_1",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group_1",
                feishu_session_key=session_key,
                feishu_original_text="上一轮问过 pipeline",
            )

            prompt = chat_prompt(
                root,
                run_id,
                "main",
                {
                    "sender": "feishu",
                    "message": "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n现在能回答吗",
                    "task_id": task["id"],
                    "role": "main",
                },
                "",
            )

        self.assertIn("Digital-human information source index", prompt)
        self.assertIn("AHA Knowledge Base index", prompt)
        self.assertIn("Digital Human FAQ", prompt)
        self.assertIn("Workspace source index", prompt)
        self.assertIn(str(project), prompt)
        self.assertIn("README.md", prompt)
        self.assertIn("docs/", prompt)
        self.assertIn("Recent group @ context", prompt)
        self.assertIn("上一轮问过 pipeline", prompt)
        self.assertNotIn("KB BODY SHOULD NOT BE IN PROMPT", prompt)
        self.assertNotIn("README BODY SHOULD NOT BE IN PROMPT", prompt)
        self.assertNotIn("GUIDE BODY SHOULD NOT BE IN PROMPT", prompt)

    def test_group_handoff_action_forwards_to_service_steward_and_returns_fixed_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.task_messaging.handle_send_payload",
            return_value={"ok": True},
        ) as send, mock.patch(
            "aha_cli.services.feishu_group_actions._send_owner_handoff_card",
            return_value={"ok": True, "sent": True, "message_id": "om_owner_card"},
        ):
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "integrations": {
                            "feishu": {
                                "owner_open_id": "ou_owner",
                                "owner_chat_id": "oc_owner",
                                "allowed_open_ids": ["ou_owner"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n请帮我提交代码",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group",
                feishu_session_key=session_key,
                feishu_original_text="请帮我提交代码",
            )
            reply = json.dumps(
                {
                    "actions": [
                        {
                            "type": "feishu_group_handoff",
                            "arguments": {
                                "reason": "needs execution",
                                "summary": "提交代码",
                                "details": "群成员希望 AHA 代为提交当前代码，并说明是否需要继续推送。",
                            },
                        }
                    ],
                    "response": "",
                },
                ensure_ascii=False,
            )

            executed = execute_actions(root, run_id, task["id"], reply)
            handoffs = json.loads(feishu_group_handoffs_path(root).read_text(encoding="utf-8"))["handoffs"]
            subscriptions = load_subscription_state(root)["subscriptions"]

        self.assertEqual(executed[0]["type"], "feishu_group_handoff")
        self.assertTrue(executed[0]["ok"])
        self.assertEqual(executed[0]["user_response"], FEISHU_GROUP_HANDOFF_ACK)
        handoff = next(iter(handoffs.values()))
        self.assertEqual(handoff["group_chat_id"], "oc_group")
        self.assertEqual(handoff["group_message_id"], "om_group")
        self.assertEqual(handoff["open_id"], "ou_requester")
        self.assertEqual(handoff["owner_open_id"], "ou_owner")
        self.assertEqual(handoff["owner_chat_id"], "oc_owner")
        self.assertEqual(handoff["request_summary"], "提交代码")
        self.assertEqual(handoff["request_detail"], "群成员希望 AHA 代为提交当前代码，并说明是否需要继续推送。")
        self.assertEqual(handoff["handoff_reason"], "needs execution")
        self.assertEqual(send.call_args.args[1], executed[0]["steward_run_id"])
        self.assertEqual(send.call_args.args[2]["sender"], "feishu-group")
        self.assertEqual(send.call_args.args[2]["reply_target"], "feishu")
        self.assertEqual(send.call_args.args[2]["feishu_chat_id"], "oc_owner")
        self.assertEqual(send.call_args.args[2]["feishu_mention_open_id"], "ou_owner")
        self.assertEqual(send.call_args.args[2]["feishu_chat_type"], "p2p")
        self.assertEqual(send.call_args.args[2]["feishu_session_key"], "tenant-1:p2p:ou_owner")
        self.assertEqual(send.call_args.args[2]["feishu_group_handoff_id"], handoff["id"])
        self.assertFalse(send.call_args.kwargs["background_backend_start"])
        self.assertTrue(send.call_args.kwargs["suppress_backend_start"])
        self.assertIn("请帮我提交代码", send.call_args.args[2]["message"])
        self.assertIn("群聊提问者 open_id：\nou_requester", send.call_args.args[2]["message"])
        self.assertIn("需求详情：\n群成员希望 AHA 代为提交当前代码，并说明是否需要继续推送。", send.call_args.args[2]["message"])
        self.assertIn("AHA 系统生成的飞书群聊转单信封", send.call_args.args[2]["message"])
        self.assertIn("不是数字人对管家的处理指令", send.call_args.args[2]["message"])
        self.assertIn(f"handoff_id={handoff['id']}", send.call_args.args[2]["message"])
        self.assertNotIn("处理 SOP：", send.call_args.args[2]["message"])
        self.assertNotIn("计划/可延后类先 create_memo", send.call_args.args[2]["message"])
        self.assertNotIn("当前群聊数字人只支持文本回复", send.call_args.args[2]["message"])
        self.assertEqual(subscriptions["tenant-1:p2p:ou_owner"]["chat_id"], "oc_owner")
        self.assertEqual(subscriptions["tenant-1:p2p:ou_owner"]["task_id"], executed[0]["steward_task_id"])

    def test_group_handoff_merges_same_requester_followups_into_one_pending_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.task_messaging.handle_send_payload",
            return_value={"ok": True},
        ) as send:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "integrations": {
                            "feishu": {
                                "owner_open_id": "ou_owner",
                                "owner_chat_id": "oc_owner",
                                "allowed_open_ids": ["ou_owner"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            executed_ids = []
            for index, text in enumerate(("给我 vega 最新 pipeline", "你在帮我问一下", "再帮我催一下"), start=1):
                append_message(
                    root,
                    run_id,
                    "main",
                    f"飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n{text}",
                    sender="feishu",
                    task_id=task["id"],
                    role="main",
                    feishu_channel="group_digital_human",
                    feishu_chat_id="oc_group",
                    feishu_reply_to=f"om_group_{index}",
                    feishu_mention_open_id="ou_requester",
                    feishu_tenant_key="tenant-1",
                    feishu_chat_type="group",
                    feishu_message_id=f"om_group_{index}",
                    feishu_session_key=session_key,
                    feishu_original_text=text,
                )
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "feishu_group_handoff",
                                "arguments": {"reason": "needs owner", "summary": "vega pipeline"},
                            }
                        ],
                        "response": "",
                    },
                    ensure_ascii=False,
                )
                executed = execute_actions(root, run_id, task["id"], reply)
                executed_ids.append(executed[0]["handoff_id"])

            handoffs = json.loads(feishu_group_handoffs_path(root).read_text(encoding="utf-8"))["handoffs"]
            handoff = next(iter(handoffs.values()))

        self.assertEqual(len(handoffs), 1)
        self.assertEqual(len(set(executed_ids)), 1)
        self.assertEqual(handoff["status"], "pending")
        self.assertEqual(handoff["merged_count"], 2)
        self.assertEqual(handoff["group_message_id"], "om_group_1")
        self.assertEqual(handoff["latest_group_message_id"], "om_group_3")
        self.assertEqual(handoff["group_message_ids"], ["om_group_1", "om_group_2", "om_group_3"])
        self.assertIn("给我 vega 最新 pipeline", handoff["request_preview"])
        self.assertIn("你在帮我问一下", handoff["request_preview"])
        self.assertIn("再帮我催一下", handoff["request_preview"])
        self.assertEqual(send.call_count, 3)
        self.assertIn("已合并到现有待处理单", send.call_args.args[2]["message"])

    def test_group_handoff_can_reuse_model_selected_pending_handoff_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.task_messaging.handle_send_payload",
            return_value={"ok": True},
        ):
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "integrations": {
                            "feishu": {
                                "owner_open_id": "ou_owner",
                                "owner_chat_id": "oc_owner",
                                "allowed_open_ids": ["ou_owner"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})

            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n给我 vega 最新 pipeline",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group_1",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group_1",
                feishu_session_key=session_key,
                feishu_original_text="给我 vega 最新 pipeline",
            )
            first = execute_actions(
                root,
                run_id,
                task["id"],
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "feishu_group_handoff",
                                "arguments": {"reason": "needs owner", "summary": "vega pipeline"},
                            }
                        ],
                        "response": "",
                    },
                    ensure_ascii=False,
                ),
            )[0]
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n顺带把延迟指标也问下",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group_2",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group_2",
                feishu_session_key=session_key,
                feishu_original_text="顺带把延迟指标也问下",
            )
            second = execute_actions(
                root,
                run_id,
                task["id"],
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "feishu_group_handoff",
                                "arguments": {
                                    "reason": "same pending need",
                                    "summary": "补充延迟指标",
                                    "merge_handoff_id": first["handoff_id"],
                                },
                            }
                        ],
                        "response": "",
                    },
                    ensure_ascii=False,
                ),
            )[0]
            handoffs = json.loads(feishu_group_handoffs_path(root).read_text(encoding="utf-8"))["handoffs"]
            handoff = next(iter(handoffs.values()))

        self.assertEqual(first["handoff_id"], second["handoff_id"])
        self.assertTrue(second["merged_existing"])
        self.assertEqual(second["merge_source"], "model")
        self.assertEqual(len(handoffs), 1)
        self.assertIn("顺带把延迟指标也问下", handoff["request_preview"])

    def test_group_handoff_reopens_recent_delivered_thread_for_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.task_messaging.handle_send_payload",
            return_value={"ok": True},
        ):
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "integrations": {
                            "feishu": {
                                "owner_open_id": "ou_owner",
                                "owner_chat_id": "oc_owner",
                                "allowed_open_ids": ["ou_owner"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})

            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n给我 vega 最新 pipeline",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group_1",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group_1",
                feishu_session_key=session_key,
                feishu_original_text="给我 vega 最新 pipeline",
            )
            first = execute_actions(
                root,
                run_id,
                task["id"],
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "feishu_group_handoff",
                                "arguments": {"reason": "needs owner", "summary": "vega pipeline"},
                            }
                        ],
                        "response": "",
                    },
                    ensure_ascii=False,
                ),
            )[0]
            mark_group_handoff(root, first["handoff_id"], "delivered")

            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n给我发张图片吧",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group_2",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group_2",
                feishu_session_key=session_key,
                feishu_original_text="给我发张图片吧",
            )
            second = execute_actions(
                root,
                run_id,
                task["id"],
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "feishu_group_handoff",
                                "arguments": {"reason": "needs owner", "summary": "发送图片", "new_handoff": "false"},
                            }
                        ],
                        "response": "",
                    },
                    ensure_ascii=False,
                ),
            )[0]
            stored = get_group_handoff(root, first["handoff_id"])

        self.assertEqual(first["handoff_id"], second["handoff_id"])
        self.assertTrue(second["merged_existing"])
        self.assertEqual(second["merge_source"], "active_thread")
        self.assertEqual(stored["status"], "pending")
        self.assertTrue(stored["delivered_at"])
        self.assertTrue(stored["reopened_at"])
        self.assertIn("给我 vega 最新 pipeline", stored["request_preview"])
        self.assertIn("给我发张图片吧", stored["request_preview"])

    def test_pending_group_handoffs_fold_legacy_same_scope_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now_epoch = time.time()
            records = {
                "root": {
                    "id": "root",
                    "digital_run_id": "run-digital",
                    "digital_task_id": "task-digital",
                    "digital_session_key": "tenant:feishu-group-user:ou_requester",
                    "group_chat_id": "oc_group",
                    "group_message_id": "om_group_1",
                    "open_id": "ou_requester",
                    "owner_open_id": "ou_owner",
                    "owner_chat_id": "oc_owner",
                    "steward_run_id": "run-steward",
                    "steward_task_id": "task-steward",
                    "request_preview": "给我 vega 最新 pipeline",
                    "status": "delivered",
                    "created_at": "2026-08-03T00:05:28+00:00",
                    "created_at_epoch": now_epoch - 1800,
                    "updated_at": "2026-08-03T00:59:56+00:00",
                    "updated_at_epoch": now_epoch - 1200,
                    "delivered_at": "2026-08-03T00:59:56+00:00",
                },
                "ask": {
                    "id": "ask",
                    "digital_run_id": "run-digital",
                    "digital_task_id": "task-digital",
                    "digital_session_key": "tenant:feishu-group-user:ou_requester",
                    "group_chat_id": "oc_group",
                    "group_message_id": "om_group_2",
                    "open_id": "ou_requester",
                    "owner_open_id": "ou_owner",
                    "owner_chat_id": "oc_owner",
                    "steward_run_id": "run-steward",
                    "steward_task_id": "task-steward",
                    "request_preview": "你在帮我问一下",
                    "status": "pending",
                    "created_at": "2026-08-03T00:33:41+00:00",
                    "created_at_epoch": now_epoch - 1000,
                    "updated_at": "2026-08-03T00:33:41+00:00",
                    "updated_at_epoch": now_epoch - 1000,
                },
                "urge": {
                    "id": "urge",
                    "digital_run_id": "run-digital",
                    "digital_task_id": "task-digital",
                    "digital_session_key": "tenant:feishu-group-user:ou_requester",
                    "group_chat_id": "oc_group",
                    "group_message_id": "om_group_3",
                    "open_id": "ou_requester",
                    "owner_open_id": "ou_owner",
                    "owner_chat_id": "oc_owner",
                    "steward_run_id": "run-steward",
                    "steward_task_id": "task-steward",
                    "request_preview": "再帮我催一下",
                    "status": "pending",
                    "created_at": "2026-08-03T00:45:41+00:00",
                    "created_at_epoch": now_epoch - 900,
                    "updated_at": "2026-08-03T00:45:41+00:00",
                    "updated_at_epoch": now_epoch - 900,
                },
            }
            path = feishu_group_handoffs_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"version": 1, "handoffs": records}), encoding="utf-8")

            pending = pending_group_handoffs_for_steward_reply(root, "run-steward", "task-steward")
            stored = json.loads(path.read_text(encoding="utf-8"))["handoffs"]
            ask_alias = get_group_handoff(root, "ask")

        self.assertEqual([item["id"] for item in pending], ["root"])
        self.assertEqual(stored["root"]["status"], "pending")
        self.assertEqual(stored["ask"]["status"], "merged")
        self.assertEqual(stored["ask"]["merged_into"], "root")
        self.assertEqual(stored["urge"]["status"], "merged")
        self.assertIn("给我 vega 最新 pipeline", stored["root"]["request_preview"])
        self.assertIn("你在帮我问一下", stored["root"]["request_preview"])
        self.assertIn("再帮我催一下", stored["root"]["request_preview"])
        self.assertEqual(ask_alias["id"], "root")

    def test_force_new_handoff_keeps_separate_active_thread_for_same_requester(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = register_group_handoff(
                root,
                digital_run_id="run-digital",
                digital_task_id="task-digital",
                digital_session_key="tenant:feishu-group-user:ou_requester",
                group_chat_id="oc_group",
                group_message_id="om_group_1",
                open_id="ou_requester",
                owner_open_id="ou_owner",
                owner_chat_id="oc_owner",
                steward_run_id="run-steward",
                steward_task_id="task-steward",
                request_message="给我 vega 最新 pipeline",
            )
            second = register_group_handoff(
                root,
                digital_run_id="run-digital",
                digital_task_id="task-digital",
                digital_session_key="tenant:feishu-group-user:ou_requester",
                group_chat_id="oc_group",
                group_message_id="om_group_2",
                open_id="ou_requester",
                owner_open_id="ou_owner",
                owner_chat_id="oc_owner",
                steward_run_id="run-steward",
                steward_task_id="task-steward",
                request_message="另外帮我建一个新任务",
                force_new=True,
            )
            pending = pending_group_handoffs_for_steward_reply(root, "run-steward", "task-steward")

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([item["id"] for item in pending], [first["id"], second["id"]])

    def test_group_handoff_falls_back_to_current_app_owner_when_configured_owner_has_no_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.task_messaging.handle_send_payload",
            return_value={"ok": True},
        ) as send:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "integrations": {
                            "feishu": {
                                "owner_open_id": "ou_old_owner",
                                "allowed_open_ids": ["ou_old_owner", "ou_current_owner"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            remember_owner_private_chat(
                root,
                tenant_key="tenant-1",
                open_id="ou_current_owner",
                chat_id="oc_current_owner",
                session_key="tenant-1:p2p:ou_current_owner",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n现在给我发吧",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group",
                feishu_session_key=session_key,
                feishu_original_text="现在给我发吧",
            )
            reply = json.dumps(
                {
                    "actions": [
                        {
                            "type": "feishu_group_handoff",
                            "arguments": {"reason": "needs owner", "summary": "发送资料"},
                        }
                    ],
                    "response": "",
                },
                ensure_ascii=False,
            )

            executed = execute_actions(root, run_id, task["id"], reply)
            handoffs = json.loads(feishu_group_handoffs_path(root).read_text(encoding="utf-8"))["handoffs"]

        self.assertTrue(executed[0]["ok"])
        self.assertEqual(executed[0]["owner_open_id"], "ou_current_owner")
        handoff = next(iter(handoffs.values()))
        self.assertEqual(handoff["owner_open_id"], "ou_current_owner")
        self.assertEqual(handoff["owner_chat_id"], "oc_current_owner")
        self.assertEqual(send.call_args.args[2]["feishu_chat_id"], "oc_current_owner")
        self.assertEqual(send.call_args.args[2]["feishu_session_key"], "tenant-1:p2p:ou_current_owner")

    def test_inactive_group_task_is_archived_without_deletion_and_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant:feishu-group-user:ou_user"
            first = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            mark_feishu_group_task_interaction(root, run_id, first["id"], at="2026-01-01T00:00:00+00:00")

            archived = archive_inactive_feishu_group_tasks(
                root,
                run_id,
                now="2026-02-01T00:00:01+00:00",
            )
            second = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            plan = require_plan(root, run_id)
            first_stored = next(task for task in plan["tasks"] if task["id"] == first["id"])

        self.assertEqual(archived, 1)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(first_stored["hidden"])
        self.assertEqual(first_stored["status"], "completed")
        self.assertTrue(first_stored["feishu_group_archived_at"])
        self.assertFalse(first_stored.get("deleted_at"))


if __name__ == "__main__":
    unittest.main()
