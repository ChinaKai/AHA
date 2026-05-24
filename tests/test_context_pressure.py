from __future__ import annotations

import unittest

from aha_cli.services.context_pressure import context_pressure, context_window_for_model


class ContextPressureTests(unittest.TestCase):
    def test_codex_gpt55_uses_prompt_tokens_for_context_pressure(self) -> None:
        pressure = context_pressure("codex-chat", "gpt-5.5", {"total": {"tokens": 735000, "chars": 1234, "bytes": 1234}})

        self.assertEqual(pressure["backend"], "codex")
        self.assertEqual(pressure["prompt_tokens"], 735000)
        self.assertEqual(pressure["prompt_chars"], 1234)
        self.assertEqual(pressure["context_window"], 258_000)
        self.assertEqual(pressure["context_window_source"], "table")
        self.assertEqual(pressure["pressure_source"], "prompt_metrics.tokens")
        self.assertEqual(pressure["ratio"], 2.848837)
        self.assertEqual(pressure["percent"], 284.88)
        self.assertEqual(pressure["level"], "high")

    def test_runtime_context_window_takes_priority_over_table(self) -> None:
        pressure = context_pressure(
            "codex-chat",
            "gpt-5.5",
            {"total": {"tokens": 129200}},
            runtime_context_window=258400,
            environ={},
        )

        self.assertEqual(pressure["context_window"], 258400)
        self.assertEqual(pressure["context_window_source"], "runtime")
        self.assertEqual(pressure["ratio"], 0.5)
        self.assertEqual(pressure["percent"], 50.0)
        self.assertEqual(pressure["level"], "ok")

    def test_config_still_overrides_runtime_context_window(self) -> None:
        pressure = context_pressure(
            "codex-chat",
            "gpt-5.5",
            {"total": {"tokens": 500}},
            runtime_context_window=258400,
            cfg={"context_windows": {"codex": {"gpt-5.5": 1000}}},
            environ={},
        )

        self.assertEqual(pressure["context_window"], 1000)
        self.assertEqual(pressure["context_window_source"], "config")
        self.assertEqual(pressure["percent"], 50.0)
        self.assertEqual(pressure["level"], "ok")

    def test_prompt_chars_without_tokens_keeps_pressure_unknown(self) -> None:
        pressure = context_pressure("codex-chat", "gpt-5.5", {"total": {"chars": 120000, "bytes": 130000}})

        self.assertIsNone(pressure["prompt_tokens"])
        self.assertEqual(pressure["prompt_chars"], 120000)
        self.assertEqual(pressure["prompt_bytes"], 130000)
        self.assertEqual(pressure["context_window"], 258_000)
        self.assertIsNone(pressure["ratio"])
        self.assertIsNone(pressure["percent"])
        self.assertEqual(pressure["pressure_source"], "prompt_metrics.chars")
        self.assertEqual(pressure["level"], "unknown")

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
            runtime_context_window=258400,
            cfg={"context_windows": {"codex": {"gpt-5.5": 123456}}},
            environ={"AHA_CONTEXT_WINDOW_CODEX_GPT_5_5": "234567"},
        )

        self.assertEqual(window, 234567)
        self.assertEqual(source, "env:AHA_CONTEXT_WINDOW_CODEX_GPT_5_5")

    def test_unknown_context_window_keeps_pressure_unknown(self) -> None:
        pressure = context_pressure("claude", None, {"total": {"tokens": 10}})

        self.assertIsNone(pressure["context_window"])
        self.assertIsNone(pressure["ratio"])
        self.assertEqual(pressure["level"], "unknown")
