from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from aha_cli.domain.models import task_metadata_projection
from aha_cli.store.filesystem import status_snapshot
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
        self.assertEqual(snapshot_task["workspace_id"], "ws-legacy")
        self.assertEqual(snapshot_task["preferred_sub_backend"], "claude")
        self.assertEqual(snapshot_task["preferred_sub_model"], "sonnet")
        self.assertEqual(snapshot_task["collaboration_mode"], "pair")
        self.assertEqual(snapshot_task["max_sub_agents"], 1)


if __name__ == "__main__":
    unittest.main()
