from __future__ import annotations

from pathlib import Path
import unittest

from aha_cli.web.task_command_router import SlashCommandHandlers, default_slash_command_handlers, handle_slash_command


class TaskCommandRouterTests(unittest.TestCase):
    def make_handlers(self) -> tuple[SlashCommandHandlers, list[tuple]]:
        calls: list[tuple] = []

        def append_message(*args, **kwargs):
            calls.append(("append_message", args, kwargs))
            return {"target": args[2], "message": args[3], "agent_id": kwargs.get("agent_id")}

        def append_event(*args, **kwargs):
            calls.append(("append_event", args, kwargs))
            return {"type": args[2], "payload": args[3]}

        handlers = SlashCommandHandlers(
            format_aha_command=lambda root, run_id, task_id, command, target: f"formatted {command} for {target}",
            format_agent_command=lambda root, run_id, task_id, agent_id, command: (False, "/status", None),
            record_task_checkpoint=lambda root, run_id, task_id, command: "checkpoint recorded",
            request_task_finalization=lambda root, run_id, task_id, command: f"final requested by {command}",
            reopen_selected_task=lambda root, run_id, task_id: "reopened",
            interrupt_selected_agent=lambda root, run_id, task_id, target: ("interrupted", {"interrupted": True, "target": target}),
            compact_reset_selected_agent=lambda root, run_id, task_id, target: (
                "compact reset",
                {"old_backend_session_id": "session-1", "target": target},
            ),
            transition_selected_agent_phase=lambda root, run_id, task_id, target, command: (
                "phase changed",
                {"phase": command.split()[2], "target": target},
            ),
            prepare_task_main_autostart=lambda root, run_id, task_id: {"backend": "codex", "target": "main", "task_id": task_id},
            append_message=append_message,
            append_event=append_event,
        )
        return handlers, calls

    def test_agent_command_is_forwarded_with_original_command_metadata(self) -> None:
        handlers, calls = self.make_handlers()

        handled, forwarded, payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "main"},
            "/agent status",
            "task-001",
            handlers=handlers,
        )

        self.assertFalse(handled)
        self.assertEqual(forwarded, "/status")
        self.assertEqual(payload, {"command_namespace": "agent", "original_command": "/agent status"})
        self.assertEqual(calls, [])

    def test_default_handlers_use_format_and_action_candidates(self) -> None:
        from aha_cli.web import task_command_actions

        handlers = default_slash_command_handlers()

        self.assertEqual(handlers.format_aha_command.__module__, "aha_cli.web.task_command_format")
        self.assertEqual(handlers.format_agent_command.__module__, "aha_cli.web.task_command_format")
        self.assertEqual(handlers.record_task_checkpoint.__module__, "aha_cli.web.task_command_actions")
        self.assertIs(handlers.request_task_finalization, task_command_actions.request_task_finalization)

    def test_aha_final_records_command_and_returns_backend_autostart(self) -> None:
        handlers, calls = self.make_handlers()

        handled, forwarded, payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "main"},
            "/aha final",
            "task-001",
            handlers=handlers,
        )

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertEqual(payload["backend_autostart"]["backend"], "codex")
        self.assertEqual(payload["message"]["message"], "final requested by /aha final")
        self.assertEqual(calls[0][0], "append_message")
        self.assertEqual(calls[0][1][2], "aha")
        self.assertEqual(calls[1][0], "append_event")
        self.assertEqual(calls[1][1][2], "aha_command_handled")

    def test_aha_finalize_is_not_a_finalization_alias(self) -> None:
        handlers, _ = self.make_handlers()

        handled, forwarded, payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "main"},
            "/aha finalize",
            "task-001",
            handlers=handlers,
        )

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertNotIn("backend_autostart", payload)
        self.assertEqual(payload["message"]["message"], "formatted /aha finalize for main")

    def test_aha_complete_is_not_a_finalization_alias(self) -> None:
        handlers, _ = self.make_handlers()

        handled, forwarded, payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "main"},
            "/aha complete",
            "task-001",
            handlers=handlers,
        )

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertNotIn("backend_autostart", payload)
        self.assertEqual(payload["message"]["message"], "formatted /aha complete for main")

    def test_interrupt_and_compact_reset_attach_action_payloads(self) -> None:
        handlers, _ = self.make_handlers()

        interrupt_handled, _, interrupt_payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "sub-001"},
            "/aha interrupt",
            "task-001",
            handlers=handlers,
        )
        reset_handled, _, reset_payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "main"},
            "/aha session compact-reset",
            "task-001",
            handlers=handlers,
        )

        self.assertTrue(interrupt_handled)
        self.assertEqual(interrupt_payload["interrupt"]["target"], "sub-001")
        self.assertTrue(reset_handled)
        self.assertEqual(reset_payload["compact_reset"]["old_backend_session_id"], "session-1")

    def test_phase_command_attaches_phase_transition_payload(self) -> None:
        handlers, _ = self.make_handlers()

        handled, forwarded, payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "main"},
            "/aha phase implement start coding",
            "task-001",
            handlers=handlers,
        )

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertEqual(payload["phase_transition"], {"phase": "implement", "target": "main"})


if __name__ == "__main__":
    unittest.main()
