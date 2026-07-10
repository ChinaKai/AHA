from __future__ import annotations

from pathlib import Path

from aha_cli.services.backend_runtime import start_backend
from aha_cli.store.io import read_json
from aha_cli.store.paths import session_path


def compact_reset_backend_session(*args: object, **kwargs: object) -> dict:
    from aha_cli.services.session_compact import compact_reset_backend_session as compact_reset

    return compact_reset(*args, **kwargs)


def backend_session_has_active_id(root: Path, run_id: str, task_id: str, agent_id: str) -> bool:
    path = session_path(root, run_id, task_id, agent_id)
    if not path.exists():
        return False
    try:
        session = read_json(path)
    except (OSError, ValueError):
        return False
    return bool(session.get("backend_session_id"))


def runtime_context_percent(pressure: dict) -> float | None:
    pressure_source = str(pressure.get("pressure_source") or "")
    if not pressure.get("pressure_is_runtime") and not pressure_source.startswith("runtime."):
        return None
    try:
        return float(pressure.get("runtime_percent", pressure.get("percent")))
    except (TypeError, ValueError):
        return None


def auto_compact_agent_context_before_backend_start(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_state: dict | None = None,
) -> dict | None:
    # Automatic compact/reset is no longer part of task startup. The hook stays
    # for compatibility with callers while token saving is handled by providers
    # such as Headroom without breaking sticky backend sessions.
    return None


def auto_compact_agent_context_after_turn(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_state: dict | None = None,
) -> dict | None:
    # Turn-end implicit reset hurts backend-session continuity; keep the public
    # hook for callers/tests, but do not clear backend_session_id automatically.
    return None


def start_backend_after_auto_compact(
    root: Path,
    run_id: str,
    target: str = "main",
    *,
    backend: str = "codex",
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    model: str | None = None,
    reasoning_effort: str | None = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    interval: float = 1.0,
    from_start: bool = False,
    no_json: bool = False,
    extra_args: list[str] | None = None,
    prompt_prefix: str | None = None,
    task_id: str | None = None,
) -> dict:
    auto_compact_agent_context_before_backend_start(root, run_id, task_id, target)
    start_kwargs = {
        "backend": backend,
        "codex_bin": codex_bin,
        "claude_bin": claude_bin,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "sandbox": sandbox,
        "approval": approval,
        "interval": interval,
        "from_start": from_start,
        "no_json": no_json,
        "extra_args": extra_args,
        "task_id": task_id,
    }
    if prompt_prefix is not None:
        start_kwargs["prompt_prefix"] = prompt_prefix
    return start_backend(
        root,
        run_id,
        target,
        **start_kwargs,
    )


__all__ = [
    "auto_compact_agent_context_after_turn",
    "auto_compact_agent_context_before_backend_start",
    "backend_session_has_active_id",
    "runtime_context_percent",
    "start_backend_after_auto_compact",
]
