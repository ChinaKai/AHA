from __future__ import annotations

import unittest

from aha_cli.backends.registry import CODEX_DEFAULT_MODEL, agent_backend_names, agent_backends, backend_names, model_options, resolve_model


class BackendRegistryTests(unittest.TestCase):
    def test_command_backend_is_not_an_agent_backend(self) -> None:
        self.assertIn("command", backend_names())
        self.assertNotIn("command", agent_backend_names())
        self.assertIn("codex", agent_backend_names())
        self.assertIn("claude", agent_backend_names())

    def test_model_options_are_bound_to_agent_backend(self) -> None:
        codex_options = model_options("codex")
        claude_options = model_options("claude")
        stub_options = model_options("stub")
        self.assertEqual(codex_options[0]["name"], "")
        self.assertEqual(codex_options[0]["label"], f"default ({CODEX_DEFAULT_MODEL})")
        self.assertIn("gpt-5.3-codex", {item["name"] for item in codex_options})
        self.assertEqual(
            [item["name"] for item in claude_options],
            ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
        )
        self.assertEqual(stub_options, [{"name": "", "label": "default"}])
        self.assertIn("commands", agent_backends()[0])

    def test_codex_default_model_resolves_explicitly(self) -> None:
        self.assertEqual(resolve_model("codex", None), CODEX_DEFAULT_MODEL)
        self.assertEqual(resolve_model("codex", ""), CODEX_DEFAULT_MODEL)
        self.assertEqual(resolve_model("codex", "default"), CODEX_DEFAULT_MODEL)
        self.assertEqual(resolve_model("codex", "gpt-5.4"), "gpt-5.4")
        self.assertIsNone(resolve_model("claude", None))
