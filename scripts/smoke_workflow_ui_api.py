#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from aha_cli.cli import initialize_aha_home  # noqa: E402
from aha_cli.store.filesystem import create_plan  # noqa: E402
from aha_cli.web.server import handle_ui_client  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def fetch_ui_response(
    root: Path,
    run_id: str,
    target: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 2.0,
) -> bytes:
    server = await asyncio.start_server(
        lambda reader, writer: handle_ui_client(root, run_id, reader, writer),
        "127.0.0.1",
        0,
    )
    host, port = server.sockets[0].getsockname()
    try:
        reader, writer = await asyncio.open_connection(host, port)
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        header_lines = [
            f"{method} {target} HTTP/1.1",
            "Host: workflow-smoke",
            "Connection: close",
            f"Content-Length: {len(body)}",
        ]
        if payload is not None:
            header_lines.append("Content-Type: application/json")
        writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return response
    finally:
        server.close()
        await server.wait_closed()


def response_body(response: bytes) -> dict:
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    require(" 200 " in status_line or " 201 " in status_line, f"unexpected HTTP status: {status_line}")
    return json.loads(response.split(b"\r\n\r\n", 1)[1].decode("utf-8"))


async def run_smoke() -> dict:
    with tempfile.TemporaryDirectory(prefix="aha-workflow-smoke-") as tmp:
        root = Path(tmp) / ".aha"
        with contextlib.redirect_stdout(io.StringIO()):
            initialize_aha_home(
                root,
                SimpleNamespace(force=True, backend="codex", runner_command=None, parallel=10),
            )
        plan = create_plan(
            root=root,
            goal="Workflow smoke",
            agents=1,
            mode="research",
            task_titles=["Initial smoke task"],
            write_scopes=[],
            backend="codex",
            workspace_path=str(REPO_ROOT),
            collaboration_mode="auto",
            workflow_template="embedded-driver",
        )
        run_id = plan["id"]

        bootstrap_response = await fetch_ui_response(root, run_id, f"/api/bootstrap?run_id={run_id}")
        bootstrap = response_body(bootstrap_response)
        bootstrap_templates = bootstrap.get("workflow_templates") or []
        bootstrap_template_ids = {str(item.get("id") or "") for item in bootstrap_templates}
        bootstrap_runs = {str(item.get("id") or ""): item for item in bootstrap.get("runs") or []}
        require("fault-debug" in bootstrap_template_ids, "bootstrap workflow template registry missing fault-debug")
        require(
            any(str(item.get("description") or "") for item in bootstrap_templates),
            "bootstrap workflow template descriptions missing",
        )
        require(
            bootstrap_runs.get(run_id, {}).get("lifecycle_status") == "active",
            "bootstrap run lifecycle projection missing active status",
        )

        runs_response = await fetch_ui_response(root, run_id, f"/api/runs?run_id={run_id}")
        runs_payload = response_body(runs_response)
        api_runs = {str(item.get("id") or ""): item for item in runs_payload.get("runs") or []}
        require(api_runs.get(run_id, {}).get("lifecycle_status") == "active", "runs API lifecycle projection missing active status")

        html_response = await fetch_ui_response(root, run_id, "/")
        html = html_response.split(b"\r\n\r\n", 1)[1].decode("utf-8")
        require("<span>Execution</span>" in html, "Execution label missing from task form")
        require("<span>Workflow</span>" in html, "Workflow label missing from task form")
        require('id="workflow-template"' in html, "workflow template select missing")
        require('id="run-lifecycle"' in html, "run lifecycle badge missing from current run area")
        require("run-lifecycle-active" in html, "run lifecycle active badge class missing")
        for template in (
            "auto",
            "bugfix",
            "feature",
            "review",
            "embedded-driver",
            "fault-debug",
            "hil-regression",
            "release",
        ):
            require(f'value="{template}"' in html, f"workflow option missing: {template}")

        match = re.search(r'<select id="collaboration-mode">(.*?)</select>', html, re.S)
        require(match is not None, "collaboration/execution select missing")
        collaboration_select = match.group(1)
        require('value="auto"' in collaboration_select, "auto execution option missing")
        for legacy_mode in ("solo", "pair", "team"):
            require(
                f'value="{legacy_mode}"' not in collaboration_select,
                f"legacy mode still emphasized in main UI select: {legacy_mode}",
            )

        created_response = await fetch_ui_response(
            root,
            run_id,
            f"/api/tasks?run_id={run_id}",
            method="POST",
            payload={
                "title": "Workflow template smoke",
                "description": "Check workflow template persistence",
                "collaboration_mode": "auto",
                "workflow_template": "fault-debug",
                "max_sub_agents": 3,
                "dispatch": False,
            },
        )
        created = response_body(created_response)["task"]
        require(created["collaboration_mode"] == "auto", "created task collaboration_mode mismatch")
        require(created["workflow_template"] == "fault-debug", "created task workflow_template mismatch")
        require(created["max_sub_agents"] == 3, "created task max_sub_agents mismatch")

        legacy_compat: dict[str, int] = {}
        for mode, expected_limit in {"solo": 0, "pair": 1, "team": 2}.items():
            legacy_response = await fetch_ui_response(
                root,
                run_id,
                f"/api/tasks?run_id={run_id}",
                method="POST",
                payload={"title": f"Legacy {mode} smoke", "collaboration_mode": mode, "dispatch": False},
            )
            task = response_body(legacy_response)["task"]
            require(task["collaboration_mode"] == mode, f"legacy mode mismatch: {mode}")
            require(task["max_sub_agents"] == expected_limit, f"legacy max_sub_agents mismatch: {mode}")
            require(task["workflow_template"] == "auto", f"legacy workflow default mismatch: {mode}")
            legacy_compat[mode] = task["max_sub_agents"]

        status_response = await fetch_ui_response(root, run_id, f"/api/status?run_id={run_id}")
        status = response_body(status_response)
        status_tasks = {task["id"]: task for task in status["tasks"]}
        created_status = status_tasks[created["id"]]
        require(created_status["collaboration_mode"] == "auto", "status collaboration_mode mismatch")
        require(created_status["workflow_template"] == "fault-debug", "status workflow_template mismatch")
        require(created_status["max_sub_agents"] == 3, "status max_sub_agents mismatch")
        require(created_status["preferred_sub_backend"] == "codex", "status preferred_sub_backend missing")
        for mode, expected_limit in legacy_compat.items():
            legacy_status = next(task for task in status["tasks"] if task["title"] == f"Legacy {mode} smoke")
            require(legacy_status["collaboration_mode"] == mode, f"status legacy mode mismatch: {mode}")
            require(legacy_status["max_sub_agents"] == expected_limit, f"status legacy limit mismatch: {mode}")
            require(legacy_status["workflow_template"] == "auto", f"status legacy workflow mismatch: {mode}")

        return {
            "run_id": run_id,
            "ui": "Execution auto shown; Workflow templates shown; legacy modes hidden from main select",
            "api_workflow_template": created["workflow_template"],
            "status_workflow_template": created_status["workflow_template"],
            "run_lifecycle": api_runs[run_id]["lifecycle_status"],
            "bootstrap_workflow_templates": sorted(bootstrap_template_ids),
            "legacy_compat": legacy_compat,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test AHA workflow-template UI/API behavior.")
    parser.add_argument("--json", action="store_true", help="Print compact JSON only.")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_smoke())
    except Exception as exc:  # noqa: BLE001
        print(f"workflow smoke failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("Workflow UI/API smoke passed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
