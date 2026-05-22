from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.store.filesystem import read_json, run_dir, status_snapshot


def run_cli(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch("sys.stdout", out):
        code = main(list(args))
    return code, out.getvalue()


class RunArchiveTests(unittest.TestCase):
    def test_run_export_import_redacts_proxy_and_marks_sessions_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli(
                    "plan",
                    "Portable run",
                    "--agents",
                    "1",
                    "--enable-proxy",
                    "--http-proxy",
                    "http://user:secret@example.test:8080",
                    "--workspace-path",
                    str(root),
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-secret"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                runtime_file = run_dir(root, run_id) / "runtime" / "local.cache"
                runtime_file.parent.mkdir(parents=True)
                runtime_file.write_text("local-only", encoding="utf-8")

                archive = root / "portable.tar.gz"
                code, output = run_cli("run", "export", run_id, "-o", str(archive))
                self.assertEqual(code, 0)
                self.assertIn(f"Exported run {run_id}", output)

                with tarfile.open(archive, "r:gz") as exported:
                    names = set(exported.getnames())
                    self.assertIn("aha-run-manifest.json", names)
                    self.assertIn("run/plan.json", names)
                    self.assertNotIn("run/runtime/local.cache", names)
                    plan = json.load(exported.extractfile("run/plan.json"))
                    exported_session = json.load(exported.extractfile("run/tasks/task-001/sessions/main.json"))
                self.assertEqual(plan["tasks"][0]["preferred_http_proxy"], "<redacted>")
                self.assertNotIn("secret", json.dumps(plan))
                self.assertIsNone(exported_session["backend_session_id"])
                self.assertEqual(exported_session["imported_backend_session_id"], "backend-secret")

                import_home = root / "imported.aha"
                code, import_output = run_cli("--home", str(import_home), "run", "import", str(archive))
                self.assertEqual(code, 0)
                imported_line = [line for line in import_output.splitlines() if line.startswith("Imported run ")][0]
                imported_run_id = imported_line.split(" as ", 1)[1]
                self.assertNotEqual(imported_run_id, run_id)
                imported_plan = read_json(run_dir(import_home, imported_run_id) / "plan.json")
                imported_session = read_json(run_dir(import_home, imported_run_id) / "tasks" / "task-001" / "sessions" / "main.json")
                self.assertEqual(imported_plan["id"], imported_run_id)
                self.assertEqual(imported_session["status"], "imported")
                self.assertIsNone(imported_session["backend_session_id"])
                self.assertEqual(imported_session["imported_from_run_id"], run_id)
                self.assertFalse((run_dir(import_home, imported_run_id) / "runtime").exists())
                snapshot = status_snapshot(import_home, imported_run_id)
                self.assertEqual(snapshot["tasks"][0]["agents"][0]["session_status"], "imported")
