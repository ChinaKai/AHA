from __future__ import annotations

import unittest

from aha_cli.services.context_pressure import context_pressure, context_window_for_model


class ContextPressureTests(unittest.TestCase):
    def test_codex_gpt55_uses_table_context_window(self) -> None:
        pressure = context_pressure("codex-chat", "gpt-5.5", {"input_tokens": 735000})

        self.assertEqual(pressure["backend"], "codex")
        self.assertEqual(pressure["context_window"], 1_050_000)
        self.assertEqual(pressure["context_window_source"], "table")
        self.assertEqual(pressure["ratio"], 0.7)
        self.assertEqual(pressure["percent"], 70.0)
        self.assertEqual(pressure["level"], "watch")

    def test_context_window_can_be_overridden_by_config(self) -> None:
        window, source = context_window_for_model(
            "codex",
            "gpt-5.5",
            cfg={"context_windows": {"codex": {"gpt-5.5": 123456}}},
            environ={},
        )

        self.assertEqual(window, 123456)
        self.assertEqual(source, "config")

    def test_context_window_can_be_overridden_by_env(self) -> None:
        window, source = context_window_for_model(
            "codex",
            "gpt-5.5",
            cfg={"context_windows": {"codex": {"gpt-5.5": 123456}}},
            environ={"AHA_CONTEXT_WINDOW_CODEX_GPT_5_5": "234567"},
        )

        self.assertEqual(window, 234567)
        self.assertEqual(source, "env:AHA_CONTEXT_WINDOW_CODEX_GPT_5_5")

    def test_unknown_context_window_keeps_pressure_unknown(self) -> None:
        pressure = context_pressure("claude", None, {"input_tokens": 10})

        self.assertIsNone(pressure["context_window"])
        self.assertIsNone(pressure["ratio"])
        self.assertEqual(pressure["level"], "unknown")
