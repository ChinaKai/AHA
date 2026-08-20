from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from aha_cli.domain.models import normalize_task_browser_control, normalize_task_token_saving, task_metadata_projection
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import create_plan, status_snapshot
from aha_cli.store.io import write_json
from aha_cli.store.runs import require_plan


class TaskSchemaTests(unittest.TestCase):
    def test_task_browser_control_defaults_and_normalizes_hosts(self) -> None:
        default_policy = normalize_task_browser_control()
        self.assertEqual(default_policy["mode"], "off")
        self.assertEqual(default_policy["start_url"], "https://www.bing.com/")
        self.assertEqual(default_policy["display"], "native")
        self.assertEqual(default_policy["device_mode"], "desktop")
        self.assertEqual(default_policy["runtime"], "playwright")
        self.assertEqual(default_policy["profile_name"], "")
        policy = normalize_task_browser_control({
            "mode": "managed",
            "browser_mode": "daily",
            "device_mode": "mobile",
            "allowed_hosts": "Example.com, *.Example.org\nhttps://invalid.example",
            "agent_access": "read_only",
            "downloads": "deny",
        })

        self.assertEqual(policy["mode"], "managed")
        self.assertEqual(policy["browser_mode"], "daily")
        self.assertEqual(policy["runtime"], "user_chrome")
        # Daily mode keeps a persistent per-task profile so logins survive restarts.
        self.assertEqual(policy["profile"], "task")
        self.assertEqual(policy["display"], "native")
        self.assertEqual(policy["device_mode"], "mobile")
        self.assertEqual(policy["allowed_hosts"], ["example.com", "*.example.org"])
        # Enabling a browser always grants full access and transfers.
        self.assertEqual(policy["agent_access"], "read_write")
        self.assertEqual(policy["downloads"], "allow")
        self.assertEqual(policy["uploads"], "allow")
        self.assertEqual(policy["proxy_mode"], "direct")
        self.assertEqual(policy["proxy_server"], "")
        # Privacy mode uses a clean managed browser.
        privacy = normalize_task_browser_control({"mode": "managed", "browser_mode": "privacy"})
        self.assertEqual(privacy["runtime"], "playwright")
        self.assertEqual(privacy["profile"], "task")

        proxied = normalize_task_browser_control({
            "proxy_mode": "custom",
            "proxy_server": "http://proxy.example:7890",
            "proxy_bypass": "localhost",
            "proxy_username": "alice",
            "proxy_password": "secret",
        })
        self.assertEqual(proxied["proxy_mode"], "custom")
        self.assertEqual(proxied["proxy_server"], "http://proxy.example:7890")
        self.assertEqual(proxied["proxy_password"], "secret")
        self.assertEqual(
            normalize_task_browser_control({"display": "invalid"})["display"],
            "native",
        )
        self.assertEqual(
            normalize_task_browser_control({"device_mode": "invalid"})["device_mode"],
            "desktop",
        )
        self.assertEqual(
            normalize_task_browser_control({"runtime": "invalid"})["runtime"],
            "playwright",
        )
        self.assertEqual(
            normalize_task_browser_control({"start_url": "https://example.com/"})["start_url"],
            "https://example.com/",
        )
        # Named profiles were removed: profile is derived from browser_mode and
        # profile_name is always empty.
        daily = normalize_task_browser_control({"browser_mode": "daily", "profile": "named", "profile_name": "工作"})
        self.assertEqual(daily["profile"], "task")
        self.assertEqual(daily["profile_name"], "")
        self.assertEqual(
            normalize_task_browser_control({"profile": "named", "profile_name": ""})["profile"],
            "task",
        )

    def test_task_token_saving_normalizes_related_project_keys(self) -> None:
        policy = normalize_task_token_saving({
            "enabled": True,
            "related_project_keys": [
                "project-b",
                "project-b",
                "bad/key",
                "project-c",
                "project-d",
                "project-e",
                "project-f",
                "project-g",
            ],
        })

        self.assertEqual(
            policy["related_project_keys"],
            ["project-b", "project-c", "project-d", "project-e", "project-f"],
        )

    def test_task_metadata_projection_normalizes_legacy_fields(self) -> None:
        projection = task_metadata_projection(
            {
                "workspace_id": "ws-001",
                "workspace_path": "/repo",
                "preferred_model": "gpt-5.5",
                "delegation_policy": "disabled",
                "max_sub_agents": 3,
                "supervision": {"max_rounds": 12},
                "context_management": {"enabled": True, "threshold_percent": 88},
                "task_skills": {"skills": "/repo/.aha/skills/board-debug/SKILL.md"},
                "hardware_debug": {
                    "enabled": True,
                    "devices": {"id": "legacy-id", "type": "legacy", "port": "/dev/ttyUSB0", "baud": "115200", "prompt": "Sgs #"},
                    "permissions": {"serial_write": "true", "reset": "on"},
                },
            },
            default_backend="claude",
        )

        self.assertEqual(projection["workspace_id"], "ws-001")
        self.assertEqual(projection["workspace_path"], "/repo")
        self.assertEqual(projection["preferred_backend"], "claude")
        self.assertEqual(projection["preferred_sub_backend"], "claude")
        self.assertEqual(projection["preferred_sub_model"], "gpt-5.5")
        self.assertEqual(projection["collaboration_mode"], "solo")
        self.assertEqual(projection["workflow_template"], "auto")
        self.assertEqual(projection["delegation_policy"], "disabled")
        self.assertEqual(projection["max_sub_agents"], 0)
        self.assertEqual(projection["supervision"]["max_rounds"], 12)
        self.assertTrue(projection["context_management"]["auto_compact_enabled"])
        self.assertEqual(projection["context_management"]["auto_compact_threshold_percent"], 88)
        self.assertTrue(projection["token_saving"]["enabled"])
        self.assertEqual(projection["token_saving"]["provider"], "nav")
        self.assertEqual(projection["token_saving"]["related_project_keys"], [])
        self.assertFalse(projection["observe_proxy"]["enabled"])
        self.assertEqual(projection["task_skills"]["enabled_paths"], ["/repo/.aha/skills/board-debug/SKILL.md"])
        self.assertEqual(projection["hardware_debug"]["mode"], "serial")
        self.assertEqual(projection["hardware_debug"]["serial"], {"device": "/dev/ttyUSB0", "baudrate": 115200})
        self.assertEqual(projection["hardware_debug"]["network"], {"device_ip": ""})
        self.assertEqual(projection["hardware_debug"]["credentials"], {"username": "", "password": ""})
        self.assertEqual(projection["hardware_debug"]["permissions"], {"access": "read_write"})
        self.assertNotIn("channels", projection["hardware_debug"])

    def test_hardware_debug_v2_modes_and_legacy_channel_upgrade(self) -> None:
        from aha_cli.domain.models import normalize_task_hardware_debug

        self.assertEqual(normalize_task_hardware_debug({"channels": []})["mode"], "off")
        upgraded = normalize_task_hardware_debug(
            {
                "channels": [
                    {"type": "uart", "settings": {"port": "/dev/ttyUSB0", "username": "root"}},
                    {"type": "telnet", "settings": {"host": "192.168.1.20", "username": "root", "password": "secret"}},
                ]
            }
        )
        self.assertEqual(upgraded["mode"], "both")
        self.assertEqual(upgraded["serial"]["device"], "/dev/ttyUSB0")
        self.assertEqual(upgraded["network"]["device_ip"], "192.168.1.20")
        self.assertEqual(upgraded["credentials"], {"username": "root", "password": "secret"})
        self.assertEqual(upgraded["permissions"], {"access": "read_only"})

        canonical = normalize_task_hardware_debug(
            {
                "mode": "network",
                "serial": {"device": "/dev/ttyUSB0", "baudrate": 9600},
                "network": {"device_ip": "192.168.1.21"},
                "credentials": {"username": "admin", "password": "pw"},
                "resources": [
                    {
                        "id": "Power Relay",
                        "type": "serial_relay",
                        "label": "Board power",
                        "device": "/dev/ttyUSB1",
                        "baudrate": 9600,
                        "channel": 1,
                    }
                ],
            }
        )
        self.assertEqual(canonical["mode"], "network")
        self.assertEqual(canonical["serial"]["baudrate"], 9600)
        self.assertEqual(canonical["network"]["device_ip"], "192.168.1.21")
        self.assertEqual(canonical["permissions"], {"access": "read_write"})
        self.assertEqual([group["id"] for group in canonical["groups"]], ["default", "power-relay"])
        self.assertEqual(canonical["groups"][1]["description"], "Board power")
        self.assertEqual(canonical["groups"][1]["serial"], {"device": "/dev/ttyUSB1", "baudrate": 9600})

        tools_only = normalize_task_hardware_debug(
            {
                "mode": "tools",
                "resources": [{"id": "power", "type": "relay", "port": "COM7"}],
                "permissions": {"access": "read_write"},
            }
        )
        self.assertEqual(tools_only["mode"], "serial")
        self.assertEqual(tools_only["groups"][0]["id"], "power")
        self.assertEqual(tools_only["groups"][0]["serial"]["device"], "COM7")

        explicit_read_only = normalize_task_hardware_debug(
            {
                "mode": "serial",
                "serial": {"device": "/dev/ttyUSB1", "baudrate": 115200},
                "permissions": {"access": "read_only"},
            }
        )
        self.assertEqual(explicit_read_only["permissions"], {"access": "read_only"})

        managed_group = normalize_task_hardware_debug(
            {
                "groups": [
                    {
                        "description": "Main console",
                        "mode": "both",
                        "serial": {"device": "COM6", "baudrate": 115200},
                        "network": {"device_ip": ""},
                    }
                ]
            }
        )
        self.assertEqual(managed_group["groups"][0]["id"], "hardware-1")
        self.assertEqual(managed_group["groups"][0]["mode"], "both")
        self.assertEqual(managed_group["groups"][0]["network"]["device_ip"], "")

    def test_old_plan_compatibility_fills_task_metadata_and_status_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-legacy"
            write_json(
                root / "runs" / run_id / "plan.json",
                {
                    "id": run_id,
                    "goal": "Legacy plan",
                    "mode": "research",
                    "created_at": "2026-05-30T00:00:00+00:00",
                    "updated_at": "2026-05-30T00:00:00+00:00",
                    "write_scopes": [],
                    "tasks": [
                        {
                            "id": "task-001",
                            "title": "Legacy task",
                            "description": "",
                            "workspace_id": "ws-legacy",
                            "workspace_path": "/legacy/repo",
                            "preferred_backend": "claude",
                            "preferred_model": "sonnet",
                            "delegation_policy": "auto",
                            "max_sub_agents": 1,
                            "status": "pending",
                            "prompt_file": "prompts/task-001.md",
                            "output_file": "results/task-001.md",
                            "log_file": "logs/task-001.log",
                            "inbox_file": "inbox/task-001.jsonl",
                            "created_at": "2026-05-30T00:00:00+00:00",
                            "started_at": None,
                            "finished_at": None,
                            "exit_code": None,
                            "agents": [],
                        }
                    ],
                },
            )

            enriched_task = require_plan(root, run_id)["tasks"][0]
            snapshot_task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(enriched_task["collaboration_mode"], "pair")
        self.assertEqual(enriched_task["workflow_template"], "auto")
        self.assertEqual(enriched_task["preferred_sub_backend"], "claude")
        self.assertEqual(enriched_task["preferred_sub_model"], "sonnet")
        self.assertEqual(enriched_task["supervision"]["mode"], "manual")
        self.assertFalse(enriched_task["context_management"]["auto_compact_enabled"])
        self.assertFalse(enriched_task["token_saving"]["enabled"])
        self.assertEqual(enriched_task["token_saving"]["provider"], "nav")
        self.assertEqual(enriched_task["token_saving"]["related_project_keys"], [])
        self.assertFalse(enriched_task["observe_proxy"]["enabled"])
        self.assertEqual(enriched_task["hardware_debug"]["mode"], "off")
        self.assertEqual(enriched_task["hardware_debug"]["permissions"], {"access": "read_only"})
        self.assertEqual(enriched_task["task_skills"]["enabled_paths"], [])
        self.assertEqual(snapshot_task["workspace_id"], "ws-legacy")
        self.assertEqual(snapshot_task["preferred_sub_backend"], "claude")
        self.assertEqual(snapshot_task["preferred_sub_model"], "sonnet")
        self.assertEqual(snapshot_task["collaboration_mode"], "pair")
        self.assertEqual(snapshot_task["max_sub_agents"], 1)
        self.assertFalse(snapshot_task["observe_proxy"]["enabled"])

    def test_supervision_host_uses_dedicated_model_and_proxy_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = create_plan(
                root,
                "Host controls",
                0,
                "implementation",
                [],
                [],
                backend="codex",
                model="gpt-5.5",
                proxy_enabled=True,
                create_default_tasks=False,
            )
            task = create_task_and_dispatch(
                root,
                plan["id"],
                "Supervised task",
                backend="codex",
                model="gpt-5.5",
                proxy_enabled=True,
                supervision={
                    "mode": "assisted",
                    "host_backend": "claude",
                    "host_model": "claude-sonnet-4-5",
                    "host_proxy_enabled": False,
                    "real_agent_enabled": True,
                },
                dispatch=False,
            )
            default_host_task = create_task_and_dispatch(
                root,
                plan["id"],
                "Default supervised task",
                backend="codex",
                model="gpt-5.5",
                proxy_enabled=True,
                supervision={
                    "mode": "assisted",
                    "host_backend": "claude",
                    "real_agent_enabled": True,
                },
                dispatch=False,
            )
            snapshot_task = status_snapshot(root, plan["id"])["tasks"][0]

        main = next(agent for agent in task["agents"] if agent["id"] == "main")
        host = next(agent for agent in task["agents"] if agent["role"] == "host")
        default_host = next(agent for agent in default_host_task["agents"] if agent["role"] == "host")
        self.assertEqual(main["model"], "gpt-5.5")
        self.assertTrue(main["proxy_enabled"])
        self.assertEqual(host["backend"], "claude")
        self.assertEqual(host["model"], "claude-sonnet-4-5")
        self.assertFalse(host["proxy_enabled"])
        self.assertIsNone(default_host["model"])
        self.assertFalse(default_host["proxy_enabled"])
        self.assertEqual(snapshot_task["supervision"]["host_model"], "claude-sonnet-4-5")
        self.assertFalse(snapshot_task["supervision"]["host_proxy_enabled"])


if __name__ == "__main__":
    unittest.main()
