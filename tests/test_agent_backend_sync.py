from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import agent_backend_switch
from aha_cli.services.agent_backend_switch import sync_assistant_task_backend
from aha_cli.services.service_assistant import ensure_service_assistant_run, ensure_service_assistant_task
from aha_cli.store.snapshots import task_snapshot


def _stub_task(root: Path) -> tuple[str, str]:
    run_id = ensure_service_assistant_run(root, {"backend": "stub"})
    task = ensure_service_assistant_task(root, run_id, "tenant:p2p:ou_user", {"backend": "stub"})
    return run_id, str(task.get("id") or "")


class SyncAssistantTaskBackendTests(unittest.TestCase):
    def test_backend_drift_triggers_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            with mock.patch.object(agent_backend_switch, "switch_agent_backend") as switch, mock.patch.object(
                agent_backend_switch, "update_agent_config"
            ) as update:
                sync_assistant_task_backend(root, run_id, task, {"backend": "claude", "model": "claude-sonnet-5"})
            switch.assert_called_once()
            self.assertEqual(switch.call_args.kwargs["backend"], "claude")
            self.assertEqual(switch.call_args.kwargs["model"], "claude-sonnet-5")
            update.assert_not_called()

    def test_model_only_drift_triggers_switch_with_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            with mock.patch.object(agent_backend_switch, "switch_agent_backend") as switch:
                sync_assistant_task_backend(root, run_id, task, {"backend": "stub", "model": "gpt-5.6-sol"})
            switch.assert_called_once()
            self.assertEqual(switch.call_args.kwargs["backend"], "stub")
            self.assertEqual(switch.call_args.kwargs["model"], "gpt-5.6-sol")

    def test_effort_and_proxy_drift_persist_without_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            with mock.patch.object(agent_backend_switch, "switch_agent_backend") as switch, mock.patch.object(
                agent_backend_switch, "update_agent_config"
            ) as update:
                sync_assistant_task_backend(
                    root, run_id, task, {"backend": "stub", "reasoning_effort": "high", "proxy_enabled": True}
                )
            switch.assert_not_called()
            update.assert_called_once()
            self.assertEqual(update.call_args.kwargs["reasoning_effort"], "high")
            self.assertIs(update.call_args.kwargs["proxy_enabled"], True)

    def test_no_drift_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            with mock.patch.object(agent_backend_switch, "switch_agent_backend") as switch, mock.patch.object(
                agent_backend_switch, "update_agent_config"
            ) as update:
                result = sync_assistant_task_backend(root, run_id, task, {"backend": "stub"})
            switch.assert_not_called()
            update.assert_not_called()
            self.assertEqual(str(result.get("id") or ""), task_id)

    def test_unspecified_model_keeps_current_model_on_backend_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            # Give the task a model as if it had been created with one.
            with mock.patch.object(agent_backend_switch, "update_agent_config"):
                sync_assistant_task_backend(root, run_id, task, {"backend": "stub", "model": "existing-model"})
            task = task_snapshot(root, run_id, task_id)["task"]
            with mock.patch.object(agent_backend_switch, "switch_agent_backend") as switch:
                sync_assistant_task_backend(root, run_id, task, {"backend": "claude"})
            switch.assert_called_once()
            # No model in defaults: keep the current task model instead of clearing it.
            self.assertEqual(switch.call_args.kwargs["model"], "existing-model")

    def test_missing_main_agent_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            task["agents"] = []
            with mock.patch.object(agent_backend_switch, "switch_agent_backend") as switch:
                result = sync_assistant_task_backend(root, run_id, task, {"backend": "claude"})
            switch.assert_not_called()
            self.assertEqual(str(result.get("id") or ""), task_id)

    def test_real_switch_updates_agent_and_task_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id = _stub_task(root)
            task = task_snapshot(root, run_id, task_id)["task"]
            sync_assistant_task_backend(root, run_id, task, {"backend": "claude", "model": "claude-sonnet-5"})
            updated = task_snapshot(root, run_id, task_id)["task"]
            main = next(item for item in updated["agents"] if item.get("id") == "main")
            self.assertEqual(main.get("backend"), "claude")
            self.assertEqual(main.get("model"), "claude-sonnet-5")
            self.assertEqual(updated.get("preferred_backend"), "claude")
            self.assertEqual(updated.get("preferred_model"), "claude-sonnet-5")
