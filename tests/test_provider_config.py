from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from aha_cli.services.provider_config import normalize_configured_models, normalize_providers, sync_legacy_backend_env
from aha_cli.store.io import read_json
from tests.helpers import fetch_ui_response, json_response_body


class ProviderConfigTests(unittest.TestCase):
    def test_normalize_providers_defaults_authentication_to_auto(self) -> None:
        providers = normalize_providers([
            {"id": "gateway", "name": "Gateway", "base_url": "https://gateway.test", "credential": "secret"},
        ])

        self.assertEqual(providers[0]["auth_style"], "auto")

    def test_normalize_providers_keeps_existing_credential_on_empty_update(self) -> None:
        existing = [{"id": "gateway", "name": "Old", "base_url": "https://old.test", "auth_style": "bearer", "credential": "secret"}]

        providers = normalize_providers(
            [{"id": "gateway", "name": "New", "base_url": "https://new.test", "auth_style": "x-api-key", "credential": ""}],
            existing,
        )

        self.assertEqual(providers[0]["credential"], "secret")
        self.assertEqual(providers[0]["name"], "New")

    def test_normalize_providers_keeps_anthropic_base_url(self) -> None:
        providers = normalize_providers([
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "anthropic_base_url": "https://api.deepseek.com/anthropic", "auth_style": "bearer", "credential": "secret"},
        ])

        self.assertEqual(providers[0]["anthropic_base_url"], "https://api.deepseek.com/anthropic")

    def test_sync_legacy_env_uses_provider_anthropic_base_url_for_claude(self) -> None:
        cfg = {
            "providers": [{"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "anthropic_base_url": "https://api.deepseek.com/anthropic", "auth_style": "bearer", "credential": "secret"}],
            "configured_models": [
                {"provider_id": "deepseek", "model_id": "deepseek-v4-pro", "backend": "claude", "wire_api": "chat_completions"},
            ],
            "codex": {"env": []},
            "claude": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        self.assertEqual(cfg["claude"]["env"][0]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(cfg["claude"]["env"][0]["ANTHROPIC_MODEL"], "deepseek-v4-pro")

    def test_sync_legacy_env_falls_back_to_base_url_without_anthropic_base(self) -> None:
        cfg = {
            "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}],
            "configured_models": [
                {"provider_id": "p1", "model_id": "model-a", "backend": "claude", "wire_api": "anthropic_messages"},
            ],
            "codex": {"env": []},
            "claude": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        self.assertEqual(cfg["claude"]["env"][0]["ANTHROPIC_BASE_URL"], "https://gateway.test/v1")

    def test_sync_legacy_env_allows_same_model_on_both_backends(self) -> None:
        cfg = {
            "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}],
            "configured_models": [
                {"provider_id": "p1", "model_id": "shared-model", "backend": "codex", "wire_api": "responses"},
                {"provider_id": "p1", "model_id": "shared-model", "backend": "claude", "wire_api": "anthropic_messages"},
            ],
            "codex": {"env": []},
            "claude": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        self.assertEqual(cfg["codex"]["env"][0]["OPENAI_MODEL"], "shared-model")
        self.assertEqual(cfg["codex"]["env"][0]["CODEX_WIRE_API"], "responses")
        self.assertEqual(cfg["claude"]["env"][0]["ANTHROPIC_MODEL"], "shared-model")
        self.assertEqual(cfg["claude"]["env"][0]["ANTHROPIC_AUTH_TOKEN"], "secret")

    def test_sync_legacy_env_generates_opencode_provider_binding_without_secret(self) -> None:
        cfg = {
            "providers": [{
                "id": "gateway",
                "name": "Gateway",
                "base_url": "https://gateway.test/v1",
                "auth_style": "bearer",
                "credential": "secret",
            }],
            "configured_models": [{
                "provider_id": "gateway",
                "model_id": "model-a",
                "backend": "opencode",
                "wire_api": "chat_completions",
                "context_window": 200000,
            }],
            "codex": {"env": []},
            "claude": {"env": []},
            "opencode": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        group = cfg["opencode"]["env"][0]
        self.assertEqual(group["AHA_PROVIDER_ID"], "gateway")
        self.assertEqual(group["OPENCODE_MODEL"], "model-a")
        self.assertEqual(group["OPENCODE_WIRE_API"], "chat_completions")
        self.assertEqual(group["OPENCODE_CONTEXT_WINDOW"], "200000")
        self.assertNotIn("credential", group)
        self.assertNotIn("secret", json.dumps(group))

    def test_normalize_configured_models_accepts_opencode_backend(self) -> None:
        models = normalize_configured_models(
            [{
                "provider_id": "p1",
                "model_id": "model-a",
                "backend": "opencode",
                "wire_api": "responses",
                "max_output_tokens": 64000,
            }],
            ["p1"],
        )

        self.assertEqual(models[0]["backend"], "opencode")
        self.assertEqual(models[0]["max_output_tokens"], 64000)

    def test_sync_legacy_env_propagates_configured_model_context_window(self) -> None:
        cfg = {
            "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}],
            "configured_models": [
                {"provider_id": "p1", "model_id": "deepseek-v4-flash", "backend": "claude", "wire_api": "anthropic_messages", "context_window": "262144"},
                {"provider_id": "p1", "model_id": "gpt-5.5", "backend": "codex", "wire_api": "responses", "context_window": 258000},
            ],
            "codex": {"env": []},
            "claude": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        self.assertEqual(cfg["claude"]["env"][0]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "262144")
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", cfg["codex"]["env"][0])
        self.assertNotIn("OPENAI_MODEL", cfg["claude"]["env"][0])

    def test_normalize_configured_models_keeps_role_models(self) -> None:
        models = normalize_configured_models(
            [{
                "provider_id": "p1",
                "model_id": "deepseek-v4-flash",
                "backend": "claude",
                "wire_api": "anthropic_messages",
                "context_window": "262144",
                "fable_model": "fable-x",
                "opus_model": "opus-y",
                "sonnet_model": "sonnet-z",
                "haiku_model": "haiku-w",
            }],
            ["p1"],
        )

        self.assertEqual(models[0]["context_window"], "262144")
        self.assertEqual(models[0]["fable_model"], "fable-x")
        self.assertEqual(models[0]["opus_model"], "opus-y")
        self.assertEqual(models[0]["sonnet_model"], "sonnet-z")
        self.assertEqual(models[0]["haiku_model"], "haiku-w")

    def test_normalize_configured_models_keeps_auto_compact_threshold(self) -> None:
        models = normalize_configured_models(
            [
                {"provider_id": "p1", "model_id": "deepseek-v4-flash", "backend": "claude", "wire_api": "anthropic_messages", "auto_compact_threshold_percent": "60"},
                {"provider_id": "p1", "model_id": "gpt-5.5", "backend": "codex", "wire_api": "responses", "auto_compact_threshold_percent": 120},
            ],
            ["p1"],
        )

        self.assertEqual(models[0]["auto_compact_threshold_percent"], 60)
        self.assertNotIn("auto_compact_threshold_percent", models[1])

    def test_sync_legacy_env_translates_auto_compact_threshold(self) -> None:
        cfg = {
            "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}],
            "configured_models": [
                {"provider_id": "p1", "model_id": "deepseek-v4-flash", "backend": "claude", "wire_api": "anthropic_messages", "auto_compact_threshold_percent": 60},
                {"provider_id": "p1", "model_id": "gpt-5.5", "backend": "codex", "wire_api": "responses", "context_window": 258000, "auto_compact_threshold_percent": 60},
                {"provider_id": "p1", "model_id": "kimi-k3", "backend": "codex", "wire_api": "responses", "auto_compact_threshold_percent": 60},
            ],
            "codex": {"env": []},
            "claude": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        claude_group = cfg["claude"]["env"][0]
        self.assertEqual(claude_group["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "60")
        gpt_group = cfg["codex"]["env"][0]
        self.assertEqual(gpt_group["CODEX_AUTO_COMPACT_THRESHOLD_PERCENT"], "60")
        self.assertNotIn("CODEX_AUTO_COMPACT_TOKEN_LIMIT", gpt_group)
        # Codex has no percent knob of its own; without a context window the
        # threshold cannot be translated and is left to the backend default.
        kimi_group = next(group for group in cfg["codex"]["env"] if group.get("OPENAI_MODEL") == "kimi-k3")
        self.assertNotIn("CODEX_AUTO_COMPACT_TOKEN_LIMIT", kimi_group)

    def test_sync_legacy_env_propagates_claude_role_models(self) -> None:
        cfg = {
            "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}],
            "configured_models": [
                {"provider_id": "p1", "model_id": "deepseek-v4-flash", "backend": "claude", "wire_api": "anthropic_messages", "context_window": "262144", "opus_model": "claude-opus-5"},
            ],
            "codex": {"env": []},
            "claude": {"env": []},
        }

        sync_legacy_backend_env(cfg)

        claude_group = cfg["claude"]["env"][0]
        self.assertEqual(claude_group["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-5")
        self.assertNotIn("ANTHROPIC_DEFAULT_FABLE_MODEL", claude_group)

    def test_bootstrap_persists_provider_but_never_returns_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(fetch_ui_response(
                root,
                "",
                "/api/bootstrap",
                method="POST",
                payload={
                    "backend": "codex",
                    "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "super-secret"}],
                    "configured_models": [{"provider_id": "p1", "model_id": "model-a", "backend": "codex", "wire_api": "responses"}],
                },
            ))
            body = json_response_body(response)
            stored = read_json(root / "config.json")

        self.assertEqual(stored["providers"][0]["credential"], "super-secret")
        self.assertNotIn("super-secret", response.decode("utf-8"))
        self.assertTrue(body["config"]["providers"][0]["credential_configured"])
        self.assertNotIn("credential", body["config"]["providers"][0])
        self.assertNotIn("OPENAI_API_KEY", body["config"]["codex"]["env"][0])

    def test_bootstrap_persists_opencode_provider_binding_without_copying_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(fetch_ui_response(
                root,
                "",
                "/api/bootstrap",
                method="POST",
                payload={
                    "backend": "opencode",
                    "providers": [{
                        "id": "gateway",
                        "name": "Gateway",
                        "base_url": "https://gateway.test/v1",
                        "auth_style": "bearer",
                        "credential": "opencode-provider-secret",
                    }],
                    "configured_models": [{
                        "provider_id": "gateway",
                        "model_id": "model-a",
                        "backend": "opencode",
                        "wire_api": "responses",
                        "context_window": 200000,
                        "max_output_tokens": 64000,
                    }],
                    "opencode": {
                        "bin": "opencode",
                        "model": "env:gateway-model-a",
                        "model_source": "env",
                    },
                },
            ))
            body = json_response_body(response)
            stored = read_json(root / "config.json")

        self.assertEqual(stored["backend"], "opencode")
        self.assertEqual(stored["configured_models"][0]["backend"], "opencode")
        self.assertEqual(stored["configured_models"][0]["max_output_tokens"], 64000)
        self.assertEqual(stored["opencode"]["env"][0]["AHA_PROVIDER_ID"], "gateway")
        self.assertEqual(stored["opencode"]["env"][0]["OPENCODE_MODEL"], "model-a")
        self.assertNotIn("opencode-provider-secret", json.dumps(stored["opencode"]))
        self.assertNotIn("opencode-provider-secret", response.decode("utf-8"))
        self.assertTrue(body["config"]["providers"][0]["credential_configured"])

    def test_bootstrap_persists_and_returns_anthropic_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(fetch_ui_response(
                root,
                "",
                "/api/bootstrap",
                method="POST",
                payload={
                    "backend": "codex",
                    "providers": [{"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "anthropic_base_url": "https://api.deepseek.com/anthropic", "auth_style": "bearer", "credential": "sk-anthropic-test-xyz"}],
                    "configured_models": [{"provider_id": "deepseek", "model_id": "deepseek-v4-pro", "backend": "claude", "wire_api": "chat_completions"}],
                },
            ))
            body = json_response_body(response)
            stored = read_json(root / "config.json")

        self.assertEqual(stored["providers"][0]["anthropic_base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(stored["claude"]["env"][0]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(body["config"]["providers"][0]["anthropic_base_url"], "https://api.deepseek.com/anthropic")
        self.assertNotIn("sk-anthropic-test-xyz", response.decode("utf-8"))

    def test_bootstrap_redacts_legacy_custom_secret_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({
                "codex": {"env": [{"name": "legacy", "OPENAI_ACCESS_TOKEN": "legacy-secret", "OPENAI_MODEL": "model-a"}]},
            }), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap"))

        self.assertNotIn("legacy-secret", response.decode("utf-8"))
        self.assertNotIn("OPENAI_ACCESS_TOKEN", json_response_body(response)["config"]["codex"]["env"][0])

    def test_bootstrap_empty_credential_update_preserves_secret_and_explicit_delete_removes_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({
                "backend": "codex",
                "providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}],
                "configured_models": [],
            }), encoding="utf-8")
            asyncio.run(fetch_ui_response(root, "", "/api/bootstrap", method="POST", payload={
                "force": True,
                "backend": "codex",
                "providers": [{"id": "p1", "name": "Renamed", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": ""}],
                "configured_models": [],
            }))
            kept = read_json(root / "config.json")
            asyncio.run(fetch_ui_response(root, "", "/api/bootstrap", method="POST", payload={
                "force": True,
                "backend": "codex",
                "providers": [],
                "configured_models": [],
            }))
            deleted = read_json(root / "config.json")

        self.assertEqual(kept["providers"][0]["credential"], "secret")
        self.assertEqual(kept["providers"][0]["name"], "Renamed")
        self.assertEqual(deleted["providers"], [])
        self.assertEqual(deleted["codex"]["env"], [])

    def test_detect_models_uses_saved_provider_without_returning_secret(self) -> None:
        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return json.dumps({"data": [{"id": "model-a"}]}).encode()

        with tempfile.TemporaryDirectory() as tmp, mock.patch("aha_cli.web.run_routes.urlopen", return_value=FakeResponse()) as opened:
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "bearer", "credential": "secret"}]}), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/detect-models", method="POST", payload={"provider_id": "p1"}))
            request = opened.call_args[0][0]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(response)["models"], [{"id": "model-a"}])
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertNotIn("secret", response.decode())

    def test_detect_models_auto_detects_authentication_header(self) -> None:
        calls = []

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return json.dumps({"data": [{"id": "model-a"}]}).encode()

        def fake_open(request, **_kwargs):
            calls.append(request)
            if request.headers.get("Authorization"):
                raise HTTPError(request.full_url, 401, "unauthorized", {}, None)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp, mock.patch("aha_cli.web.run_routes.urlopen", side_effect=fake_open):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test/v1", "auth_style": "auto", "credential": "secret"}]}), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/detect-models", method="POST", payload={"provider_id": "p1"}))

        body = json_response_body(response)
        self.assertEqual(body["auth_style"], "x-api-key")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].headers["X-api-key"], "secret")

    def test_detect_models_uses_opencode_zen_catalog_and_catalog_capabilities(self) -> None:
        catalog = [{
            "id": "big-pickle",
            "mode": "chat_completions",
            "max_input_tokens": 200000,
            "max_output_tokens": 32000,
        }]
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "aha_cli.web.run_routes.detect_opencode_zen_models_for_runtime",
                return_value=catalog,
            ) as detect,
        ):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({
                "providers": [{
                    "id": "zen",
                    "name": "OpenCode Zen",
                    "base_url": "https://opencode.ai/zen/v1",
                    "auth_style": "bearer",
                    "credential": "zen-secret",
                }],
            }), encoding="utf-8")
            detected_response = asyncio.run(fetch_ui_response(
                root,
                "",
                "/api/detect-models",
                method="POST",
                payload={"provider_id": "zen"},
            ))
            tested_response = asyncio.run(fetch_ui_response(
                root,
                "",
                "/api/detect-models/test",
                method="POST",
                payload={"provider_id": "zen", "models": ["big-pickle"]},
            ))

        detected = json_response_body(detected_response)
        tested = json_response_body(tested_response)
        self.assertEqual(detected["models"], catalog)
        self.assertEqual(detected["auth_style"], "bearer")
        capabilities = tested["results"][0]["capabilities"]
        self.assertEqual(capabilities["chat_completions"]["status"], "supported")
        self.assertEqual(capabilities["responses"]["status"], "unsupported")
        self.assertEqual(
            capabilities["chat_completions"]["source"],
            "opencode_catalog",
        )
        self.assertEqual(detect.call_count, 2)
        self.assertNotIn("zen-secret", detected_response.decode("utf-8"))
        self.assertNotIn("zen-secret", tested_response.decode("utf-8"))

    def test_capability_probe_classifies_each_protocol_for_selected_models(self) -> None:
        calls = []

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        def fake_open(request, **_kwargs):
            calls.append(request)
            if request.full_url.endswith("/responses"):
                return FakeResponse()
            if request.full_url.endswith("/chat/completions"):
                raise HTTPError(request.full_url, 404, "not found", {}, None)
            raise HTTPError(request.full_url, 429, "limited", {}, None)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("aha_cli.web.run_routes.urlopen", side_effect=fake_open):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test", "auth_style": "x-api-key", "credential": "secret"}]}), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/detect-models/test", method="POST", payload={"provider_id": "p1", "models": ["chosen-model"]}))

        body = json_response_body(response)
        capabilities = body["results"][0]["capabilities"]
        self.assertEqual(capabilities["responses"]["status"], "supported")
        self.assertEqual(capabilities["chat_completions"]["status"], "unsupported")
        self.assertEqual(capabilities["anthropic_messages"]["status"], "rate_limited")
        self.assertEqual(len(calls), 3)
        for request in calls:
            payload = json.loads(request.data.decode())
            self.assertEqual(payload["model"], "chosen-model")
            self.assertIn(1, [payload.get("max_tokens"), payload.get("max_output_tokens")])
        self.assertNotIn("secret", response.decode())

    def test_capability_probe_falls_back_to_anthropic_base(self) -> None:
        calls = []

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        def fake_open(request, **_kwargs):
            calls.append(request.full_url)
            if request.full_url.endswith("/anthropic/v1/messages"):
                return FakeResponse()
            raise HTTPError(request.full_url, 404, "not found", {}, None)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("aha_cli.web.run_routes.urlopen", side_effect=fake_open):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"providers": [{"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "auth_style": "bearer", "credential": "secret"}]}), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/detect-models/test", method="POST", payload={"provider_id": "deepseek", "models": ["deepseek-v4-pro"]}))

        body = json_response_body(response)
        result = body["results"][0]
        self.assertEqual(result["capabilities"]["anthropic_messages"]["status"], "supported")
        self.assertEqual(result["anthropic_base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(len(calls), 4)  # responses, chat, messages@root(404), messages@/anthropic
        self.assertNotIn("secret", response.decode())

    def test_capability_probe_does_not_fallback_on_rate_limit(self) -> None:
        calls = []

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        def fake_open(request, **_kwargs):
            calls.append(request.full_url)
            if request.full_url.endswith("/v1/responses") or request.full_url.endswith("/v1/chat/completions"):
                raise HTTPError(request.full_url, 404, "not found", {}, None)
            raise HTTPError(request.full_url, 429, "limited", {}, None)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("aha_cli.web.run_routes.urlopen", side_effect=fake_open):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test", "auth_style": "x-api-key", "credential": "secret"}]}), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/detect-models/test", method="POST", payload={"provider_id": "p1", "models": ["chosen-model"]}))

        capabilities = json_response_body(response)["results"][0]["capabilities"]
        self.assertEqual(capabilities["anthropic_messages"]["status"], "rate_limited")
        self.assertEqual(len(calls), 3)  # no extra /anthropic probe on non-404

    def test_capability_probe_classifies_auth_and_unavailable(self) -> None:
        effects = [HTTPError("url", 401, "unauthorized", {}, None), HTTPError("url", 503, "down", {}, None), URLError("offline")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch("aha_cli.web.run_routes.urlopen", side_effect=effects):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"providers": [{"id": "p1", "name": "Gateway", "base_url": "https://gateway.test", "auth_style": "bearer", "credential": "secret"}]}), encoding="utf-8")
            response = asyncio.run(fetch_ui_response(root, "", "/api/detect-models/test", method="POST", payload={"provider_id": "p1", "model": "model-a"}))

        capabilities = json_response_body(response)["results"][0]["capabilities"]
        self.assertEqual(capabilities["responses"]["status"], "auth_error")
        self.assertEqual(capabilities["chat_completions"]["status"], "unavailable")
        self.assertEqual(capabilities["anthropic_messages"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
