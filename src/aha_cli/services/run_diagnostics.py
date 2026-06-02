from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
import subprocess
import time

from aha_cli.constants import PLAN_FILE, RUNS_DIR
from aha_cli.services.backend_runtime import backend_status
from aha_cli.services.run_cleanup import (
    DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    DEFAULT_STALE_SECONDS,
    classify_run_cleanup_candidate,
    run_has_active_heartbeat,
)
from aha_cli.store.io import read_json
from aha_cli.store.paths import aha_home_path
from aha_cli.store.runs import list_run_summaries

CommandRunner = Callable[[list[str]], str]
BackendStatusProvider = Callable[[Path, str, str, str | None], dict]

_COMMON_AHA_PORTS = {"8766", "8788"}
_LISTENER_PORT_RE = re.compile(r":(?P<port>\d+)(?:\s|$)")
_LISTENER_PROCESS_RE = re.compile(r'"(?P<name>[^"]+)",pid=(?P<pid>\d+)')


def _default_command_runner(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout or ""


def _run_paths(aha_home: Path) -> list[Path]:
    runs_dir = aha_home_path(aha_home) / RUNS_DIR
    if not runs_dir.is_dir():
        return []
    try:
        return sorted(path for path in runs_dir.iterdir() if path.is_dir())
    except OSError:
        return []


def _read_plan_goal(run_path: Path) -> str:
    plan = _read_plan(run_path)
    return str(plan.get("goal") or "") if plan else ""


def _read_plan(run_path: Path) -> dict | None:
    plan_path = run_path / PLAN_FILE
    if not plan_path.exists():
        return None
    try:
        return read_json(plan_path)
    except (OSError, ValueError):
        return None


def _iter_running_agent_candidates(plan: dict) -> list[dict]:
    candidates: list[dict] = []
    for task in plan.get("tasks") or []:
        task_id = str(task.get("id") or "")
        task_status = str(task.get("status") or "")
        if not task_id or task_status != "running":
            continue
        for agent in task.get("agents") or []:
            agent_id = str(agent.get("id") or "")
            agent_status = str(agent.get("status") or "")
            if not agent_id or agent_status != "running":
                continue
            candidates.append(
                {
                    "task_id": task_id,
                    "task_title": str(task.get("title") or ""),
                    "task_status": task_status,
                    "agent_id": agent_id,
                    "agent_role": str(agent.get("role") or ""),
                    "agent_status": agent_status,
                }
            )
    return candidates


def _stale_running_agents(
    aha_home: Path,
    run_path: Path,
    *,
    backend_status_provider: BackendStatusProvider,
) -> list[dict]:
    plan = _read_plan(run_path)
    if not plan:
        return []
    run_id = run_path.name
    stale_agents: list[dict] = []
    for candidate in _iter_running_agent_candidates(plan):
        task_id = candidate["task_id"]
        agent_id = candidate["agent_id"]
        try:
            state = backend_status_provider(aha_home, run_id, agent_id, task_id)
        except (OSError, SystemExit, ValueError):
            continue
        backend_process_status = str(state.get("status") or "stopped").lower()
        if backend_process_status != "stopped":
            continue
        stale_agents.append(
            {
                "run_id": run_id,
                **candidate,
                "backend_status": backend_process_status,
                "backend": str(state.get("backend") or ""),
                "last_pid": state.get("last_pid"),
                "stopped_at": state.get("stopped_at") or "",
                "log_path": str(state.get("log_path") or ""),
                "reason": "running_agent_stopped_backend",
            }
        )
    return stale_agents


def _cleanup_diagnostic(cleanup: dict) -> dict:
    dry_run_action = "would_delete" if cleanup.get("action") == "delete" else str(cleanup.get("action") or "")
    return {**cleanup, "dry_run_action": dry_run_action}


def _interesting_listener(line: str, port: str) -> bool:
    lowered = line.lower()
    return "aha" in lowered or "aha_cli" in lowered or port in _COMMON_AHA_PORTS


def _probe_listeners(command_runner: CommandRunner) -> list[dict]:
    output = command_runner(["ss", "-ltnp"])
    listeners: list[dict] = []
    for line in output.splitlines():
        port_match = _LISTENER_PORT_RE.search(line)
        if not port_match:
            continue
        port = port_match.group("port")
        if not _interesting_listener(line, port):
            continue
        process_match = _LISTENER_PROCESS_RE.search(line)
        listeners.append(
            {
                "port": port,
                "line": line.strip(),
                "process": process_match.group("name") if process_match else "",
                "pid": process_match.group("pid") if process_match else "",
            }
        )
    return listeners


def _probe_processes(command_runner: CommandRunner) -> list[dict]:
    output = command_runner(["ps", "-eo", "pid=,ppid=,stat=,args="])
    processes: list[dict] = []
    for line in output.splitlines():
        if "aha" not in line.lower() and "aha_cli" not in line.lower():
            continue
        parts = line.strip().split(maxsplit=3)
        if len(parts) < 4:
            continue
        processes.append({"pid": parts[0], "ppid": parts[1], "stat": parts[2], "command": parts[3]})
    return processes


def _probe_service_units(command_runner: CommandRunner) -> list[dict]:
    output = command_runner(["systemctl", "--user", "list-units", "--type=service", "--all", "--no-pager", "--plain"])
    units: list[dict] = []
    for line in output.splitlines():
        if "aha" not in line.lower():
            continue
        parts = line.strip().split(maxsplit=4)
        if len(parts) < 4:
            continue
        units.append(
            {
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return units


def diagnose_runs(
    aha_home: Path,
    *,
    current_run_id: str | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    now: float | None = None,
    command_runner: CommandRunner | None = None,
    backend_status_provider: BackendStatusProvider | None = None,
) -> dict:
    now = time.time() if now is None else now
    aha_home = aha_home_path(aha_home)
    command_runner = command_runner or _default_command_runner
    backend_status_provider = backend_status_provider or backend_status
    visible_runs = list_run_summaries(aha_home)
    visible_by_id = {str(run.get("id") or ""): run for run in visible_runs}
    runs: list[dict] = []
    active_heartbeat_runs: list[str] = []
    cleanup_candidates: list[dict] = []
    stale_running_agents: list[dict] = []

    for run_path in _run_paths(aha_home):
        run_id = run_path.name
        active_heartbeat = run_has_active_heartbeat(
            run_path,
            now=now,
            active_heartbeat_seconds=active_heartbeat_seconds,
        )
        if active_heartbeat:
            active_heartbeat_runs.append(run_id)
        cleanup = _cleanup_diagnostic(
            classify_run_cleanup_candidate(
                run_path,
                current_run_id=current_run_id,
                now=now,
                stale_seconds=stale_seconds,
                active_heartbeat_seconds=active_heartbeat_seconds,
            )
        )
        cleanup_candidates.append(cleanup)
        stale_running_agents.extend(
            _stale_running_agents(
                aha_home,
                run_path,
                backend_status_provider=backend_status_provider,
            )
        )
        runs.append(
            {
                "run_id": run_id,
                "path": str(run_path),
                "has_plan": (run_path / PLAN_FILE).exists(),
                "visible": run_id in visible_by_id,
                "goal": str(visible_by_id.get(run_id, {}).get("goal") or _read_plan_goal(run_path)),
                "lifecycle_status": str(visible_by_id.get(run_id, {}).get("lifecycle_status") or ""),
                "active_heartbeat": active_heartbeat,
                "cleanup": cleanup,
            }
        )

    services = {
        "listeners": _probe_listeners(command_runner),
        "processes": _probe_processes(command_runner),
        "service_units": _probe_service_units(command_runner),
    }
    return {
        "aha_home": str(aha_home),
        "current_run_id": current_run_id or "",
        "visible_runs": visible_runs,
        "active_heartbeat_runs": active_heartbeat_runs,
        "stale_running_agents": stale_running_agents,
        "runs": runs,
        "cleanup": {"candidates": cleanup_candidates},
        "services": services,
    }


def format_run_diagnostics(result: dict) -> str:
    lines = ["AHA runs diagnose"]
    lines.append(f"aha_home: {result.get('aha_home') or '-'}")
    lines.append(f"current_run: {result.get('current_run_id') or '-'}")
    visible_runs = result.get("visible_runs") or []
    lines.append(f"visible_runs: {len(visible_runs)}")
    for run in result.get("runs") or []:
        cleanup = run.get("cleanup") or {}
        heartbeat = " active-heartbeat" if run.get("active_heartbeat") else ""
        lifecycle = f" lifecycle={run['lifecycle_status']}" if run.get("lifecycle_status") else ""
        lines.append(
            f"- {run['run_id']}: {cleanup.get('dry_run_action') or cleanup.get('action')} "
            f"({cleanup.get('reason')}){heartbeat}{lifecycle}"
        )
    if not result.get("runs"):
        lines.append("- no runs")

    stale_agents = result.get("stale_running_agents") or []
    lines.append(f"stale_running_agents: {len(stale_agents)}")
    for agent in stale_agents:
        backend = f" backend={agent['backend']}" if agent.get("backend") else ""
        last_pid = f" last_pid={agent['last_pid']}" if agent.get("last_pid") else ""
        lines.append(
            f"- {agent['run_id']} {agent['task_id']}/{agent['agent_id']}: "
            f"{agent['backend_status']} ({agent['reason']}){backend}{last_pid}"
        )

    services = result.get("services") or {}
    listeners = services.get("listeners") or []
    lines.append(f"listeners: {len(listeners)}")
    for listener in listeners:
        process = f" {listener['process']}[{listener['pid']}]" if listener.get("process") else ""
        lines.append(f"- :{listener['port']}{process}")

    processes = services.get("processes") or []
    lines.append(f"processes: {len(processes)}")
    for process in processes:
        lines.append(f"- {process['pid']} {process['stat']} {process['command']}")

    service_units = services.get("service_units") or []
    lines.append(f"service_units: {len(service_units)}")
    for unit in service_units:
        lines.append(f"- {unit['unit']} {unit['active']}/{unit['sub']}")
    return "\n".join(lines) + "\n"
