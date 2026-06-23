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
            request_task_finalization=lambda root, run_id, task_id, command: f"final requested by {command}",
            complete_selected_task=lambda root, run_id, task_id: ("completed directly", {"ok": True, "mode": "direct"}),
            reopen_selected_task=lambda root, run_id, task_id: "reopened",
            interrupt_selected_agent=lambda root, run_id, task_id, target: ("interrupted", {"interrupted": True, "target": target}),
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

    def test_aha_complete_marks_task_complete_without_backend_autostart(self) -> None:
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
        self.assertEqual(payload["message"]["message"], "completed directly")
        self.assertEqual(payload["completion"]["mode"], "direct")

    def test_interrupt_attaches_action_payload(self) -> None:
        handlers, _ = self.make_handlers()

        interrupt_handled, _, interrupt_payload = handle_slash_command(
            Path("/tmp/root"),
            "run-1",
            {"sender": "browser", "target": "sub-001"},
            "/aha interrupt",
            "task-001",
            handlers=handlers,
        )

        self.assertTrue(interrupt_handled)
        self.assertEqual(interrupt_payload["interrupt"]["target"], "sub-001")

    def test_removed_aha_commands_are_formatted_as_unsupported(self) -> None:
        handlers, _ = self.make_handlers()

        for command in ("/aha help", "/aha status", "/aha agents", "/aha checkpoint x", "/aha phase implement", "/aha session compact-reset"):
            with self.subTest(command=command):
                handled, forwarded, payload = handle_slash_command(
                    Path("/tmp/root"),
                    "run-1",
                    {"sender": "browser", "target": "main"},
                    command,
                    "task-001",
                    handlers=handlers,
                )

                self.assertTrue(handled)
                self.assertIsNone(forwarded)
                self.assertNotIn("backend_autostart", payload)
                self.assertNotIn("interrupt", payload)
                self.assertEqual(payload["message"]["message"], f"formatted {command} for main")


if __name__ == "__main__":
    unittest.main()
