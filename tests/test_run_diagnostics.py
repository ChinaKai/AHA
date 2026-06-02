from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from aha_cli.services.run_diagnostics import diagnose_runs, format_run_diagnostics
from aha_cli.store.io import read_json, write_json


def write_plan(run_path: Path, run_id: str, *, temporary: bool = False, tasks: list[dict] | None = None) -> None:
    data = {
        "id": run_id,
        "goal": f"Run {run_id}",
        "mode": "research",
        "created_at": "2026-05-31T00:00:00+00:00",
        "updated_at": "2026-05-31T00:00:00+00:00",
        "write_scopes": [],
        "tasks": tasks or [],
    }
    if temporary:
        data["temporary"] = True
    write_json(run_path / "plan.json", data)


def touch_tree(path: Path, mtime: float) -> None:
    for item in [path, *path.rglob("*")]:
        os.utime(item, (mtime, mtime))


class FakeCommandRunner:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(tuple(argv))
        return self.outputs.get(argv[0], "")


class RunDiagnosticsTests(unittest.TestCase):
    def test_diagnose_reports_runs_cleanup_reasons_and_service_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            current_run = aha_home / "runs" / "current-run"
            active_run = aha_home / "runs" / "active-run"
            user_run = aha_home / "runs" / "user-run"
            temp_run = aha_home / "runs" / "temp-run"
            orphan_run = aha_home / "runs" / "orphan-run"
            for run_path, run_id, temporary in (
                (current_run, "current-run", True),
                (active_run, "active-run", True),
                (user_run, "user-run", False),
                (temp_run, "temp-run", True),
            ):
                write_plan(run_path, run_id, temporary=temporary)
                if temporary:
                    (run_path / ".aha-temp-run").write_text("", encoding="utf-8")
                touch_tree(run_path, 1000)
            orphan_run.mkdir(parents=True)
            touch_tree(orphan_run, 1000)
            heartbeat = active_run / "logs" / "realtime-debug.log"
            heartbeat.parent.mkdir(parents=True)
            heartbeat.write_text('{"type":"heartbeat_sent"}\n', encoding="utf-8")
            os.utime(heartbeat, (1995, 1995))

            runner = FakeCommandRunner(
                {
                    "ss": "\n".join(
                        [
                            "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process",
                            'LISTEN 0 128 127.0.0.1:8788 0.0.0.0:* users:(("python3",pid=123,fd=7))',
                            'LISTEN 0 128 127.0.0.1:9999 0.0.0.0:* users:(("postgres",pid=999,fd=7))',
                        ]
                    ),
                    "ps": "\n".join(
                        [
                            "123 1 Sl python3 -m aha_cli ui current-run --port 8788",
                            "999 1 S postgres",
                        ]
                    ),
                    "systemctl": "aha.service loaded active running AHA UI\n",
                }
            )

            result = diagnose_runs(
                aha_home,
                current_run_id="current-run",
                stale_seconds=60,
                active_heartbeat_seconds=30,
                now=2000,
                command_runner=runner,
            )
            runs = {run["run_id"]: run for run in result["runs"]}

            self.assertEqual(result["current_run_id"], "current-run")
            self.assertEqual(runs["current-run"]["cleanup"]["reason"], "current_run")
            self.assertEqual(runs["active-run"]["cleanup"]["reason"], "active_heartbeat")
            self.assertTrue(runs["active-run"]["active_heartbeat"])
            self.assertEqual(runs["user-run"]["cleanup"]["reason"], "non_temporary_run")
            self.assertEqual(runs["temp-run"]["cleanup"]["dry_run_action"], "would_delete")
            self.assertEqual(runs["orphan-run"]["cleanup"]["reason"], "stale_orphan_run")
            self.assertTrue(temp_run.exists())
            self.assertEqual(result["active_heartbeat_runs"], ["active-run"])
            self.assertIn("user-run", {run["id"] for run in result["visible_runs"]})
            self.assertEqual(result["services"]["listeners"][0]["port"], "8788")
            self.assertEqual(result["services"]["processes"][0]["pid"], "123")
            self.assertEqual(result["services"]["service_units"][0]["unit"], "aha.service")
            self.assertIn(("ss", "-ltnp"), runner.calls)
            self.assertIn(("ps", "-eo", "pid=,ppid=,stat=,args="), runner.calls)
            self.assertIn(
                ("systemctl", "--user", "list-units", "--type=service", "--all", "--no-pager", "--plain"),
                runner.calls,
            )

    def test_format_run_diagnostics_is_human_readable(self) -> None:
        result = {
            "aha_home": "/tmp/.aha",
            "current_run_id": "current-run",
            "visible_runs": [{"id": "current-run"}],
            "runs": [
                {
                    "run_id": "current-run",
                    "lifecycle_status": "active",
                    "active_heartbeat": True,
                    "cleanup": {"dry_run_action": "protect", "reason": "current_run"},
                }
            ],
            "services": {
                "listeners": [{"port": "8788", "process": "python3", "pid": "123"}],
                "processes": [{"pid": "123", "stat": "Sl", "command": "python3 -m aha_cli ui"}],
                "service_units": [{"unit": "aha.service", "active": "active", "sub": "running"}],
            },
        }

        output = format_run_diagnostics(result)

        self.assertIn("current_run: current-run", output)
        self.assertIn("current-run: protect (current_run)", output)
        self.assertIn(":8788 python3[123]", output)
        self.assertIn("aha.service active/running", output)

    def test_diagnose_reports_stale_running_agents_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            run_path = aha_home / "runs" / "run-with-stale-agent"
            tasks = [
                {
                    "id": "task-001",
                    "title": "Investigate stale backend",
                    "status": "running",
                    "agents": [
                        {"id": "main", "role": "task-main", "status": "running"},
                        {"id": "sub-001", "role": "task-sub", "status": "completed"},
                    ],
                },
                {
                    "id": "task-002",
                    "title": "Healthy task",
                    "status": "running",
                    "agents": [{"id": "main", "role": "task-main", "status": "running"}],
                },
                {
                    "id": "task-003",
                    "title": "Finished task",
                    "status": "completed",
                    "agents": [{"id": "main", "role": "task-main", "status": "running"}],
                },
            ]
            write_plan(run_path, "run-with-stale-agent", tasks=tasks)
            before_plan = read_json(run_path / "plan.json")

            def fake_backend_status(_root: Path, _run_id: str, target: str, task_id: str | None) -> dict:
                if task_id == "task-001" and target == "main":
                    return {
                        "status": "stopped",
                        "backend": "codex",
                        "last_pid": 12345,
                        "stopped_at": "2026-05-31T01:00:00+00:00",
                        "log_path": "/tmp/aha.log",
                    }
                return {"status": "running", "backend": "codex", "pid": 23456}

            result = diagnose_runs(
                aha_home,
                now=2000,
                command_runner=FakeCommandRunner({}),
                backend_status_provider=fake_backend_status,
            )

            self.assertEqual(
                result["stale_running_agents"],
                [
                    {
                        "run_id": "run-with-stale-agent",
                        "task_id": "task-001",
                        "task_title": "Investigate stale backend",
                        "task_status": "running",
                        "agent_id": "main",
                        "agent_role": "task-main",
                        "agent_status": "running",
                        "backend_status": "stopped",
                        "backend": "codex",
                        "last_pid": 12345,
                        "stopped_at": "2026-05-31T01:00:00+00:00",
                        "log_path": "/tmp/aha.log",
                        "reason": "running_agent_stopped_backend",
                    }
                ],
            )
            self.assertEqual(read_json(run_path / "plan.json"), before_plan)
            event_log = run_path / "events.jsonl"
            self.assertFalse(event_log.exists())

            output = format_run_diagnostics(result)
            self.assertIn("stale_running_agents: 1", output)
            self.assertIn("run-with-stale-agent task-001/main: stopped", output)
            self.assertIn("running_agent_stopped_backend", output)


if __name__ == "__main__":
    unittest.main()
