from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from aha_cli.backends import registry
from aha_cli.backends.registry import (
    CODEX_DEFAULT_MODEL,
    agent_backend_names,
    agent_backends,
    backend_names,
    model_options,
    resolve_model,
)


class BackendRegistryTests(unittest.TestCase):
    def test_command_backend_is_not_an_agent_backend(self) -> None:
        self.assertIn("command", backend_names())
        self.assertNotIn("command", agent_backend_names())
        self.assertIn("codex", agent_backend_names())
        self.assertIn("claude", agent_backend_names())

    def test_model_options_are_bound_to_agent_backend(self) -> None:
        with mock.patch("aha_cli.backends.registry.subprocess.run", side_effect=FileNotFoundError):
            config = {"codex": {"bin": "missing-codex-for-test"}}
            codex_options = model_options("codex", config)
            backend_options = agent_backends(config)
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
        self.assertIn("commands", backend_options[0])

    def test_codex_model_options_are_loaded_from_codex_catalog(self) -> None:
        registry._CODEX_MODEL_OPTIONS_CACHE.clear()
        catalog = {
            "models": [
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 1},
                {
                    "slug": "gpt-new",
                    "display_name": "GPT New",
                    "visibility": "list",
                    "priority": 3,
                    "default_reasoning_level": "high",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}],
                },
                {"slug": "gpt-fast", "display_name": "GPT Fast", "visibility": "list", "priority": 2},
            ]
        }
        completed = subprocess.CompletedProcess(
            ["test-codex", "debug", "models"],
            0,
            stdout=json.dumps(catalog),
            stderr="",
        )

        with mock.patch("aha_cli.backends.registry.subprocess.run", return_value=completed) as run:
            options = model_options("codex", {"codex": {"bin": "test-codex"}})

        self.assertEqual(options[0]["name"], "")
        self.assertEqual(options[0]["label"], f"default ({CODEX_DEFAULT_MODEL})")
        self.assertIn({"name": "xhigh", "label": "xhigh"}, options[0]["reasoning_efforts"])
        self.assertEqual(
            options[1:],
            [
                {"name": "gpt-fast", "label": "GPT Fast"},
                {
                    "name": "gpt-new",
                    "label": "GPT New",
                    "reasoning_efforts": [
                        {"name": "", "label": "default"},
                        {"name": "low", "label": "low"},
                        {"name": "high", "label": "high"},
                        {"name": "ultra", "label": "ultra"},
                    ],
                    "default_reasoning_effort": "high",
                },
            ],
        )
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["test-codex", "debug", "models"])

    def test_codex_model_catalog_uses_user_backend_paths(self) -> None:
        registry._CODEX_MODEL_OPTIONS_CACHE.clear()
        catalog = {"models": [{"slug": "gpt-new", "display_name": "GPT New", "visibility": "list"}]}
        completed = subprocess.CompletedProcess(
            ["test-codex-path", "debug", "models"],
            0,
            stdout=json.dumps(catalog),
            stderr="",
        )

        def add_paths(env: dict[str, str]) -> None:
            env["PATH"] = "/test/user/bin"

        with (
            mock.patch("aha_cli.backends.registry.add_user_backend_paths", side_effect=add_paths) as paths,
            mock.patch("aha_cli.backends.registry.subprocess.run", return_value=completed) as run,
        ):
            options = model_options("codex", {"codex": {"bin": "test-codex-path"}})

        self.assertIn({"name": "gpt-new", "label": "GPT New"}, options)
        paths.assert_called_once()
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/test/user/bin")

    def test_codex_model_options_fall_back_when_catalog_is_unavailable(self) -> None:
        registry._CODEX_MODEL_OPTIONS_CACHE.clear()
        with mock.patch("aha_cli.backends.registry.subprocess.run", side_effect=FileNotFoundError):
            options = model_options("codex", {"codex": {"bin": "missing-codex-catalog"}})

        self.assertIn("gpt-5.3-codex", {item["name"] for item in options})

    def test_codex_default_model_resolves_explicitly(self) -> None:
        self.assertEqual(resolve_model("codex", None), CODEX_DEFAULT_MODEL)
        self.assertEqual(resolve_model("codex", ""), CODEX_DEFAULT_MODEL)
        self.assertEqual(resolve_model("codex", "default"), CODEX_DEFAULT_MODEL)
        self.assertEqual(resolve_model("codex", "gpt-5.4"), "gpt-5.4")
        self.assertIsNone(resolve_model("claude", None))
