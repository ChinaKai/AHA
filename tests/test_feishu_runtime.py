from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.domain.models import normalize_feishu_integration_config
from aha_cli.services.feishu_runtime import (
    _create_feishu_channel,
    feishu_credentials,
    feishu_status,
    update_feishu_notifications_enabled,
    update_feishu_settings,
)
from aha_cli.web.system_routes import system_route_response
from tests.helpers import json_response_body


class FeishuRuntimeTests(unittest.TestCase):
    def test_channel_factory_constructs_sdk_with_expected_settings(self) -> None:
        channel_type = mock.Mock(return_value=object())
        security_type = mock.Mock(return_value="security")
        fake_sdk = mock.Mock(FeishuChannel=channel_type, SecurityConfig=security_type)
        with mock.patch.dict("sys.modules", {"lark_channel": fake_sdk}):
            channel = _create_feishu_channel("cli_test", "secret", "strict")

        self.assertIs(channel, channel_type.return_value)
        security_type.assert_called_once_with(mode="strict")
        channel_type.assert_called_once_with(
            app_id="cli_test",
            app_secret="secret",
            transport="ws",
            security="security",
        )

    def test_credentials_use_configured_environment_names(self) -> None:
        config = {"app_id": "", "app_id_env": "CUSTOM_ID", "app_secret_env": "CUSTOM_SECRET"}
        self.assertEqual(
            feishu_credentials(config, {"CUSTOM_ID": "cli_env", "CUSTOM_SECRET": "secret"}),
            ("cli_env", "secret"),
        )

    def test_credentials_prefer_settings_secret_over_environment(self) -> None:
        config = {
            "app_id": "cli_settings",
            "app_secret": "settings-secret",
            "app_secret_env": "CUSTOM_SECRET",
        }
        self.assertEqual(
            feishu_credentials(config, {"CUSTOM_SECRET": "environment-secret"}),
            ("cli_settings", "settings-secret"),
        )

    def test_status_never_returns_app_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ",
            {"AHA_FEISHU_APP_SECRET": "super-secret"},
            clear=False,
        ):
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "integrations": {
                            "feishu": {
                                "enabled": True,
                                "app_id": "cli_test",
                                "app_secret": "stored-secret",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            status = feishu_status(root)
        self.assertTrue(status["configured"])
        self.assertTrue(status["app_secret_configured"])
        self.assertNotIn("app_secret", status)
        self.assertNotIn("install_command", status)
        self.assertNotIn("web", status)
        self.assertNotIn("stored-secret", json.dumps(status))
        self.assertNotIn("super-secret", json.dumps(status))

    def test_legacy_feishu_web_settings_are_dropped(self) -> None:
        config = normalize_feishu_integration_config(
            {
                "enabled": True,
                "web": {
                    "enabled": True,
                    "public_base_url": "https://aha.example.com",
                    "session_ttl_seconds": 3600,
                },
            }
        )
        self.assertTrue(config["enabled"])
        self.assertNotIn("web", config)

    def test_system_route_exposes_feishu_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.system_routes.feishu_status",
            return_value={"enabled": True, "configured": True},
        ):
            response = system_route_response(Path(tmp), "", "GET", "/api/feishu", {}, b"")
            body = json_response_body(response)
        self.assertTrue(body["ok"])
        self.assertTrue(body["feishu"]["configured"])

    def test_notification_update_preserves_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "backend": "codex",
                        "custom": {"keep": True},
                        "integrations": {
                            "feishu": {
                                "enabled": True,
                                "app_id": "cli_test",
                                "app_secret": "secret",
                                "notifications_enabled": True,
                            },
                            "weixin": {"enabled": False, "visible": False},
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = update_feishu_notifications_enabled(root, False)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(status["notifications_enabled"])
        self.assertEqual(saved["custom"], {"keep": True})
        self.assertEqual(saved["integrations"]["feishu"]["app_secret"], "secret")
        self.assertFalse(saved["integrations"]["feishu"]["notifications_enabled"])
        self.assertEqual(saved["integrations"]["weixin"], {"enabled": False, "visible": False})

    def test_settings_update_preserves_secret_and_other_integrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "backend": "codex",
                        "integrations": {
                            "feishu": {"app_secret": "stored-secret", "app_id": "cli_old"},
                            "custom": {"keep": True},
                        },
                    }
                ),
                encoding="utf-8",
            )

            status = update_feishu_settings(
                root,
                {
                    "enabled": True,
                    "app_id": "cli_new",
                    "app_secret": "",
                    "backend": "claude",
                    "model": "claude-sonnet-4-6",
                    "reasoning_effort": "high",
                    "proxy_enabled": True,
                    "allowed_open_ids": "ou_a, ou_b, ou_a",
                    "group_mentions_only": False,
                    "notifications_enabled": False,
                    "security_mode": "strict",
                    "ignored": "value",
                },
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(status["enabled"])
        self.assertEqual(status["allowed_open_ids"], ["ou_a", "ou_b"])
        self.assertEqual(saved["integrations"]["feishu"]["app_secret"], "stored-secret")
        self.assertEqual(saved["integrations"]["feishu"]["app_id"], "cli_new")
        self.assertEqual(status["effective_backend"], "claude")
        self.assertEqual(status["effective_model"], "claude-sonnet-4-6")
        self.assertEqual(status["effective_reasoning_effort"], "high")
        self.assertTrue(status["effective_proxy_enabled"])
        self.assertEqual(saved["integrations"]["feishu"]["backend"], "claude")
        self.assertEqual(saved["integrations"]["feishu"]["model"], "claude-sonnet-4-6")
        self.assertEqual(saved["integrations"]["feishu"]["reasoning_effort"], "high")
        self.assertTrue(saved["integrations"]["feishu"]["proxy_enabled"])
        self.assertNotIn("ignored", saved["integrations"]["feishu"])
        self.assertEqual(saved["integrations"]["custom"], {"keep": True})

    def test_system_route_updates_all_feishu_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.system_routes.update_feishu_settings",
            return_value={"enabled": True, "configured": True},
        ) as update:
            root = Path(tmp)
            payload = {"enabled": True, "app_id": "cli_test"}
            response = system_route_response(
                root,
                "",
                "POST",
                "/api/feishu/settings",
                {},
                json.dumps(payload).encode("utf-8"),
            )
            body = json_response_body(response)

        self.assertTrue(body["ok"])
        update.assert_called_once_with(root, payload)

    def test_system_route_updates_feishu_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.system_routes.update_feishu_notifications_enabled",
            return_value={"notifications_enabled": False, "configured": True},
        ) as update:
            root = Path(tmp)
            response = system_route_response(
                root,
                "",
                "POST",
                "/api/feishu/notifications",
                {},
                json.dumps({"enabled": False}).encode("utf-8"),
            )
            body = json_response_body(response)

        self.assertTrue(body["ok"])
        self.assertFalse(body["feishu"]["notifications_enabled"])
        update.assert_called_once_with(root, False)


if __name__ == "__main__":
    unittest.main()
