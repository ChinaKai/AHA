from __future__ import annotations

import json
import unittest
from urllib.request import urlopen

from aha_cli.backends.codex_litellm_bridge import start_litellm_responses_bridge


class CodexLitellmBridgeTests(unittest.TestCase):
    def test_models_endpoint_returns_codex_model_metadata_shape(self) -> None:
        with start_litellm_responses_bridge(
            bridge_config={
                "client_model": "kimi-k2.6",
                "upstream_model": "kimi-for-coding",
                "upstream_base_url": "https://api.kimi.com/coding/v1",
            },
            api_key="test-key",
        ) as bridge:
            with urlopen(f"{bridge.base_url}/models", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))

        model = body["models"][0]
        self.assertEqual(model["slug"], "kimi-k2.6")
        self.assertEqual(model["display_name"], "kimi-k2.6")
        self.assertEqual(model["shell_type"], "shell_command")
        self.assertEqual(model["base_instructions"], "")
        self.assertEqual(model["max_context_window"], 262144)
        self.assertFalse(model["supports_search_tool"])
        self.assertIsInstance(model["supported_reasoning_levels"][0], dict)
        self.assertEqual(model["supported_reasoning_levels"][0]["effort"], "low")


if __name__ == "__main__":
    unittest.main()
