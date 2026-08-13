from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.domain.models import (
    normalize_agents_config,
    normalize_feishu_integration_config,
    resolve_group_digital_human_permissions,
)
from aha_cli.services.feishu_runtime import (
    _create_feishu_channel,
    _feishu_env_groups,
    _install_bot_menu_dispatcher,
    feishu_credentials,
    feishu_runtime_path,
    feishu_status,
    update_feishu_notifications_enabled,
    update_feishu_settings,
)
from aha_cli.services.feishu_group import ensure_feishu_group_run
from aha_cli.store.filesystem import create_plan
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

    def test_bot_menu_dispatcher_patch_survives_sdk_start_rebuild(self) -> None:
        class Dispatcher:
            def __init__(self) -> None:
                self._processorMap = {"p2.im.message.receive_v1": object()}

        class Channel:
            def __init__(self) -> None:
                self._dispatcher = None
                self.build_count = 0

            def _build_dispatcher(self) -> Dispatcher:
                self.build_count += 1
                self._dispatcher = Dispatcher()
                return self._dispatcher

            async def _invoke(self, name: str, event: object) -> None:
                return None

            def schedule(self, coroutine: object) -> None:
                close = getattr(coroutine, "close", None)
                if close:
                    close()

        channel = Channel()

        _install_bot_menu_dispatcher(channel)
        self.assertIsNone(channel._dispatcher)
        dispatcher = channel._build_dispatcher()

        self.assertEqual(channel.build_count, 1)
        self.assertIn("p2.im.message.receive_v1", dispatcher._processorMap)
        self.assertIn("p2.application.bot.menu_v6", dispatcher._processorMap)
        self.assertIn("p1.application.bot.menu_v6", dispatcher._processorMap)

        _install_bot_menu_dispatcher(channel)
        rebuilt = channel._build_dispatcher()
        self.assertEqual(channel.build_count, 2)
        self.assertIn("p2.application.bot.menu_v6", rebuilt._processorMap)

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

    def test_feishu_env_groups_honor_model_source(self) -> None:
        config = {
            "codex": {
                "model_source": "official",
                "env": [{"name": "codex-gw", "OPENAI_MODEL": "deepseek-v4-flash"}],
            },
            "claude": {
                "model_source": "env",
                "env": [{"name": "claude-gw", "ANTHROPIC_MODEL": "claude-deepseek-v4-flash"}],
            },
        }

        groups = _feishu_env_groups(config)

        # codex official -> no env groups listed.
        self.assertEqual(groups["codex"], [])
        # claude env -> env group listed.
        self.assertEqual(groups["claude"], [{"name": "claude-gw", "model": "claude-deepseek-v4-flash"}])

    def test_feishu_env_groups_default_to_both(self) -> None:
        config = {
            "codex": {"env": [{"name": "codex-gw", "OPENAI_MODEL": "gpt-5.6-sol"}]},
            "claude": {"env": [{"name": "claude-gw", "ANTHROPIC_MODEL": "deepseek-v4-flash"}]},
        }

        groups = _feishu_env_groups(config)

        self.assertEqual(groups["codex"], [{"name": "codex-gw", "model": "gpt-5.6-sol"}])
        self.assertEqual(groups["claude"], [{"name": "claude-gw", "model": "deepseek-v4-flash"}])

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

    def test_group_permissions_are_normalized_with_defaults(self) -> None:
        config = normalize_feishu_integration_config(
            {
                "group_permissions": {
                    "read_paths": ["/data/proj"],
                    "allow_common_knowledge": True,
                    "allowed_topics": ["vega", "hlcloud"],
                    "handoff_always": "payment, legal",
                }
            }
        )
        self.assertEqual(config["group_permissions"]["read_paths"], ["/data/proj"])
        self.assertTrue(config["group_permissions"]["allow_common_knowledge"])
        self.assertEqual(config["group_permissions"]["allowed_topics"], ["vega", "hlcloud"])
        self.assertEqual(config["group_permissions"]["handoff_always"], ["payment", "legal"])

        empty = normalize_feishu_integration_config({})
        self.assertEqual(empty["group_permissions"]["read_paths"], [])
        self.assertFalse(empty["group_permissions"]["allow_common_knowledge"])
        self.assertEqual(empty["group_permissions"]["allowed_topics"], [])
        self.assertEqual(empty["group_permissions"]["handoff_always"], [])

    def test_agents_section_normalizes_group_digital_human_permissions(self) -> None:
        config = normalize_agents_config(
            {
                "group_digital_human": {
                    "permissions": {
                        "read_paths": "/data/proj\n/data/docs",
                        "allow_common_knowledge": True,
                        "allowed_topics": ["vega", "hlcloud"],
                        "handoff_always": "payment, legal",
                    }
                }
            }
        )
        group = config["group_digital_human"]["permissions"]
        self.assertEqual(group["read_paths"], ["/data/proj", "/data/docs"])
        self.assertTrue(group["allow_common_knowledge"])
        self.assertEqual(group["allowed_topics"], ["vega", "hlcloud"])
        self.assertEqual(group["handoff_always"], ["payment", "legal"])

        defaults = normalize_agents_config({})
        self.assertEqual(defaults["group_digital_human"]["permissions"]["read_paths"], [])
        self.assertFalse(defaults["group_digital_human"]["permissions"]["allow_common_knowledge"])
        self.assertEqual(defaults["group_digital_human"]["permissions"]["allowed_topics"], [])

    def test_resolve_group_digital_human_permissions_prefers_agents_over_legacy(self) -> None:
        config = {
            "agents": {
                "group_digital_human": {
                    "permissions": {"read_paths": ["/new"], "allow_common_knowledge": True, "allowed_topics": ["new"], "handoff_always": []}
                }
            },
            "integrations": {
                "feishu": {
                    "group_permissions": {
                        "read_paths": ["/legacy"],
                        "allowed_topics": ["legacy"],
                        "handoff_always": [],
                    }
                }
            },
        }
        resolved = resolve_group_digital_human_permissions(config)
        self.assertEqual(resolved["read_paths"], ["/new"])
        self.assertTrue(resolved["allow_common_knowledge"])
        self.assertEqual(resolved["allowed_topics"], ["new"])

    def test_resolve_group_digital_human_permissions_falls_back_to_legacy(self) -> None:
        config = {
            "integrations": {
                "feishu": {
                    "group_permissions": {
                        "read_paths": ["/legacy-path"],
                        "allow_common_knowledge": True,
                        "allowed_topics": ["legacy-topic"],
                        "handoff_always": ["legacy-handoff"],
                    }
                }
            }
        }
        resolved = resolve_group_digital_human_permissions(config)
        self.assertEqual(resolved["read_paths"], ["/legacy-path"])
        self.assertTrue(resolved["allow_common_knowledge"])
        self.assertEqual(resolved["allowed_topics"], ["legacy-topic"])
        self.assertEqual(resolved["handoff_always"], ["legacy-handoff"])

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
                    "default_run_id": "",
                    "proxy_enabled": True,
                    "owner_open_id": "ou_owner",
                    "owner_chat_id": "oc_owner",
                    "allowed_open_ids": "ou_a, ou_b, ou_a",
                    "allowed_chat_ids": "oc_a, oc_b, oc_a",
                    "group_access_mode": "all_members",
                    "group_mentions_only": False,
                    "notifications_enabled": False,
                    "security_mode": "strict",
                    "ignored": "value",
                },
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(status["enabled"])
        self.assertEqual(status["allowed_open_ids"], ["ou_owner", "ou_a", "ou_b"])
        self.assertEqual(status["owner_open_id"], "ou_owner")
        self.assertEqual(status["owner_chat_id"], "oc_owner")
        self.assertEqual(status["allowed_chat_ids"], ["oc_a", "oc_b"])
        self.assertEqual(status["allowed_chat_id_count"], 2)
        self.assertEqual(status["group_access_mode"], "all_members")
        self.assertEqual(saved["integrations"]["feishu"]["app_secret"], "stored-secret")
        self.assertEqual(saved["integrations"]["feishu"]["app_id"], "cli_new")
        self.assertEqual(status["effective_backend"], "claude")
        self.assertEqual(status["effective_model"], "claude-sonnet-4-6")
        self.assertEqual(status["effective_reasoning_effort"], "high")
        self.assertEqual(status["default_run_id"], "")
        self.assertTrue(status["effective_proxy_enabled"])
        self.assertEqual(saved["integrations"]["feishu"]["backend"], "claude")
        self.assertEqual(saved["integrations"]["feishu"]["model"], "claude-sonnet-4-6")
        self.assertEqual(saved["integrations"]["feishu"]["reasoning_effort"], "high")
        self.assertEqual(saved["integrations"]["feishu"]["default_run_id"], "")
        self.assertTrue(saved["integrations"]["feishu"]["proxy_enabled"])
        self.assertEqual(saved["integrations"]["feishu"]["owner_open_id"], "ou_owner")
        self.assertEqual(saved["integrations"]["feishu"]["owner_chat_id"], "oc_owner")
        self.assertEqual(saved["integrations"]["feishu"]["allowed_open_ids"], ["ou_owner", "ou_a", "ou_b"])
        self.assertNotIn("ignored", saved["integrations"]["feishu"])
        self.assertEqual(saved["integrations"]["custom"], {"keep": True})

    def test_settings_default_run_accepts_only_non_system_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = create_plan(root, "Work run", 1, "implementation", [], [], backend="stub", create_default_tasks=False)
            system_run = ensure_feishu_group_run(root, {"backend": "stub"})

            status = update_feishu_settings(root, {"default_run_id": work["id"]})
            with self.assertRaises(ValueError):
                update_feishu_settings(root, {"default_run_id": system_run})

        self.assertEqual(status["default_run_id"], work["id"])
        self.assertEqual([item["id"] for item in status["work_run_options"]], [work["id"]])
        self.assertTrue(status["default_run_available"])

    def test_status_exposes_recent_groups_for_authenticated_settings_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from aha_cli.services.feishu import record_recent_group, record_recent_private_chat

            record_recent_group(root, "oc_old", seen_at="2026-08-01T00:00:00Z", display_name="旧群")
            record_recent_group(root, "oc_new", seen_at="2026-08-02T00:00:00Z", display_name="新群")
            record_recent_private_chat(
                root,
                chat_id="oc_private",
                open_id="ou_owner",
                display_name="主人",
                seen_at="2026-08-03T00:00:00Z",
            )
            status = feishu_status(root)

        self.assertEqual([item["chat_id"] for item in status["recent_groups"]], ["oc_new", "oc_old"])
        self.assertEqual(status["recent_groups"][0]["display_name"], "新群")
        self.assertEqual(status["recent_private_chats"][0]["open_id"], "ou_owner")
        self.assertEqual(status["identity_profiles"]["open_ids"]["ou_owner"]["display_name"], "主人")

    def test_connected_status_refreshes_feishu_identity_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_runtime.refresh_identity_profiles",
            return_value={"attempted": 2, "updated": 2, "errors": []},
        ) as refresh:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "integrations": {
                            "feishu": {
                                "enabled": True,
                                "app_id": "cli_app",
                                "app_secret": "secret",
                                "owner_open_id": "ou_owner",
                                "allowed_open_ids": ["ou_owner"],
                                "allowed_chat_ids": ["oc_group"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            feishu_runtime_path(root).parent.mkdir(parents=True, exist_ok=True)
            feishu_runtime_path(root).write_text(json.dumps({"status": "connected"}), encoding="utf-8")
            status = feishu_status(root)

        self.assertEqual(status["identity_profile_refresh"], {"attempted": 2, "updated": 2, "errors": []})
        refresh.assert_called_once()
        self.assertIn("ou_owner", refresh.call_args.kwargs["open_ids"])
        self.assertIn("oc_group", refresh.call_args.kwargs["chat_ids"])

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

    def test_system_route_cleans_old_feishu_app_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.web.system_routes.cleanup_feishu_old_app_state",
            return_value={"cleanup": {"ok": True, "dry_run": True}, "feishu": {"configured": True}},
        ) as cleanup:
            root = Path(tmp)
            response = system_route_response(
                root,
                "",
                "POST",
                "/api/feishu/cleanup-old-app",
                {},
                json.dumps({"dry_run": True}).encode("utf-8"),
            )
            body = json_response_body(response)

        self.assertTrue(body["ok"])
        self.assertTrue(body["cleanup"]["dry_run"])
        cleanup.assert_called_once_with(root, dry_run=True)


if __name__ == "__main__":
    unittest.main()
