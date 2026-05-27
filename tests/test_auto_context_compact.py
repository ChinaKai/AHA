from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.auto_context_compact import start_backend_after_auto_compact
from aha_cli.store.event_views import conversation_events_page
from aha_cli.store.filesystem import update_task_context_management_config
from aha_cli.store.sessions import ensure_session, save_session


class AutoContextCompactTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_start_backend_auto_compacts_before_process_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Auto compact before start", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_context_management_config(
                    root,
                    run_id,
                    "task-001",
                    auto_compact_enabled=True,
                    auto_compact_threshold_percent=75,
                )
                session = ensure_session(root, run_id, "task-001", "main", "codex", model="gpt-5.5")
                session["backend_session_id"] = "codex-session-high-context"
                save_session(root, session)

                calls: list[str] = []

                def fake_compact_reset(*_args: object, **_kwargs: object) -> dict:
                    calls.append("compact")
                    return {"ok": True, "summary_path": "tasks/task-001/compacts/main.md"}

                def fake_start_backend(*_args: object, **_kwargs: object) -> dict:
                    calls.append("start")
                    return {"status": "running", "started": True}

                with (
                    mock.patch(
                        "aha_cli.services.auto_context_compact.backend_status",
                        return_value={"status": "stopped", "context_pressure": {"level": "watch", "percent": 80.0}},
                    ),
                    mock.patch(
                        "aha_cli.services.auto_context_compact.compact_reset_backend_session",
                        side_effect=fake_compact_reset,
                    ) as compact_reset,
                    mock.patch(
                        "aha_cli.services.auto_context_compact.start_backend",
                        side_effect=fake_start_backend,
                    ) as start_backend,
                ):
                    backend = start_backend_after_auto_compact(root, run_id, "main", backend="codex", task_id="task-001")
                    conversation = conversation_events_page(root, run_id, "task-001", "main", categories={"chat"})

        self.assertEqual(backend["status"], "running")
        self.assertEqual(calls, ["compact", "start"])
        compact_reset.assert_called_once_with(root, run_id, "task-001", "main", reason="large", restart=False)
        start_backend.assert_called_once()
        messages = [event["data"]["message"] for event in conversation["events"] if event["type"] == "message"]
        self.assertTrue(any("AHA 已自动整理 `main` 的 agent context" in message for message in messages))
        self.assertTrue(any("tasks/task-001/compacts/main.md" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
