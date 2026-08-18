from __future__ import annotations

from contextlib import contextmanager
from aha_cli import locking, platform, process_control
import hashlib
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
import subprocess
import sys
import time
import zipfile

from aha_cli.backends.claude import (
    apply_claude_environment,
    claude_cli_model,
    claude_config_env,
    claude_config_for_model,
    claude_context_window,
    claude_resolved_model,
)
from aha_cli.backends.codex import apply_codex_environment, codex_cli_model, codex_config_for_model, codex_resolved_model
from aha_cli.backends.registry import CODEX_DEFAULT_MODEL, normalize_model_selector, normalize_reasoning_effort, resolve_model
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_paths import add_user_backend_paths
from aha_cli.services.commit_policy import generated_by_for_backend_model
from aha_cli.services.context_pressure import context_pressure
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.proxy import PROXY_ENV_KEYS, apply_proxy_environment, proxy_env_for_agent
from aha_cli.store.filesystem import (
    append_event,
    event_path,
    iter_jsonl_reverse,
    load_config,
    read_json,
    require_plan,
    run_dir,
    session_path,
    task_snapshot,
    write_json,
)

BACKEND_ACTIVITY_SCAN_LIMIT = 5000
CODEX_CONTEXT_WINDOW_SCAN_LIMIT = 1000
CLAUDE_CONTEXT_WINDOW_SCAN_LIMIT = 5000
CODEX_CONTEXT_DROP_MIN_PREVIOUS_PERCENT = 70.0
CODEX_CONTEXT_DROP_MAX_CURRENT_PERCENT = 60.0
CODEX_CONTEXT_DROP_MIN_DELTA_PERCENT = 20.0
CODEX_CONTEXT_DROP_MIN_DELTA_TOKENS = 30_000
PROCESS_AGENT_BACKENDS = {"codex", "claude"}
_WINDOWS = os.name == "nt"

# Platform-layer public interface (L4 分层固化): the backend process lifecycle
# is a stable platform capability. Business modules consume these entrypoints;
# underscore-prefixed helpers are internal and may change without notice.
__all__ = [
    "PROCESS_AGENT_BACKENDS",
    "safe_target",
    "backend_key",
    "backend_state_path",
    "backend_log_path",
    "backend_lock_path",
    "locked_backend",
    "pid_is_running",
    "detect_runtime_context_compaction",
    "backend_status",
    "ensure_backend_wsl_state",
    "mark_backend_stopped",
    "stop_all_backends",
    "stop_task_backends",
    "start_backend",
    "stop_backend",
]


def safe_target(target: str) -> str:
    return (target or "main").replace("/", "_")


def backend_key(target: str = "main", task_id: str | None = None) -> str:
    target_name = safe_target(target)
    if task_id:
        return f"{safe_target(task_id)}-{target_name}"
    return target_name


def backend_state_path(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> Path:
    return run_dir(root, run_id) / "runtime" / f"backend-{backend_key(target, task_id)}.json"


def backend_log_path(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> Path:
    return run_dir(root, run_id) / "logs" / f"backend-{backend_key(target, task_id)}.log"


def backend_lock_path(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> Path:
    return run_dir(root, run_id) / "runtime" / f"backend-{backend_key(target, task_id)}.lock"


@contextmanager
def locked_backend(root: Path, run_id: str, target: str = "main", task_id: str | None = None):
    lock_path = backend_lock_path(root, run_id, target, task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        locking.acquire(lock_file.fileno())
        try:
            yield
        finally:
            locking.release(lock_file.fileno())


def pid_is_running(pid: int | None) -> bool:
    return process_control.process_exists(pid)


def _read_state(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    path = backend_state_path(root, run_id, target, task_id)
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def _write_state(root: Path, run_id: str, target: str, state: dict, task_id: str | None = None) -> dict:
    write_json(backend_state_path(root, run_id, target, task_id), state)
    return state


def _current_wsl_runtime_context(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[str | None, str | None]:
    env = os.environ if environ is None else environ
    distro = str(env.get("AHA_WSL_DISTRO") or env.get("WSL_DISTRO_NAME") or "").strip() or None
    if not distro:
        return None, None
    native_home = str(home if home is not None else Path.home()).strip()
    return distro, native_home if native_home.startswith("/") else None


def ensure_backend_wsl_state(
    root: Path,
    run_id: str,
    target: str = "main",
    *,
    task_id: str | None = None,
) -> dict:
    distro, native_home = _current_wsl_runtime_context()
    if not distro:
        return _read_state(root, run_id, target, task_id)
    with locked_backend(root, run_id, target, task_id):
        state = _read_state(root, run_id, target, task_id)
        if not state:
            return state
        updated = dict(state)
        updated["wsl_distro"] = distro
        if native_home:
            updated["wsl_native_home"] = native_home
        if updated != state:
            _write_state(root, run_id, target, updated, task_id)
        return updated


def _event_time(event: dict) -> str:
    return str(event.get("ts", "") or "")


def _backend_activity(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    return _backend_event_runtime(root, run_id, target, task_id)["activity"]


def _event_matches_metric_target(data: dict, target: str, task_id: str | None) -> bool:
    if data.get("target") != target:
        return False
    if task_id and data.get("task_id") != task_id:
        return False
    if task_id is None and data.get("task_id"):
        return False
    return True


def _backend_event_runtime(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    latest_started: dict | None = None
    latest_finished: dict | None = None
    latest_reply: dict | None = None
    latest_error: dict | None = None
    latest_usage: dict | None = None
    latest_prompt_metrics: dict | None = None
    scanned = 0
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        scanned += 1
        event_type = event.get("type")
        raw_data = event.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}

        if scanned <= BACKEND_ACTIVITY_SCAN_LIMIT and (not task_id or data.get("task_id") == task_id):
            if latest_started is None and event_type == "agent_started" and data.get("target") == target:
                latest_started = event
            elif latest_finished is None and event_type == "agent_finished" and data.get("target") == target:
                latest_finished = event
            elif latest_error is None and event_type == "agent_error" and data.get("target") == target:
                latest_error = event
            elif latest_reply is None and event_type == "message" and data.get("sender") == target:
                latest_reply = event

        if latest_usage is None and event_type == "agent_usage":
            metric_data = data if isinstance(data, dict) else {}
            if _event_matches_metric_target(metric_data, target, task_id):
                usage = metric_data.get("usage")
                latest_usage = usage if isinstance(usage, dict) else {}

        if latest_prompt_metrics is None and event_type == "agent_prompt_metrics":
            metric_data = data if isinstance(data, dict) else {}
            if _event_matches_metric_target(metric_data, target, task_id):
                latest_prompt_metrics = metric_data

        activity_complete = (
            scanned > BACKEND_ACTIVITY_SCAN_LIMIT
            or (latest_started and latest_finished and latest_reply and latest_error)
        )
        if activity_complete and latest_usage is not None and latest_prompt_metrics is not None:
            break
    started_at = _event_time(latest_started or {})
    finished_at = _event_time(latest_finished or {})
    busy = bool(started_at and (not finished_at or started_at > finished_at))
    return {
        "activity": {
            "busy": busy,
            "last_started_at": started_at or None,
            "last_finished_at": finished_at or None,
            "last_reply_at": _event_time(latest_reply or {}) or None,
            "last_error_at": _event_time(latest_error or {}) or None,
        },
        "latest_usage": latest_usage or {},
        "latest_prompt_metrics": latest_prompt_metrics or {},
    }


def _latest_agent_usage(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    return _backend_event_runtime(root, run_id, target, task_id)["latest_usage"]


def _latest_agent_prompt_metrics(root: Path, run_id: str, target: str, task_id: str | None = None) -> dict:
    return _backend_event_runtime(root, run_id, target, task_id)["latest_prompt_metrics"]


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _codex_session_jsonl_path(
    session_id: str,
    *,
    distro: str | None = None,
    native_home: str | None = None,
) -> Path | None:
    safe_id = str(session_id or "").strip()
    if not safe_id:
        return None
    candidates = list((Path.home() / ".codex" / "sessions").glob(f"**/*{safe_id}.jsonl"))
    if not candidates and distro and native_home:
        candidates = list(_wsl_session_paths(distro, native_home, Path(".codex") / "sessions", f"**/*{safe_id}.jsonl"))
    return candidates[0] if candidates else None


def _claude_session_jsonl_path(
    session_id: str,
    *,
    distro: str | None = None,
    native_home: str | None = None,
) -> Path | None:
    safe_id = str(session_id or "").strip()
    if not safe_id:
        return None
    candidates = list((Path.home() / ".claude" / "projects").glob(f"*/*{safe_id}.jsonl"))
    if not candidates and distro and native_home:
        candidates = list(_wsl_session_paths(distro, native_home, Path(".claude") / "projects", f"*/*{safe_id}.jsonl"))
    return candidates[0] if candidates else None


def _wsl_session_paths(distro: str, native_home: str, rel: Path, pattern: str) -> list[Path]:
    """Resolve claude/codex session files under a WSL native home via UNC.

    The Web service runs on Windows and uses ``Path.home()`` for session
    lookup; a WSL backend writes its claude/codex sessions under the distro's
    native home (e.g. ``/home/kaikai/.claude``), which is unreachable from a
    Windows ``Path.home()``. Map the native home to ``\\wsl.localhost\\<distro>\\...``
    so the Windows process can read the same session files (single copy).
    """
    from aha_cli.store.ws_target import wsl_unc_from_native

    unc = wsl_unc_from_native(distro, native_home)
    if not unc:
        return []
    return list((Path(unc) / rel).glob(pattern))


def _claude_assistant_usage(record: dict) -> tuple[str, dict] | None:
    if record.get("type") != "assistant" or record.get("is_api_error_message"):
        return None
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    response_id = str(message.get("id") or "").strip()
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    if not response_id or str(message.get("model") or "").strip() == "<synthetic>":
        return None
    normalized = {
        key: value
        for key, value in {
            "input_tokens": _positive_int(usage.get("input_tokens")),
            "cache_read_input_tokens": _positive_int(usage.get("cache_read_input_tokens")),
            "cache_creation_input_tokens": _positive_int(usage.get("cache_creation_input_tokens")),
            "output_tokens": _positive_int(usage.get("output_tokens")),
        }.items()
        if value is not None
    }
    effective_input = sum(
        int(normalized.get(key) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return (response_id, normalized) if effective_input > 0 else None


def _codex_token_count_info(record: dict) -> dict:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    if payload.get("type") == "token_count" and info:
        return info
    if record.get("type") == "token_count":
        record_info = record.get("info") if isinstance(record.get("info"), dict) else {}
        return record_info or info or payload
    if info and (info.get("model_context_window") or info.get("last_token_usage")):
        return info
    if payload.get("model_context_window") or payload.get("last_token_usage"):
        return payload
    return {}


def _codex_token_count_sample(record: dict) -> dict:
    info = _codex_token_count_info(record)
    if not info:
        return {}
    usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    input_tokens = _positive_int(usage.get("input_tokens"))
    context_window = _positive_int(info.get("model_context_window"))
    if input_tokens is None or context_window is None:
        return {}
    percent = round(input_tokens / context_window * 100, 2) if context_window else None
    return {
        "timestamp": str(record.get("timestamp") or record.get("ts") or ""),
        "input_tokens": input_tokens,
        "cached_input_tokens": _positive_int(usage.get("cached_input_tokens")),
        "output_tokens": _positive_int(usage.get("output_tokens")),
        "reasoning_output_tokens": _positive_int(usage.get("reasoning_output_tokens")),
        "total_tokens": _positive_int(usage.get("total_tokens")),
        "context_window": context_window,
        "percent": percent,
    }


def _codex_token_count_samples(session_id: str, *, limit: int = CODEX_CONTEXT_WINDOW_SCAN_LIMIT) -> list[dict]:
    path = _codex_session_jsonl_path(session_id)
    if not path:
        return []
    samples: list[dict] = []
    scanned = 0
    for _offset, record in iter_jsonl_reverse(path) or ():
        scanned += 1
        sample = _codex_token_count_sample(record)
        if sample:
            samples.append(sample)
        if scanned >= limit:
            break
    return list(reversed(samples))


def detect_runtime_context_compaction(root: Path, run_id: str, target: str, task_id: str | None, session: dict | None = None) -> dict:
    """Infer silent backend context compaction from runtime token_count drops.

    Some Codex sessions reduce the retained conversation without emitting a
    machine-readable compact event. The token_count stream still shows a sharp
    drop in last-turn input tokens for the same backend session; use that as a
    conservative signal so the next AHA prompt can be full again.
    """
    del root, run_id, target, task_id
    backend_session_id = str((session or {}).get("backend_session_id") or "").strip()
    if not backend_session_id:
        return {}
    samples = _codex_token_count_samples(backend_session_id)
    peak: dict | None = None
    detected: dict | None = None
    for sample in samples:
        current = int(sample.get("input_tokens") or 0)
        window = int(sample.get("context_window") or 0)
        if not current or not window:
            if peak:
                previous = int(peak.get("input_tokens") or 0)
                prev_percent = float(peak.get("percent") or 0.0)
                drop_tokens = previous - current
                drop_percent = prev_percent
                if (
                    prev_percent >= CODEX_CONTEXT_DROP_MIN_PREVIOUS_PERCENT
                    and drop_percent >= CODEX_CONTEXT_DROP_MIN_DELTA_PERCENT
                    and drop_tokens >= CODEX_CONTEXT_DROP_MIN_DELTA_TOKENS
                ):
                    detected = {
                        "backend_session_id": backend_session_id,
                        "previous": peak,
                        "current": sample,
                        "drop_tokens": drop_tokens,
                        "drop_percent": round(drop_percent, 2),
                    }
            continue
        if peak is None or current >= int(peak.get("input_tokens") or 0):
            peak = sample
            continue
        previous = int(peak.get("input_tokens") or 0)
        prev_percent = float(peak.get("percent") or 0.0)
        current_percent = float(sample.get("percent") or 0.0)
        drop_tokens = previous - current
        drop_percent = prev_percent - current_percent
        if (
            prev_percent >= CODEX_CONTEXT_DROP_MIN_PREVIOUS_PERCENT
            and current_percent <= CODEX_CONTEXT_DROP_MAX_CURRENT_PERCENT
            and drop_percent >= CODEX_CONTEXT_DROP_MIN_DELTA_PERCENT
            and drop_tokens >= CODEX_CONTEXT_DROP_MIN_DELTA_TOKENS
        ):
            detected = {
                "backend_session_id": backend_session_id,
                "previous": peak,
                "current": sample,
                "drop_tokens": drop_tokens,
                "drop_percent": round(drop_percent, 2),
            }
        # After a drop, keep tracking from the lower baseline so a later growth
        # does not erase the latest detected compaction signal.
        if current_percent < CODEX_CONTEXT_DROP_MAX_CURRENT_PERCENT:
            peak = sample
    if not detected:
        return {}
    previous = detected["previous"]
    current = detected["current"]
    signature_basis = "|".join(
        [
            backend_session_id,
            str(previous.get("timestamp") or ""),
            str(previous.get("input_tokens") or ""),
            str(current.get("timestamp") or ""),
            str(current.get("input_tokens") or ""),
        ]
    )
    detected["signature"] = "runtime_drop:" + hashlib.sha1(signature_basis.encode("utf-8")).hexdigest()[:16]
    return detected


def _codex_runtime_context(
    root: Path,
    run_id: str,
    target: str,
    task_id: str | None = None,
    *,
    distro: str | None = None,
    native_home: str | None = None,
) -> dict:
    session_file = session_path(root, run_id, task_id, target)
    if not session_file.exists():
        return {}
    try:
        session = read_json(session_file)
    except (OSError, ValueError):
        return {}
    path = _codex_session_jsonl_path(str(session.get("backend_session_id") or ""), distro=distro, native_home=native_home)
    if not path:
        return {}
    scanned = 0
    for _offset, record in iter_jsonl_reverse(path) or ():
        scanned += 1
        info = _codex_token_count_info(record)
        if info:
            usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            return {
                "context_window": _positive_int(info.get("model_context_window")),
                "last_token_usage": {
                    key: value
                    for key, value in {
                        "input_tokens": _positive_int(usage.get("input_tokens")),
                        "cached_input_tokens": _positive_int(usage.get("cached_input_tokens")),
                        "output_tokens": _positive_int(usage.get("output_tokens")),
                        "reasoning_output_tokens": _positive_int(usage.get("reasoning_output_tokens")),
                        "total_tokens": _positive_int(usage.get("total_tokens")),
                    }.items()
                    if value is not None
                },
                "source": "runtime",
            }
        if scanned >= CODEX_CONTEXT_WINDOW_SCAN_LIMIT:
            break
    return {}


def _claude_runtime_context(
    root: Path,
    run_id: str,
    target: str,
    task_id: str | None = None,
    *,
    state: dict | None = None,
    cfg: dict | None = None,
) -> dict:
    session_file = session_path(root, run_id, task_id, target)
    session: dict = {}
    if session_file.exists():
        try:
            session = read_json(session_file)
        except (OSError, ValueError):
            session = {}

    state = state if isinstance(state, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else load_config(root)
    requested_model = state.get("requested_model")
    if requested_model is None:
        requested_model = session.get("requested_model") or session.get("model") or state.get("model")
    claude_cfg = cfg.get("claude") if isinstance(cfg.get("claude"), dict) else {}
    selected_config = claude_config_for_model(claude_cfg, requested_model)
    context_window = claude_context_window(selected_config)

    distro = str(state.get("wsl_distro") or "").strip() or None
    native_home = str(state.get("wsl_native_home") or "").strip() or None
    path = _claude_session_jsonl_path(str(session.get("backend_session_id") or ""), distro=distro, native_home=native_home)
    if not path:
        return {"context_window": context_window, "last_token_usage": {}, "source": "runtime"}

    scanned = 0
    for _offset, record in iter_jsonl_reverse(path) or ():
        scanned += 1
        candidate = _claude_assistant_usage(record)
        if candidate:
            response_id, usage = candidate
            # The transcript may repeat one response ID as its content streams.
            # Reading in reverse and returning one snapshot prevents duplicates
            # from being summed as though they were separate model requests.
            return {
                "context_window": context_window,
                "last_token_usage": usage,
                "response_id": response_id,
                "source": "runtime",
            }
        if scanned >= CLAUDE_CONTEXT_WINDOW_SCAN_LIMIT:
            break
    return {"context_window": context_window, "last_token_usage": {}, "source": "runtime"}


def _process_matches_task(parts: list[str], task_id: str | None) -> bool:
    if "--task-id" not in parts:
        return task_id is None
    if task_id is None:
        return False
    index = parts.index("--task-id")
    return len(parts) > index + 1 and parts[index + 1] == task_id


def _process_matches_home(parts: list[str], root: Path) -> bool:
    if "--home" not in parts:
        return False
    index = parts.index("--home")
    if len(parts) <= index + 1:
        return False
    try:
        process_home = Path(parts[index + 1]).expanduser().resolve()
        expected_home = root.expanduser().resolve()
    except OSError:
        return False
    return process_home == expected_home


def _backend_name_from_state(state: dict, fallback: str = "unknown") -> str:
    return str(state.get("backend") or fallback)


def _discover_backend_process(root: Path, run_id: str, target: str, task_id: str | None = None) -> tuple[int, str] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    current_pid = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == current_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        chat_commands = [command for command in ("codex-chat", "claude-chat") if command in parts]
        if not chat_commands:
            continue
        index = parts.index(chat_commands[0])
        if (
            len(parts) > index + 2
            and parts[index + 1] == run_id
            and parts[index + 2] == target
            and _process_matches_task(parts, task_id)
            and _process_matches_home(parts, root)
            and pid_is_running(pid)
        ):
            return pid, chat_commands[0]
    return None


def _task_workspace_path(root: Path, run_id: str, task_id: str | None) -> str | None:
    """Return the workspace_path for a task, or None when unavailable."""
    if not task_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except (KeyError, SystemExit):
        return None
    workspace = str(task.get("workspace_path") or "").strip()
    return workspace or None


def _provider_id_for_model(cfg: dict, backend: str, model: str | None) -> str | None:
    """Resolve the provider id behind a model selector.

    The active model is usually ``env:<env-group-name>`` (e.g.
    ``env:deepseek-deepseek-v4-flash-452b42ce``); the env group carries the
    provider's ``AHA_PROVIDER_ID``. This lets context-window resolution match the
    exact provider so the same model_id bound to two providers does not share a
    window. Returns ``None`` when the provider cannot be determined.
    """
    selector = str(model or "").strip()
    if not selector:
        return None
    group_name = selector[len("env:") :].strip() if selector.startswith("env:") else ""
    if not group_name:
        return None
    section = cfg.get(backend) if isinstance(cfg.get(backend), dict) else {}
    raw_groups = section.get("env")
    if isinstance(raw_groups, dict):
        raw_groups = [raw_groups]
    for group in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(group, dict):
            continue
        if str(group.get("name") or "").strip() == group_name:
            provider_id = str(group.get("AHA_PROVIDER_ID") or "").strip()
            return provider_id or None
    return None


def _state_wsl_context(state: dict) -> tuple[str | None, str | None]:
    """Return (distro, native_home) for a WSL backend from its state.

    Prefers the fields stored by ``start_backend``. Backends started before WSL
    home probing stored no home; derive it from the recorded launch command
    (e.g. ``wsl.exe -d <distro> ... --claude-bin /home/<user>/...``) so session
    lookup still works for already-running WSL tasks.
    """
    distro = str(state.get("wsl_distro") or "").strip() or None
    native_home = str(state.get("wsl_native_home") or "").strip() or None
    if distro and native_home:
        return distro, native_home
    command = state.get("command")
    if not isinstance(command, list) or not command or not str(command[0]).endswith("wsl.exe"):
        return distro, native_home
    if len(command) > 3 and str(command[1]) == "-d":
        distro = distro or str(command[2])
    # The inner command is a single ``bash -c`` script string, so also scan the
    # joined command line for --claude-bin/--codex-bin (e.g.
    # ``--claude-bin /home/kaikai/.local/bin/claude``).
    joined = " ".join(str(part) for part in command)
    for flag in ("--claude-bin", "--codex-bin"):
        marker = f"{flag} "
        if marker in joined:
            value = joined.split(marker, 1)[1].strip().split()[0]
            parts = value.split("/")
            if len(parts) >= 3 and parts[0] == "" and parts[1] == "home":
                native_home = native_home or f"/home/{parts[2]}"
    return distro, native_home


def _state_pid_is_cross_os_uncheckable(state: dict) -> bool:
    if not _WINDOWS:
        return False
    command = state.get("command")
    executable = str(command[0] if isinstance(command, list) and command else "").replace("\\", "/")
    executable_name = executable.rsplit("/", 1)[-1].lower()
    if executable_name in {"wsl", "wsl.exe"}:
        return False
    return bool(str(state.get("wsl_distro") or "").strip() or executable.startswith("/"))


def _task_wsl_context(root: Path, run_id: str, task_id: str | None) -> tuple[str | None, str | None]:
    if not task_id:
        return None, None
    workspace = _task_workspace_path(root, run_id, task_id)
    from aha_cli.services.wsl_backend import cached_wsl_backends
    from aha_cli.store.ws_target import wsl_distro_and_path

    distro, native_workspace = wsl_distro_and_path(workspace)
    if not distro:
        return None, None
    cached = cached_wsl_backends(root, distro) or {}
    native_home = str(cached.get("home") or "").strip() or None
    if native_home:
        return distro, native_home
    parts = PurePosixPath(str(native_workspace or "")).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home":
        native_home = str(PurePosixPath(*parts[:3]))
    elif parts[:2] == ("/", "root"):
        native_home = "/root"
    return distro, native_home


def backend_status(root: Path, run_id: str, target: str = "main", task_id: str | None = None) -> dict:
    require_plan(root, run_id)
    target = target or "main"
    task_id = task_id or None
    state = _read_state(root, run_id, target, task_id)
    state_pid = int(state.get("pid") or 0) or None
    pid = None if state.get("status") == "stopped" else state_pid
    managed = bool(state.get("managed")) if state else False
    event_runtime = _backend_event_runtime(root, run_id, target, task_id)
    activity = event_runtime["activity"]
    running = pid_is_running(pid)
    discovered_backend = None
    discovered = None if running else _discover_backend_process(root, run_id, target, task_id)
    if discovered:
        pid, discovered_backend = discovered
        running = True
        managed = bool(state.get("managed")) if state and state.get("pid") == pid else False
    # Cross-OS fallback: a WSL-hosted backend (workspace is a WSL path) has a
    # Linux pid that a Windows-side Web service cannot resolve with OpenProcess.
    # When the state is not explicitly stopped and a turn is in flight, the
    # backend is alive even though the pid is not checkable from this OS.
    if (
        not running
        and pid
        and str(state.get("status") or "").strip().lower() != "stopped"
        and activity.get("busy")
        and _state_pid_is_cross_os_uncheckable(state)
    ):
        running = True
    status = "busy" if running and activity["busy"] else "running" if running else "stopped"
    backend_name = _backend_name_from_state(state, discovered_backend or "unknown")
    resolved_model = state.get("resolved_model") or state.get("model")
    requested_model = state.get("requested_model") or state.get("model") or resolved_model
    latest_usage = event_runtime["latest_usage"]
    latest_prompt_metrics = event_runtime["latest_prompt_metrics"]
    cfg = load_config(root)
    normalized_backend_name = str(backend_name).removesuffix("-chat")
    wsl_distro, wsl_native_home = _state_wsl_context(state)
    if not wsl_distro or not wsl_native_home:
        task_wsl_distro, task_wsl_native_home = _task_wsl_context(root, run_id, task_id)
        wsl_distro = wsl_distro or task_wsl_distro
        wsl_native_home = wsl_native_home or task_wsl_native_home
    if normalized_backend_name == "codex":
        runtime_context = _codex_runtime_context(
            root,
            run_id,
            target,
            task_id,
            distro=wsl_distro,
            native_home=wsl_native_home,
        )
    elif normalized_backend_name == "claude":
        if wsl_distro or wsl_native_home:
            state = {**state, "wsl_distro": wsl_distro, "wsl_native_home": wsl_native_home}
        runtime_context = _claude_runtime_context(root, run_id, target, task_id, state=state, cfg=cfg)
    else:
        runtime_context = {}
    runtime_context_window = _positive_int(runtime_context.get("context_window"))
    if normalized_backend_name != "claude":
        runtime_context_window = (
            runtime_context_window
            or _positive_int(latest_usage.get("context_window"))
            or _positive_int(latest_usage.get("model_context_window"))
        )
    provider_id = _provider_id_for_model(cfg, normalized_backend_name, requested_model)
    runtime_context_usage = runtime_context.get("last_token_usage") if isinstance(runtime_context.get("last_token_usage"), dict) else {}
    # The runtime context reads claude/codex session files under the process
    # HOME. For a WSL backend the Windows-side Web service cannot always reach
    # them (they live in the distro's native home); fall back to the latest
    # agent_usage event, which the backend already wrote to the single-copy
    # events.jsonl, so context pressure keeps refreshing in WSL workspaces.
    pressure_runtime_usage = runtime_context_usage or latest_usage
    return {
        "target": target,
        "task_id": task_id,
        "backend": backend_name,
        "status": status,
        "pid": pid if running else None,
        "last_pid": state_pid if not running else None,
        "managed": managed,
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target, task_id)),
        "command": state.get("command", []),
        "model": state.get("model"),
        "requested_model": state.get("requested_model"),
        "resolved_model": state.get("resolved_model"),
        "reasoning_effort": state.get("reasoning_effort"),
        "runtime_context_window": runtime_context_window,
        "runtime_context_usage": pressure_runtime_usage,
        "latest_usage": latest_usage,
        "latest_prompt_metrics": latest_prompt_metrics,
        "context_pressure": context_pressure(
            backend_name,
            str(resolved_model) if resolved_model else None,
            latest_prompt_metrics,
            runtime_context_window=runtime_context_window,
            runtime_token_usage=pressure_runtime_usage,
            cfg=cfg,
            prefer_runtime_context_window=normalized_backend_name == "claude" and runtime_context_window is not None,
            provider_id=provider_id,
        ),
        **activity,
    }


def _process_cmdline_parts(pid: int) -> list[str]:
    if not Path("/proc").is_dir():
        return []
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _pid_is_backend_worker(pid: int, run_id: str, target: str, task_id: str | None, root: Path) -> bool:
    """Whether ``pid`` is a live backend worker for this run/target/task.

    A WSL backend records the Windows-side ``wsl.exe`` host pid in the state file
    while the worker inside the distro sees its own Linux-side pid via
    ``os.getpid()``. The two live in different pid namespaces and can never be
    equal, so a raw pid comparison wrongly rejects a legitimate worker self-stop.
    Match the caller's own command line instead: the worker process carries the
    ``{backend}-chat <run_id> <target> [--task-id <task_id>] --home <root>``
    signature, which uniquely identifies it regardless of pid namespace.
    """
    parts = _process_cmdline_parts(pid)
    chat_commands = [command for command in ("codex-chat", "claude-chat") if command in parts]
    if not chat_commands:
        return False
    index = parts.index(chat_commands[0])
    return (
        len(parts) > index + 2
        and parts[index + 1] == run_id
        and parts[index + 2] == target
        and _process_matches_task(parts, task_id)
        and _process_matches_home(parts, root)
    )


def _same_backend_process(
    state: dict,
    pid: int,
    run_id: str,
    target: str,
    task_id: str | None,
    root: Path,
) -> bool:
    """Whether ``pid`` is the same backend worker that ``state`` records.

    Accept the stop when the pid matches the recorded state, when the recorded
    process has exited (nothing newer holds the state), or when the caller is a
    live backend worker for this run/target/task (the WSL worker's own pid lives
    in a different namespace than the recorded ``wsl.exe`` host pid).
    """
    state_pid = int(state.get("pid") or 0) or None
    if not state_pid:
        return True
    if state_pid == int(pid):
        return True
    if not pid_is_running(state_pid):
        return True
    return _pid_is_backend_worker(int(pid), run_id, target, task_id, root)


def mark_backend_stopped(root: Path, run_id: str, target: str = "main", *, task_id: str | None = None, pid: int | None = None) -> dict:
    task_id = task_id or None
    target = target or "main"
    with locked_backend(root, run_id, target, task_id):
        state = _read_state(root, run_id, target, task_id)
        state_pid = int(state.get("pid") or 0) or None
        previous_pid = int(pid or state_pid or 0) or None
        if pid and state_pid and not _same_backend_process(state, int(pid), run_id, target, task_id, root) and state.get("status") != "stopped":
            append_event(
                root,
                run_id,
                "backend_stop_ignored",
                {"target": target, "task_id": task_id, "pid": pid, "current_pid": state_pid},
            )
            return backend_status(root, run_id, target, task_id) | {"stale_stop_ignored": True}
        state.update(
            {
                "target": target,
                "task_id": task_id,
                "backend": _backend_name_from_state(state),
                "status": "stopped",
                "pid": previous_pid,
                "managed": bool(state),
                "stopped_at": utc_now(),
                "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target, task_id)),
                "command": state.get("command", []),
            }
        )
        _write_state(root, run_id, target, state, task_id)
        append_event(root, run_id, "backend_stopped", {"target": target, "task_id": task_id, "pid": previous_pid})
        return backend_status(root, run_id, target, task_id) | {"stopped": True}

def stop_task_backends(root: Path, run_id: str, task_id: str, *, exclude_pid: int | None = None, timeout: float = 5.0) -> list[dict]:
    plan = require_plan(root, run_id)
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    if not task:
        return []
    stopped: list[dict] = []
    for agent in task.get("agents", []):
        target = str(agent.get("id") or "main")
        state = backend_status(root, run_id, target, task_id)
        pid = int(state.get("pid") or 0) or None
        if not pid or state.get("status") == "stopped":
            continue
        if exclude_pid and pid == int(exclude_pid):
            continue
        stopped.append(stop_backend(root, run_id, target, task_id=task_id, timeout=timeout))
    if stopped:
        append_event(
            root,
            run_id,
            "task_backends_stopped",
            {
                "task_id": task_id,
                "count": len(stopped),
                "targets": [item.get("target") for item in stopped],
            },
        )
    return stopped


def stop_all_backends(root: Path, *, timeout: float = 5.0) -> dict:
    """Stop every live backend worker owned by this AHA home."""
    from aha_cli.store.runs import list_run_summaries

    stopped: list[dict] = []
    errors: list[dict] = []
    checked = 0
    for summary in list_run_summaries(root):
        run_id = str(summary.get("id") or "")
        if not run_id:
            continue
        try:
            plan = require_plan(root, run_id)
        except (OSError, TimeoutError, ValueError, KeyError, SystemExit) as exc:
            errors.append({"run_id": run_id, "error": str(exc)})
            continue
        refs: list[tuple[str, str | None]] = []
        if backend_state_path(root, run_id).exists():
            refs.append(("main", None))
        for task in plan.get("tasks", []):
            if task.get("deleted_at"):
                continue
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            refs.extend(
                (str(agent.get("id") or "main"), task_id)
                for agent in task.get("agents", [])
                if backend_state_path(root, run_id, str(agent.get("id") or "main"), task_id).exists()
            )
        seen: set[tuple[str, str | None]] = set()
        for target, task_id in refs:
            key = (target, task_id)
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            try:
                state = _read_state(root, run_id, target, task_id)
                if state.get("status") == "stopped" or not state.get("pid"):
                    continue
                stopped.append(stop_backend(root, run_id, target, task_id=task_id, timeout=timeout))
            except Exception as exc:  # noqa: BLE001 - one backend must not block Web shutdown
                errors.append({"run_id": run_id, "task_id": task_id, "target": target, "error": str(exc)})
    return {"checked": checked, "stopped": stopped, "errors": errors}


def _running_zipapp_path() -> Path | None:
    raw_path = sys.argv[0] if sys.argv else ""
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).expanduser().resolve()
    except OSError:
        return None
    try:
        if candidate.is_file() and zipfile.is_zipfile(candidate):
            return candidate
    except OSError:
        return None
    return None


def _aha_cli_invocation() -> list[str]:
    zipapp_path = _running_zipapp_path()
    if zipapp_path:
        return [sys.executable, str(zipapp_path)]
    return [sys.executable, "-m", "aha_cli"]


def _shlex_quote_each(parts: list[str]) -> list[str]:
    return [shlex.quote(str(part)) for part in parts]


def _resolve_wsl_target(
    root: Path,
    workspace: str | None,
    backend: str,
) -> dict | None:
    """Build a WSL launch target when the workspace is a WSL path.

    Returns a dict with ``distro``, ``aha_home`` (WSL-native), ``aha_bin``
    (onebin path as seen from WSL), and ``backend_bin`` (native codex/claude),
    or ``None`` when the workspace is not WSL or no native backend is available.
    """
    from aha_cli.store.ws_target import is_wsl_workspace, wsl_distro_and_path, wsl_native_home
    from aha_cli.services.wsl_backend import wsl_backends_for_workspace

    if not is_wsl_workspace(workspace):
        return None
    distro, _native = wsl_distro_and_path(workspace)
    if not distro:
        return None
    backends = wsl_backends_for_workspace(root, distro)
    backend_bin = backends.get(backend)
    if not backend_bin:
        return None
    wsl_home = wsl_native_home(root)
    if not wsl_home:
        return None
    # The WSL watcher needs the same AHA onebin reachable inside the distro. When
    # AHA is not running from a zipapp (source/editable install), there is no
    # portable binary to hand to WSL, so fall back to the Windows backend rather
    # than launching a WSL process that cannot start aha_cli.
    onebin_path = _running_zipapp_path()
    if not onebin_path:
        return None
    aha_bin = wsl_native_home(onebin_path) or str(onebin_path)
    return {
        "distro": distro,
        "aha_home": wsl_home,
        "aha_bin": aha_bin,
        "backend_bin": backend_bin,
        # WSL native home of the backend user (e.g. /home/kaikai), probed from
        # the distro so the Windows Web service can reach backend session files.
        "native_home": str(backends.get("home") or "").strip() or None,
        # Native python inside the distro (excludes /mnt/* shims) used to run
        # the onebin; without it we would fall back to PATH "python3", which can
        # resolve to a Windows shim and fail to launch.
        "python": str(backends.get("python3") or "").strip() or None,
    }


def _agent_chat_command(
    run_id: str,
    target: str,
    *,
    backend: str = "codex",
    aha_home: Path,
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
    prompt_prefix: str = render_prompt_template("backend_prompt_prefix.md").strip(),
    task_id: str | None = None,
    wsl_target: dict | None = None,
) -> list[str]:
    if backend not in PROCESS_AGENT_BACKENDS:
        raise ValueError(f"backend {backend} does not have a chat process")
    command_model = resolve_model(backend, model)
    command = [
        *_aha_cli_invocation(),
        "--home",
        str(aha_home),
        f"{backend}-chat",
        run_id,
        target,
        "--sender",
        target,
        "--sandbox",
        sandbox,
        "--approval",
        approval,
        "--interval",
        str(interval),
        "--prompt-prefix",
        prompt_prefix,
    ]
    if backend == "codex":
        command.extend(["--codex-bin", codex_bin])
    else:
        command.extend(["--claude-bin", claude_bin])
    if task_id:
        command.extend(["--task-id", task_id])
    if command_model:
        command.extend(["--model", command_model])
        if backend == "codex" and not model:
            command.extend(["--requested-model", ""])
    if reasoning_effort:
        command.extend(["--reasoning-effort", reasoning_effort])
    if from_start:
        command.append("--from-start")
    if no_json and backend == "codex":
        command.append("--no-json")
    for item in extra_args or []:
        command.extend(["--extra-arg", item])
    if not wsl_target:
        return command
    # Run the whole backend watcher inside the WSL distro so codex/claude operate
    # on native Linux paths. The inner command is a Python invocation of the same
    # onebin via the WSL-mapped AHA home.
    distro = str(wsl_target.get("distro") or "").strip()
    wsl_home = str(wsl_target.get("aha_home") or "").strip()
    default_bin = codex_bin if backend == "codex" else claude_bin
    inner_bin = str(wsl_target.get("backend_bin") or default_bin).strip()
    inner_python = str(wsl_target.get("python") or "python3").strip()
    aha_bin = str(wsl_target.get("aha_bin") or "").strip()
    if not distro or not wsl_home or not aha_bin:
        return command
    # Rebuild the inner command with the WSL-mapped home and bin paths. Prefer a
    # native python probed from the distro (absolute path, excludes /mnt/* shims)
    # so the onebin never launches through a Windows python3 shim. Only fall back
    # to PATH "python3" when the probe returned nothing.
    inner: list[str] = [inner_python]
    if aha_bin == "-m":
        # Fallback when the running AHA is not a zipapp: python -m aha_cli.
        inner.extend(["-m", "aha_cli"])
    else:
        inner.append(aha_bin)
    inner.extend([
        "--home",
        wsl_home,
        f"{backend}-chat",
        run_id,
        target,
        "--sender",
        target,
        "--sandbox",
        sandbox,
        "--approval",
        approval,
        "--interval",
        str(interval),
        "--prompt-prefix",
        prompt_prefix,
    ])
    inner_bin_key = "--codex-bin" if backend == "codex" else "--claude-bin"
    inner.extend([inner_bin_key, inner_bin])
    if task_id:
        inner.extend(["--task-id", task_id])
    if command_model:
        inner.extend(["--model", command_model])
        if backend == "codex" and not model:
            inner.extend(["--requested-model", ""])
    if reasoning_effort:
        inner.extend(["--reasoning-effort", reasoning_effort])
    if from_start:
        inner.append("--from-start")
    if no_json and backend == "codex":
        inner.append("--no-json")
    for item in extra_args or []:
        inner.extend(["--extra-arg", item])
    # Quote each argument individually but do NOT wrap the whole script in an
    # extra layer of single quotes: wsl.exe forwards the -c argument verbatim,
    # so a fully wrapped script becomes one word inside bash and cannot be
    # exec'd ("No such file or directory", exit 127).
    bash_script = " ".join(_shlex_quote_each(inner))
    return ["wsl.exe", "-d", distro, "--", "bash", "-c", bash_script]


def _backend_proxy_env(root: Path, run_id: str, target: str, task_id: str | None) -> dict[str, str] | None:
    if not task_id:
        return None
    try:
        plan = require_plan(root, run_id)
    except SystemExit:
        return None
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    if not task:
        return None
    agent = next((item for item in task.get("agents", []) if item.get("id") == target), None)
    if not agent:
        return None
    return proxy_env_for_agent(agent, task, plan, load_config(root))


def _backend_process_env(
    proxy_env: dict[str, str] | None = None,
    claude_config: dict | None = None,
    codex_config: dict | None = None,
    aha_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    if pythonpath:
        cwd = Path.cwd()
        env["PYTHONPATH"] = os.pathsep.join(
            str((cwd / item).resolve()) if item and not Path(item).is_absolute() else item
            for item in pythonpath.split(os.pathsep)
        )
    _add_user_backend_paths(env)
    apply_codex_environment(env, codex_config)
    apply_claude_environment(env, claude_config)
    apply_proxy_environment(env, proxy_env)
    if aha_env:
        env.update({key: value for key, value in aha_env.items() if value})
    return env


# Windows basics wsl.exe may rely on. Everything else from the service
# environment (PATH, provider keys, proxies) must stay on the Windows side.
_WSL_LAUNCH_ENV_PASS_THROUGH = ("SystemDrive", "WINDIR", "COMSPEC", "TEMP", "TMP")

_WSL_PROXY_ENV_KEY_SET = frozenset(key.upper() for key in PROXY_ENV_KEYS)


def _windows_root_fallback() -> str:
    """Derive the Windows directory without assuming the install drive.

    Real Windows processes always carry SystemRoot (D:\\Windows stays
    D:\\Windows), so this only matters for scrubbed/test environments.
    """
    system_root = os.environ.get("SystemRoot")
    if system_root:
        return system_root
    system_drive = os.environ.get("SystemDrive") or r"C:"
    return system_drive.rstrip("\\") + "\\Windows"


def _wsl_backend_process_env(
    aha_env: dict[str, str],
    proxy_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Minimal environment for the wsl.exe backend hop.

    wsl.exe flows the calling process's PATH into the distro PATH *ahead* of
    the Linux default directories, so handing it a full ``os.environ`` copy
    puts translated Windows tool dirs (e.g. the AHA install dir with its
    CRLF python3 shim) in front of /usr/bin — every ``python3``/``node``
    lookup inside the backend then resolves to a Windows shim and fails.
    Pass only the Windows basics plus the WSLENV-forwarded AHA variables;
    with no PATH of its own, the distro keeps its clean default PATH (the
    registry-appended Windows tail from ``appendWindowsPath`` lands after
    the Linux dirs and cannot hijack lookups). Task/agent proxy vars are the
    one non-AHA payload allowed through, so WSL backends honor the same
    egress config as Windows ones (the caller must also declare them in
    WSLENV or wsl.exe will not forward them).
    """
    env = {"SystemRoot": _windows_root_fallback()}
    for key in _WSL_LAUNCH_ENV_PASS_THROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    for key, value in aha_env.items():
        if value and (key == "WSLENV" or key.startswith("AHA_")):
            env[key] = value
    for key, value in (proxy_env or {}).items():
        if value and key.upper() in _WSL_PROXY_ENV_KEY_SET:
            env[key] = value
    return env


def _configured_reasoning_effort(cfg: dict, backend: str) -> str | None:
    section = cfg.get(backend) if isinstance(cfg.get(backend), dict) else {}
    return normalize_reasoning_effort(section.get("reasoning_effort"), backend)


def _effective_backend_reasoning_effort(
    root: Path,
    run_id: str,
    target: str,
    task_id: str | None,
    backend: str,
    cfg: dict,
    requested: str | None,
) -> str | None:
    if requested is not None:
        return normalize_reasoning_effort(requested, backend)
    if task_id:
        try:
            detail = task_snapshot(root, run_id, task_id)
        except (KeyError, SystemExit):
            return _configured_reasoning_effort(cfg, backend)
        task = detail["task"]
        agent = next((item for item in task.get("agents", []) if item.get("id") == target), {})
        value = agent.get("reasoning_effort")
        if value is None:
            value = task.get("preferred_reasoning_effort")
        if value is not None:
            return normalize_reasoning_effort(value, backend)
    return _configured_reasoning_effort(cfg, backend)


def _add_user_backend_paths(env: dict[str, str]) -> None:
    add_user_backend_paths(env, home=Path.home())


def start_backend(
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
    prompt_prefix: str = render_prompt_template("backend_prompt_prefix.md").strip(),
    task_id: str | None = None,
) -> dict:
    task_id = task_id or None
    target = target or "main"
    if backend not in PROCESS_AGENT_BACKENDS:
        raise ValueError(f"backend {backend} does not have a chat process")
    cfg = load_config(root)
    # Determine the workspace the backend will operate on. A task's workspace may
    # be a WSL UNC path (\\wsl.localhost\\<distro>\\...); when it is and a native
    # WSL backend exists, run the whole watcher inside WSL so codex/claude operate
    # on native Linux paths instead of Windows UNC paths.
    workspace = _task_workspace_path(root, run_id, task_id)
    if backend == "codex" and not model:
        model = CODEX_DEFAULT_MODEL if task_id else (cfg.get("codex", {}) or {}).get("model")
    if backend == "claude" and not model:
        model = (cfg.get("claude", {}) or {}).get("model")
    requested_model = model
    model = normalize_model_selector(backend, model, cfg)
    reasoning_effort = _effective_backend_reasoning_effort(root, run_id, target, task_id, backend, cfg, reasoning_effort)
    codex_config = codex_config_for_model((cfg.get("codex", {}) or {}), model) if backend == "codex" else None
    claude_config = claude_config_for_model((cfg.get("claude", {}) or {}), model) if backend == "claude" else None
    command_model = (
        claude_cli_model(model)
        if backend == "claude"
        else codex_cli_model(codex_config, model)
        if backend == "codex"
        else model
    )
    resolved_model = claude_resolved_model(claude_config, model) if backend == "claude" else codex_resolved_model(codex_config, model) if backend == "codex" else resolve_model(backend, command_model)
    with locked_backend(root, run_id, target, task_id):
        current = backend_status(root, run_id, target, task_id)
        if current["status"] in {"running", "busy"}:
            current["already_running"] = True
            return current
        wsl_target = _resolve_wsl_target(root, workspace, backend)
        log_path = backend_log_path(root, run_id, target, task_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_env = _backend_proxy_env(root, run_id, target, task_id)
        aha_env = {
            "AHA_ROOT": str(root),
            "AHA_RUN_ID": run_id,
            "AHA_AGENT_ID": target,
            "AHA_BACKEND": backend,
            "AHA_MODEL": resolved_model or "",
            "AHA_GENERATED_BY": generated_by_for_backend_model(backend, resolved_model),
            # Force UTF-8 mode so subprocess pipes and default file I/O inside the
            # backend use UTF-8 regardless of the host locale (e.g. GBK on Chinese
            # Windows), matching the UTF-8 stream-json the CLIs emit.
            "PYTHONUTF8": "1",
        }
        if task_id:
            aha_env["AHA_TASK_ID"] = task_id

        # The actual spawn+state-write+event, parameterized by the launch target.
        # Kept as a closure so a failed WSL launch can fall back to the Windows
        # backend (a WSL workspace should prefer the distro's native backend, but
        # if wsl.exe or the distro probe fails we must not leave the sub-agent dead).
        def launch(launch_command: list[str], launch_wsl: dict | None) -> dict:
            launch_aha_env = dict(aha_env)
            if launch_wsl:
                launch_aha_env["AHA_WSL_DISTRO"] = str(launch_wsl.get("distro") or "").strip()
                launch_aha_env["AHA_WSL_AHA_HOME"] = str(launch_wsl.get("aha_home") or "").strip()
                # WSLENV declares which variables pass through wsl.exe into the
                # distro. Task/agent proxy vars must be declared too, or the WSL
                # watcher and its backend CLI children lose the egress config.
                wslenv_parts = ["AHA_WSL_DISTRO", "AHA_WSL_AHA_HOME"]
                wslenv_parts.extend(
                    key for key in (proxy_env or {}) if key.upper() in _WSL_PROXY_ENV_KEY_SET
                )
                existing_wslenv = os.environ.get("WSLENV", "")
                wslenv_parts.append(existing_wslenv)
                launch_aha_env["WSLENV"] = ":".join(part for part in wslenv_parts if part)
            log_file = log_path.open("ab")
            try:
                # WSL launches get a scrubbed environment: wsl.exe places the
                # caller's translated PATH ahead of the distro defaults, so the
                # full service env (AHA install dir, Windows tool dirs) would
                # hijack PATH lookups inside the backend. Everything the distro
                # needs crosses via WSLENV-declared AHA_* variables instead.
                launch_env = (
                    _wsl_backend_process_env(launch_aha_env, proxy_env)
                    if launch_wsl
                    else _backend_process_env(proxy_env, claude_config, codex_config, launch_aha_env)
                )
                proc = subprocess.Popen(
                    launch_command,
                    cwd=root,
                    env=launch_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    **platform.hidden_subprocess_kwargs(),
                )
            finally:
                log_file.close()
            state = {
                "target": target,
                "task_id": task_id,
                "backend": f"{backend}-chat",
                "status": "running",
                "pid": proc.pid,
                "managed": True,
                "started_at": utc_now(),
                "stopped_at": None,
                "log_path": str(log_path),
                "command": launch_command,
                "sandbox": sandbox,
                "approval": approval,
                "reasoning_effort": reasoning_effort,
                "model": resolved_model,
                "requested_model": requested_model,
                "resolved_model": resolved_model,
                "from_start": from_start,
                "proxy_enabled": proxy_env is not None and bool(proxy_env),
            }
            current_wsl_distro, current_wsl_home = _current_wsl_runtime_context()
            state_wsl_distro = str((launch_wsl or {}).get("distro") or "").strip() or current_wsl_distro
            state_wsl_home = str((launch_wsl or {}).get("native_home") or "").strip() or current_wsl_home
            if state_wsl_distro:
                state["wsl_distro"] = state_wsl_distro
            if state_wsl_home:
                state["wsl_native_home"] = state_wsl_home
            _write_state(root, run_id, target, state, task_id)
            append_event(
                root,
                run_id,
                "backend_started",
                {
                    "target": target,
                    "task_id": task_id,
                    "pid": proc.pid,
                    "log_path": str(log_path),
                    "requested_model": requested_model,
                    "resolved_model": resolved_model,
                },
            )
            return backend_status(root, run_id, target, task_id) | {"started": True}

        if wsl_target:
            # Prefer the WSL backend for a WSL workspace: run the whole watcher in
            # the distro so codex/claude operate on native Linux paths. If the
            # launch itself fails (wsl.exe missing, distro not running, no native
            # backend), fall back to the Windows-side backend instead of leaving
            # the sub-agent dead with a swallowed exception.
            wsl_command = _agent_chat_command(
                run_id,
                target,
                backend=backend,
                aha_home=root,
                codex_bin=codex_bin,
                claude_bin=claude_bin,
                model=model,
                reasoning_effort=reasoning_effort,
                sandbox=sandbox,
                approval=approval,
                interval=interval,
                from_start=from_start,
                no_json=no_json,
                extra_args=extra_args,
                prompt_prefix=prompt_prefix,
                task_id=task_id,
                wsl_target=wsl_target,
            )
            try:
                return launch(wsl_command, wsl_target)
            except OSError as exc:
                # Record the WSL attempt then fall through to the Windows backend.
                append_event(
                    root,
                    run_id,
                    "backend_start_failed",
                    {
                        "target": target,
                        "task_id": task_id,
                        "backend": f"{backend}-chat",
                        "message": f"WSL backend launch failed ({exc}); falling back to Windows backend",
                        "fallback": True,
                    },
                )
        local_command = _agent_chat_command(
            run_id,
            target,
            backend=backend,
            aha_home=root,
            codex_bin=codex_bin,
            claude_bin=claude_bin,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            approval=approval,
            interval=interval,
            from_start=from_start,
            no_json=no_json,
            extra_args=extra_args,
            prompt_prefix=prompt_prefix,
            task_id=task_id,
            wsl_target=None,
        )
        return launch(local_command, None)


def _stop_wsl_backend_process(
    run_id: str,
    target: str,
    task_id: str | None,
    state: dict,
    *,
    timeout: float = 5.0,
) -> None:
    """Best-effort termination of a WSL backend's distro-side process tree.

    A WSL backend's watcher runs inside the distro (``python3 ... <backend>-chat
    <run_id> <target>``), outside the Windows process tree that ``wsl.exe``
    belongs to. Killing ``wsl.exe`` does not stop it, leaving an orphan that
    holds the backend lock and keeps calling the API. When the state records a
    WSL distro, run ``pkill`` inside that distro matching the backend's exact
    command signature. The first token uses a character-class regex
    (``[c]laude-chat``) so the pkill command line itself does not match.
    """
    distro = str(state.get("wsl_distro") or "").strip()
    if not distro:
        return
    backend = str(state.get("backend") or "").removesuffix("-chat")
    if backend not in ("codex", "claude"):
        return
    # Pattern that only matches the backend watcher, not this pkill command:
    #   <b>ackend-chat <run_id> <target> [--task-id <task_id>]
    pattern = f"[{backend[0]}]{backend[1:]}-chat {run_id} {target}"
    if task_id:
        pattern += f" --task-id {task_id}"
    script = (
        "pids=$(pgrep -f -- '" + pattern + "' 2>/dev/null | grep -v $$ || true); "
        "if [ -n \"$pids\" ]; then "
        "kill -TERM $pids 2>/dev/null || true; sleep 0.5; "
        "kill -KILL $pids 2>/dev/null || true; fi"
    )
    try:
        from aha_cli.services.wsl_backend import _wsl_executable
        import subprocess as _sp

        _sp.run(
            [_wsl_executable(), "-d", distro, "--", "bash", "-c", script],
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=int(getattr(_sp, "CREATE_NO_WINDOW", 0)),
        )
    except Exception:
        # Best-effort cleanup; a failure here must not break stop_backend.
        pass


def _wait_for_worker_stop(root: Path, run_id: str, target: str, task_id: str | None, *, timeout: float = 5.0) -> None:
    """Poll until no matching backend worker remains, bounded by ``timeout``.

    The recorded ``pid`` is the wsl.exe host; the worker lives inside the
    distro. After the pkill in ``_stop_wsl_backend_process``, wait for the
    worker (discovered via /proc) to disappear so a restart does not collide
    with an orphan still holding the consumer lock. When /proc is unavailable
    (Windows host) this is a no-op.
    """
    if not Path("/proc").is_dir():
        return
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if _discover_backend_process(root, run_id, target, task_id) is None:
            return
        time.sleep(0.1)


def stop_backend(root: Path, run_id: str, target: str = "main", *, task_id: str | None = None, timeout: float = 5.0) -> dict:
    task_id = task_id or None
    target = target or "main"
    with locked_backend(root, run_id, target, task_id):
        current = backend_status(root, run_id, target, task_id)
        pid = current.get("pid")
        if not pid or current["status"] == "stopped":
            current["already_stopped"] = True
            return current
        try:
            pgid = process_control.process_group_id(int(pid))
            if pgid == int(pid):
                process_control.signal_process_group(pgid, signal.SIGTERM)
            else:
                process_control.send_signal(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not pid_is_running(int(pid)):
                break
            time.sleep(0.1)
        if pid_is_running(int(pid)):
            try:
                pgid = process_control.process_group_id(int(pid))
                if pgid == int(pid):
                    process_control.signal_process_group(pgid, signal.SIGKILL)
                else:
                    process_control.send_signal(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        # A WSL backend runs inside the distro, outside the Windows process
        # tree. Killing wsl.exe (the Windows host) does not terminate the WSL
        # python/claude process, which would be left as an orphan still holding
        # locks and consuming quota. Explicitly pkill the WSL-side process by
        # its command-line signature.
        state = _read_state(root, run_id, target, task_id)
        _stop_wsl_backend_process(run_id, target, task_id, state, timeout=timeout)
        # The pkill above is fire-and-forget; give the distro worker a moment to
        # actually exit so a re-start does not collide with an orphan still holding
        # the consumer lock. When /proc is unavailable (Windows host) this falls
        # back to no-op and relies on the pkill alone.
        _wait_for_worker_stop(root, run_id, target, task_id, timeout=timeout)
        state.update(
            {
                "target": target,
                "task_id": task_id,
                "backend": _backend_name_from_state(state),
                "status": "stopped",
                "pid": pid,
                "managed": bool(state),
                "stopped_at": utc_now(),
                "log_path": state.get("log_path") or str(backend_log_path(root, run_id, target, task_id)),
                "command": state.get("command", []),
            }
        )
        _write_state(root, run_id, target, state, task_id)
        append_event(root, run_id, "backend_stopped", {"target": target, "task_id": task_id, "pid": pid})
        return backend_status(root, run_id, target, task_id) | {"stopped": True}
