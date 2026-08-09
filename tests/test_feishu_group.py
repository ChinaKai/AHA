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
from aha_cli.services.feishu_notifications import load_subscription_state, set_subscription
from aha_cli.services.feishu_owner import remember_owner_private_chat
from aha_cli.services.orchestrator import execute_actions
from aha_cli.services.service_assistant import ensure_service_assistant_run, ensure_service_assistant_task
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import append_message, create_plan
from aha_cli.store.knowledge import write_entry
from aha_cli.store.paths import aha_home_path, mark_aha_home
from aha_cli.store.runs import require_plan
from aha_cli.store.task_memos import create_task_memo


class FeishuGroupTests(unittest.TestCase):
    def test_failed_group_task_is_reopened_and_reused_not_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_key = "tenant:feishu-group-user:ou_user"
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            first = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            first_id = str(first.get("id") or "")

            from aha_cli.store.filesystem import set_task_status

            set_task_status(root, run_id, first_id, "failed", 1)

            reopened = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            plan = require_plan(root, run_id)
            group_tasks = [t for t in plan.get("tasks", []) if is_feishu_group_task(t)]

        self.assertEqual(str(reopened.get("id") or ""), first_id)
        self.assertEqual(reopened.get("status"), "awaiting_user")
        self.assertEqual(len(group_tasks), 1)

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

    def test_group_digital_human_prompt_filters_sources_by_read_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace-roots"
            allowed_project = workspace_root / "allowed-project"
            (allowed_project / "docs").mkdir(parents=True)
            (allowed_project / "README.md").write_text("ALLOWED README\n", encoding="utf-8")
            (allowed_project / "docs" / "guide.md").write_text("ALLOWED GUIDE\n", encoding="utf-8")
            secret_project = workspace_root / "secret-project"
            secret_project.mkdir(parents=True)
            (secret_project / "secret.md").write_text("SECRET BODY\n", encoding="utf-8")
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "workspace_roots": [str(workspace_root)],
                        "knowledge": {"enabled": True},
                        "agents": {
                            "group_digital_human": {
                                "permissions": {"read_paths": [str(allowed_project)]}
                            }
                        },
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
                title="Allowed KB Doc",
                body="ALLOWED KB BODY",
                slug="allowed-kb",
            )
            write_entry(
                root,
                config=config,
                scope="general",
                kind="wiki",
                title="Secret KB Doc",
                body="SECRET KB BODY",
                slug="secret-kb",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n能看哪些",
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
                feishu_original_text="能看哪些",
            )

            prompt = chat_prompt(
                root,
                run_id,
                "main",
                {
                    "sender": "feishu",
                    "message": "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n能看哪些",
                    "task_id": task["id"],
                    "role": "main",
                },
                "",
            )

        # Only the allowlisted path is declared; no specific content enumerated.
        self.assertIn(str(allowed_project), prompt)
        self.assertIn("Readable paths allowlist", prompt)
        # Secret sibling project is not declared at all.
        self.assertNotIn(str(secret_project), prompt)
        self.assertNotIn("secret-project", prompt)
        # No KB/workspace enumeration when read_paths is set.
        self.assertNotIn("AHA Knowledge Base index", prompt)
        self.assertNotIn("Workspace source index", prompt)
        self.assertNotIn("Allowed KB Doc", prompt)
        self.assertNotIn("Secret KB Doc", prompt)
        self.assertNotIn("SECRET KB BODY", prompt)
        self.assertNotIn("SECRET BODY", prompt)

    def test_group_digital_human_prompt_includes_linked_memo_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mark_aha_home(root)
            work = create_plan(root, "Feishu work", 1, "implementation", [], [], backend="stub", create_default_tasks=False)
            memo = create_task_memo(
                root,
                work["id"],
                {
                    "title": "跟进 VEGA Hualai 0008 OTA 包",
                    "description": "确认最新分支 VEGA Hualai 版型 0008 OTA 包构建进度并反馈。",
                    "status": "closed",
                },
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            handoff = register_group_handoff(
                root,
                digital_run_id=run_id,
                digital_task_id=task["id"],
                digital_session_key=session_key,
                group_chat_id="oc_group",
                group_message_id="om_group_1",
                open_id="ou_requester",
                owner_open_id="ou_owner",
                owner_chat_id="oc_owner",
                steward_run_id="run-steward",
                steward_task_id="task-steward",
                request_message="有进展吗",
                request_summary="查询 VEGA Hualai 0008 OTA 包进展",
                request_detail="询问最新分支 VEGA Hualai 版型 0008 OTA 包是否已有构建进展。",
            )
            mark_group_handoff(root, handoff["id"], "memo_created", memo_id=memo["id"])

            prompt = chat_prompt(
                root,
                run_id,
                "main",
                {
                    "sender": "feishu",
                    "message": "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n现在有进展吗",
                    "task_id": task["id"],
                    "role": "main",
                },
                "",
            )

        self.assertIn(f"memo={work['id']}:{memo['id']}", prompt)
        self.assertIn("memo_status=closed", prompt)
        self.assertIn("memo_status_label=已关闭", prompt)
        self.assertIn("If memo_status is done or closed", prompt)

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
            set_subscription(
                root,
                "tenant-1:p2p:ou_owner",
                chat_id="oc_owner",
                open_id="ou_owner",
                run_id="run-task-chat",
                task_id="task-chat",
                chat_type="p2p",
                mode="task_chat",
            )
            run_id = ensure_feishu_group_run(root, {"backend": "stub"})
            session_key = "tenant-1:feishu-group-user:ou_requester"
            task = ensure_feishu_group_task(root, run_id, session_key, {"backend": "stub"})
            origin = append_message(
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
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n这是稍后到达的消息",
                sender="feishu",
                task_id=task["id"],
                role="main",
                feishu_channel="group_digital_human",
                feishu_chat_id="oc_group",
                feishu_reply_to="om_group_later",
                feishu_mention_open_id="ou_requester",
                feishu_tenant_key="tenant-1",
                feishu_chat_type="group",
                feishu_message_id="om_group_later",
                feishu_session_key=session_key,
                feishu_original_text="这是稍后到达的消息",
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

            executed = execute_actions(root, run_id, task["id"], reply, origin_message=origin)
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
        self.assertEqual(subscriptions["tenant-1:p2p:ou_owner"]["mode"], "task_chat")
        self.assertEqual(subscriptions["tenant-1:p2p:ou_owner"]["run_id"], "run-task-chat")
        self.assertEqual(subscriptions["tenant-1:p2p:ou_owner"]["task_id"], "task-chat")

    def test_group_handoff_action_replies_with_existing_terminal_memo_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.task_messaging.handle_send_payload",
            return_value={"ok": True},
        ) as send, mock.patch(
            "aha_cli.services.feishu_group_actions._send_owner_handoff_card",
            return_value={"ok": True, "sent": True, "message_id": "om_owner_card"},
        ) as owner_card:
            root = Path(tmp)
            mark_aha_home(root)
            work = create_plan(root, "Feishu work", 1, "implementation", [], [], backend="stub", create_default_tasks=False)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "integrations": {
                            "feishu": {
                                "default_run_id": work["id"],
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
            steward_run = ensure_service_assistant_run(root, {"backend": "stub"})
            steward_task = ensure_service_assistant_task(root, steward_run, "tenant-1:p2p:ou_owner", {"backend": "stub"})
            existing_handoff = register_group_handoff(
                root,
                digital_run_id=run_id,
                digital_task_id=task["id"],
                digital_session_key=session_key,
                group_chat_id="oc_group",
                group_message_id="om_group_1",
                open_id="ou_requester",
                owner_open_id="ou_owner",
                owner_chat_id="oc_owner",
                steward_run_id=steward_run,
                steward_task_id=steward_task["id"],
                request_message="帮我催一下",
                request_summary="跟进 VEGA Hualai 0008 OTA 包",
                request_detail="跟进最新分支 VEGA Hualai 版型 0008 OTA 包的构建进度。",
            )
            memo = create_task_memo(
                root,
                work["id"],
                {
                    "title": "跟进 VEGA Hualai 0008 OTA 包",
                    "description": "确认最新分支 VEGA Hualai 版型 0008 OTA 包构建进度并反馈。",
                    "status": "closed",
                    "source_handoff_id": existing_handoff["id"],
                },
            )
            mark_group_handoff(root, existing_handoff["id"], "memo_created", memo_id=memo["id"], memo_run_id=work["id"])
            append_message(
                root,
                run_id,
                "main",
                "飞书群聊 @ 数字人请求\n\n当前 @ 消息：\n有进展吗",
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
                feishu_original_text="有进展吗",
            )
            reply = json.dumps(
                {
                    "actions": [
                        {
                            "type": "feishu_group_handoff",
                            "arguments": {
                                "reason": "follow-up for existing need",
                                "summary": "查询 VEGA Hualai 0008 OTA 包进展",
                                "details": "询问最新分支 VEGA Hualai 版型 0008 OTA 包是否已有构建进展。",
                            },
                        }
                    ],
                    "response": "",
                },
                ensure_ascii=False,
            )

            executed = execute_actions(root, run_id, task["id"], reply)
            stored = get_group_handoff(root, existing_handoff["id"])

        self.assertTrue(executed[0]["ok"])
        self.assertTrue(executed[0]["linked_existing_memo"])
        self.assertEqual(executed[0]["handoff_id"], existing_handoff["id"])
        self.assertEqual(executed[0]["memo_id"], memo["id"])
        self.assertEqual(executed[0]["user_response"], "这个需求关联的主人待办已关闭。")
        self.assertEqual(stored["status"], "memo_created")
        self.assertEqual(stored["memo_id"], memo["id"])
        self.assertEqual(stored["merged_count"], 1)
        send.assert_not_called()
        owner_card.assert_not_called()

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


class FeishuGroupSourcePathFilterTests(unittest.TestCase):
    """Tests for the read_paths allowlist: give paths only, no enumeration."""

    def test_read_paths_context_lists_paths_without_enumeration(self) -> None:
        from aha_cli.services.feishu_group_sources import feishu_group_source_index_context

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed-proj"
            allowed.mkdir(parents=True)
            secret = root / "secret-proj"
            secret.mkdir()
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "workspace_roots": [str(root)],
                        "knowledge": {"enabled": True},
                        "agents": {
                            "group_digital_human": {
                                "permissions": {"read_paths": [str(allowed)]}
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            task = {"id": "t1", "kind": "feishu_group_digital_human", "system_managed": True}
            ctx = feishu_group_source_index_context(root, "r1", task)

        # Only the allowlisted path is declared; nothing is enumerated.
        self.assertIn("Readable paths allowlist", ctx)
        self.assertIn(str(allowed), ctx)
        self.assertNotIn(str(secret), ctx)
        self.assertNotIn("AHA Knowledge Base index", ctx)
        self.assertNotIn("Workspace source index", ctx)
        self.assertNotIn("entry_count", ctx)
        self.assertNotIn("filtered", ctx)
        self.assertIn("do not attempt to read", ctx.lower())
        # Permission scope still present.
        self.assertIn("default answer scope", ctx)

    def test_read_paths_empty_keeps_full_enumeration(self) -> None:
        from aha_cli.services.feishu_group_sources import feishu_group_source_index_context

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            (project / "docs").mkdir(parents=True)
            (project / "README.md").write_text("r", encoding="utf-8")
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "stub",
                        "workspace_roots": [str(root)],
                        "knowledge": {"enabled": True},
                        "agents": {"group_digital_human": {"permissions": {}}},
                    }
                ),
                encoding="utf-8",
            )
            task = {"id": "t1", "kind": "feishu_group_digital_human", "system_managed": True}
            ctx = feishu_group_source_index_context(root, "r1", task)

        # No read_paths: the full KB + workspace index is still present.
        self.assertNotIn("Readable paths allowlist", ctx)
        self.assertIn("AHA Knowledge Base index", ctx)
        self.assertIn("Workspace source index", ctx)
        self.assertIn(str(project), ctx)


if __name__ == "__main__":
    unittest.main()
