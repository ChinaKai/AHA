from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from aha_cli.domain.models import task_metadata_projection
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import create_plan, status_snapshot
from aha_cli.store.io import write_json
from aha_cli.store.runs import require_plan


class TaskSchemaTests(unittest.TestCase):
    def test_task_metadata_projection_normalizes_legacy_fields(self) -> None:
        projection = task_metadata_projection(
            {
                "workspace_id": "ws-001",
                "workspace_path": "/repo",
                "preferred_model": "gpt-5.5",
                "delegation_policy": "disabled",
                "max_sub_agents": 3,
                "supervision": {"max_rounds": 12},
                "context_management": {"enabled": True, "threshold_percent": 88},
                "task_skills": {"skills": "/repo/.aha/skills/board-debug/SKILL.md"},
                "hardware_debug": {
                    "enabled": True,
                    "devices": {"id": "legacy-id", "type": "legacy", "port": "/dev/ttyUSB0", "baud": "115200", "prompt": "Sgs #"},
                    "operation_skill_path": "/repo/.aha/skills/uboot-uart/SKILL.md",
                    "permissions": {"serial_write": "true", "reset": "on"},
                },
            },
            default_backend="claude",
        )

        self.assertEqual(projection["workspace_id"], "ws-001")
        self.assertEqual(projection["workspace_path"], "/repo")
        self.assertEqual(projection["preferred_backend"], "claude")
        self.assertEqual(projection["preferred_sub_backend"], "claude")
        self.assertEqual(projection["preferred_sub_model"], "gpt-5.5")
        self.assertEqual(projection["collaboration_mode"], "solo")
        self.assertEqual(projection["workflow_template"], "auto")
        self.assertEqual(projection["delegation_policy"], "disabled")
        self.assertEqual(projection["max_sub_agents"], 0)
        self.assertEqual(projection["supervision"]["max_rounds"], 12)
        self.assertTrue(projection["context_management"]["auto_compact_enabled"])
        self.assertEqual(projection["context_management"]["auto_compact_threshold_percent"], 88)
        self.assertEqual(projection["task_skills"]["enabled_paths"], ["/repo/.aha/skills/board-debug/SKILL.md"])
        hardware_channel = projection["hardware_debug"]["channels"][0]
        self.assertEqual(hardware_channel["type"], "uart")
        self.assertEqual(hardware_channel["settings"], {"port": "/dev/ttyUSB0", "baudrate": 115200})
        self.assertEqual(hardware_channel["operation_skill_path"], "/repo/.aha/skills/uboot-uart/SKILL.md")
        self.assertTrue(hardware_channel["permissions"]["write"])
        self.assertNotIn("reset", hardware_channel["permissions"])

    def test_old_plan_compatibility_fills_task_metadata_and_status_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-legacy"
            write_json(
                root / "runs" / run_id / "plan.json",
                {
                    "id": run_id,
                    "goal": "Legacy plan",
                    "mode": "research",
                    "created_at": "2026-05-30T00:00:00+00:00",
                    "updated_at": "2026-05-30T00:00:00+00:00",
                    "write_scopes": [],
                    "tasks": [
                        {
                            "id": "task-001",
                            "title": "Legacy task",
                            "description": "",
                            "workspace_id": "ws-legacy",
                            "workspace_path": "/legacy/repo",
                            "preferred_backend": "claude",
                            "preferred_model": "sonnet",
                            "delegation_policy": "auto",
                            "max_sub_agents": 1,
                            "status": "pending",
                            "prompt_file": "prompts/task-001.md",
                            "output_file": "results/task-001.md",
                            "log_file": "logs/task-001.log",
                            "inbox_file": "inbox/task-001.jsonl",
                            "created_at": "2026-05-30T00:00:00+00:00",
                            "started_at": None,
                            "finished_at": None,
                            "exit_code": None,
                            "agents": [],
                        }
                    ],
                },
            )

            enriched_task = require_plan(root, run_id)["tasks"][0]
            snapshot_task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(enriched_task["collaboration_mode"], "pair")
        self.assertEqual(enriched_task["workflow_template"], "auto")
        self.assertEqual(enriched_task["preferred_sub_backend"], "claude")
        self.assertEqual(enriched_task["preferred_sub_model"], "sonnet")
        self.assertEqual(enriched_task["supervision"]["mode"], "manual")
        self.assertFalse(enriched_task["context_management"]["auto_compact_enabled"])
        self.assertEqual(enriched_task["hardware_debug"]["channels"], [])
        self.assertEqual(enriched_task["task_skills"]["enabled_paths"], [])
        self.assertEqual(snapshot_task["workspace_id"], "ws-legacy")
        self.assertEqual(snapshot_task["preferred_sub_backend"], "claude")
        self.assertEqual(snapshot_task["preferred_sub_model"], "sonnet")
        self.assertEqual(snapshot_task["collaboration_mode"], "pair")
        self.assertEqual(snapshot_task["max_sub_agents"], 1)

    def test_supervision_host_uses_dedicated_model_and_proxy_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = create_plan(
                root,
                "Host controls",
                0,
                "implementation",
                [],
                [],
                backend="codex",
                model="gpt-5.5",
                proxy_enabled=True,
                create_default_tasks=False,
            )
            task = create_task_and_dispatch(
                root,
                plan["id"],
                "Supervised task",
                backend="codex",
                model="gpt-5.5",
                proxy_enabled=True,
                supervision={
                    "mode": "assisted",
                    "host_backend": "claude",
                    "host_model": "claude-sonnet-4-5",
                    "host_proxy_enabled": False,
                    "real_agent_enabled": True,
                },
                dispatch=False,
            )
            default_host_task = create_task_and_dispatch(
                root,
                plan["id"],
                "Default supervised task",
                backend="codex",
                model="gpt-5.5",
                proxy_enabled=True,
                supervision={
                    "mode": "assisted",
                    "host_backend": "claude",
                    "real_agent_enabled": True,
                },
                dispatch=False,
            )
            snapshot_task = status_snapshot(root, plan["id"])["tasks"][0]

        main = next(agent for agent in task["agents"] if agent["id"] == "main")
        host = next(agent for agent in task["agents"] if agent["role"] == "host")
        default_host = next(agent for agent in default_host_task["agents"] if agent["role"] == "host")
        self.assertEqual(main["model"], "gpt-5.5")
        self.assertTrue(main["proxy_enabled"])
        self.assertEqual(host["backend"], "claude")
        self.assertEqual(host["model"], "claude-sonnet-4-5")
        self.assertFalse(host["proxy_enabled"])
        self.assertIsNone(default_host["model"])
        self.assertFalse(default_host["proxy_enabled"])
        self.assertEqual(snapshot_task["supervision"]["host_model"], "claude-sonnet-4-5")
        self.assertFalse(snapshot_task["supervision"]["host_proxy_enabled"])


if __name__ == "__main__":
    unittest.main()
