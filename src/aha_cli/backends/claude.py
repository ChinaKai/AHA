from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from aha_cli.backends.codex import OUTPUT_TAIL_LIMIT, is_context_overflow_message, tail_text
from aha_cli.backends.registry import normalize_model_selector
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_paths import add_user_backend_paths
from aha_cli.services.native_subagents import (
    CLAUDE_NATIVE_SUBAGENT_TOOLS,
    text_claims_subagent_created,
)
from aha_cli.services.output_artifacts import save_command_output_artifact
from aha_cli.services.proxy import apply_proxy_environment
from aha_cli.store.filesystem import append_event_to_file

CLAUDE_AUTH_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CLAUDE_GLOBAL_READONLY_DIR = "/"
CLAUDE_NATIVE_CONTROL_PLANE_TOOLS = ("AskUserQuestion", "ExitPlanMode")
CLAUDE_CONFIG_ENV_ALIASES = {
    "api_key": "ANTHROPIC_API_KEY",
    "auth_token": "ANTHROPIC_AUTH_TOKEN",
    "base_url": "ANTHROPIC_BASE_URL",
    "model": "ANTHROPIC_MODEL",
    "small_fast_model": "ANTHROPIC_SMALL_FAST_MODEL",
}
CLAUDE_ENV_GROUP_FIELDS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY")
CLAUDE_ENV_MODEL_PREFIX = "env:"
CLAUDE_DISABLE_ENV_KEY = "_aha_disable_env"


def claude_env_model_value(group_name: str) -> str:
    name = str(group_name or "").strip()
    return f"{CLAUDE_ENV_MODEL_PREFIX}{name}" if name else ""


def claude_env_group_from_model(model: str | None) -> str | None:
    value = str(model or "").strip()
    if not value.startswith(CLAUDE_ENV_MODEL_PREFIX):
        return None
    name = value[len(CLAUDE_ENV_MODEL_PREFIX) :].strip()
    return name or None


def claude_cli_model(model: str | None, claude_config: dict | None = None) -> str | None:
    if claude_config is not None:
        model = normalize_model_selector("claude", model, {"claude": claude_config})
    if claude_env_group_from_model(model):
        return None
    value = str(model or "").strip()
    return value or None


def claude_config_for_model(claude_config: dict | None, model: str | None) -> dict:
    cfg = dict(claude_config or {})
    model = normalize_model_selector("claude", model, {"claude": cfg})
    env_group = claude_env_group_from_model(model)
    if env_group:
        cfg["env_active"] = env_group
    elif claude_cli_model(model):
        cfg["env_active"] = None
        cfg[CLAUDE_DISABLE_ENV_KEY] = True
    return cfg


def claude_resolved_model(claude_config: dict | None, model: str | None) -> str | None:
    model = normalize_model_selector("claude", model, {"claude": claude_config or {}})
    cli_model = claude_cli_model(model)
    if cli_model:
        return cli_model
    env = claude_config_env(claude_config_for_model(claude_config, model))
    return env.get("ANTHROPIC_MODEL") or None


def _normalize_claude_env_key(key: str) -> str | None:
    cleaned = key.strip()
    if not cleaned:
        return None
    alias = CLAUDE_CONFIG_ENV_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    upper = cleaned.upper()
    if upper.startswith(("ANTHROPIC_", "CLAUDE_")):
        return upper
    return None


def claude_config_env(claude_config: dict | None) -> dict[str, str]:
    if not isinstance(claude_config, dict):
        return {}
    if claude_config.get(CLAUDE_DISABLE_ENV_KEY):
        return {}
    configured = claude_config.get("env")
    if isinstance(configured, list):
        active_configured = "env_active" in claude_config
        active = str(claude_config.get("env_active") or "").strip()
        if active_configured and not active:
            configured = {}
        else:
            groups = [item for item in configured if isinstance(item, dict)]
            selected = next((item for item in groups if active and str(item.get("name") or "").strip() == active), None)
            configured = selected or (groups[0] if groups else {})
            configured = {key: configured.get(key) for key in CLAUDE_ENV_GROUP_FIELDS}
    elif not isinstance(configured, dict):
        configured = {}
    env: dict[str, str] = {}
    for raw_key, raw_value in configured.items():
        key = _normalize_claude_env_key(str(raw_key))
        value = str(raw_value or "").strip()
        if key and value:
            env[key] = value
    return env


def apply_claude_environment(env: dict[str, str], claude_config: dict | None = None) -> dict[str, str]:
    env.update(claude_config_env(claude_config))
    return env


def claude_auth_configured(env: dict[str, str]) -> bool:
    return any(str(env.get(key) or "").strip() for key in CLAUDE_AUTH_ENV_KEYS)


def claude_missing_auth_message() -> str:
    return (
        "Claude authentication is not configured for AHA. "
        "Set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN in the AHA process environment, "
        "or add it under claude.env in AHA config."
    )


def claude_disallowed_aha_tools_arg() -> str:
    return ",".join((*CLAUDE_NATIVE_SUBAGENT_TOOLS, *CLAUDE_NATIVE_CONTROL_PLANE_TOOLS))


def claude_permission_mode(mode: str, requested: str) -> str:
    if requested == "danger-full-access":
        return "bypassPermissions"
    if requested == "workspace-write":
        return "acceptEdits"
    if requested == "auto":
        return "plan" if mode == "research" else "acceptEdits"
    return "plan"


def _content_items(message: dict) -> list[dict]:
    content = message.get("content", [])
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _tool_command(item: dict) -> str:
    name = str(item.get("name") or "tool")
    tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
    if name == "Bash" and tool_input.get("command"):
        return str(tool_input.get("command"))
    if tool_input:
        return f"{name} {json.dumps(tool_input, ensure_ascii=False, sort_keys=True)}"
    return name


def handle_claude_event(
    line: str,
    *,
    events_file: Path | None,
    run_id: str,
    task_id: str | None,
    source: str,
    target: str | None = None,
    session: dict | None = None,
) -> dict:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {}
    raw_type = event.get("type")
    data: dict = {"source": source, "raw_type": raw_type}
    result: dict = {}
    if task_id:
        data["task_id"] = task_id
    if target:
        data["target"] = target

    if raw_type == "system" and event.get("subtype") == "init":
        session_id = event.get("session_id")
        data["thread_id"] = session_id
        if session is not None and session_id:
            if not session.get("backend_session_id"):
                session["backend_session_id"] = session_id
            if session.get("backend_session_id") == session_id and session.get("status") == "reset":
                session["status"] = "active"
                session["updated_at"] = utc_now()
        append_event_to_file(events_file, run_id, "agent_thread", data)
    elif raw_type == "error":
        data["message"] = event.get("message") or event.get("error") or ""
        append_event_to_file(events_file, run_id, "agent_error", data)
        if is_context_overflow_message(data["message"]):
            append_event_to_file(events_file, run_id, "agent_context_overflow", data | {"reason": "context_window"})
    elif raw_type == "assistant":
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        for item in _content_items(message):
            item_type = item.get("type")
            if item_type == "text" and item.get("text"):
                text = str(item.get("text") or "")
                message_data = data | {"item_type": "agent_message", "text": text}
                append_event_to_file(events_file, run_id, "agent_message", message_data)
                result.setdefault("events", []).append(("agent_message", message_data))
                if target == "main" and text_claims_subagent_created(text):
                    append_event_to_file(
                        events_file,
                        run_id,
                        "claimed_sub_without_aha_agent",
                        data
                        | {
                            "text": text,
                            "reason": "assistant_text_claim_without_aha_spawn_sub",
                        },
                    )
                result.setdefault("assistant_texts", []).append(text)
            elif item_type == "tool_use":
                if str(item.get("name") or "") in CLAUDE_NATIVE_SUBAGENT_TOOLS:
                    append_event_to_file(
                        events_file,
                        run_id,
                        "native_subagent_tool_used",
                        data
                        | {
                            "item_type": "tool_use",
                            "tool_use_id": item.get("id"),
                            "tool_name": item.get("name"),
                            "command": _tool_command(item),
                            "reason": "native_subagent_tool_disabled_in_aha",
                        },
                    )
                append_event_to_file(
                    events_file,
                    run_id,
                    "agent_command_started",
                    command_data := data
                    | {
                        "item_type": "tool_use",
                        "tool_use_id": item.get("id"),
                        "tool_name": item.get("name"),
                        "command": _tool_command(item),
                        "status": "in_progress",
                        "exit_code": None,
                    },
                )
                result.setdefault("events", []).append(("agent_command_started", command_data))
    elif raw_type == "user":
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        for item in _content_items(message):
            if item.get("type") != "tool_result":
                continue
            content = item.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            is_error = bool(item.get("is_error"))
            output_ref = None
            if len(content) > OUTPUT_TAIL_LIMIT:
                output_ref = save_command_output_artifact(
                    events_file,
                    task_id=task_id,
                    target=target,
                    output=content,
                )
            append_event_to_file(
                events_file,
                run_id,
                "agent_command_finished",
                command_data := data
                | {
                    "item_type": "tool_result",
                    "tool_use_id": item.get("tool_use_id"),
                    "status": "failed" if is_error else "completed",
                    "exit_code": 1 if is_error else 0,
                    "output_tail": tail_text(content),
                    "output_chars": len(content),
                    **({"output_ref": output_ref} if output_ref else {}),
                },
            )
            result.setdefault("events", []).append(("agent_command_finished", command_data))
    elif raw_type == "result":
        session_id = event.get("session_id")
        if session is not None and session_id:
            if not session.get("backend_session_id"):
                session["backend_session_id"] = session_id
            if session.get("backend_session_id") == session_id and session.get("status") == "reset":
                session["status"] = "active"
                session["updated_at"] = utc_now()
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        for key in ("duration_ms", "duration_api_ms", "total_cost_usd", "num_turns", "subtype"):
            if key in event:
                usage[key] = event.get(key)
        append_event_to_file(events_file, run_id, "agent_usage", data | {"usage": usage})
        if event.get("result"):
            result["result_text"] = str(event.get("result") or "")
    return result


def run_claude_exec(
    prompt: str,
    *,
    cwd: Path,
    output_file: Path,
    claude_bin: str = "claude",
    model: str | None = None,
    permission_mode: str = "plan",
    extra_args: list[str] | None = None,
    events_file: Path | None = None,
    run_id: str = "",
    task_id: str | None = None,
    source: str = "claude",
    target: str | None = None,
    session: dict | None = None,
    proxy_env: dict[str, str] | None = None,
    claude_config: dict | None = None,
    event_callback: Callable[[str, dict], None] | None = None,
) -> tuple[int, str, dict | None]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    session_id = session.get("backend_session_id") if session else None
    cmd = build_claude_exec_command(
        claude_bin=claude_bin,
        model=model,
        permission_mode=permission_mode,
        session_id=session_id,
    )
    for raw in extra_args or []:
        cmd.extend(shlex.split(raw))

    config_env = claude_config_env(claude_config)
    env = os.environ.copy()
    add_user_backend_paths(env)
    env.update(config_env)
    apply_proxy_environment(env, proxy_env)
    if config_env and not claude_auth_configured(env):
        message = claude_missing_auth_message()
        append_event_to_file(
            events_file,
            run_id,
            "agent_error",
            {"source": source, "task_id": task_id, "target": target, "message": message},
        )
        output_file.write_text(message, encoding="utf-8")
        return 1, message, session

    print(f"Running Claude backend: {' '.join(shlex.quote(part) for part in cmd)}", flush=True)
    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
    except OSError as exc:
        exit_code = 127 if isinstance(exc, FileNotFoundError) else 1
        binary = exc.filename or cmd[0]
        message = f"Failed to start Claude backend command `{binary}`: {exc.strerror or exc}"
        append_event_to_file(
            events_file,
            run_id,
            "agent_error",
            {"source": source, "task_id": task_id, "target": target, "message": message, "reason": "backend_start_failed"},
        )
        output_file.write_text(message, encoding="utf-8")
        return exit_code, message, session
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass

    assistant_texts: list[str] = []
    result_text = ""
    for raw_line in process.stdout:
        print(raw_line, end="", flush=True)
        parsed = handle_claude_event(
            raw_line.strip(),
            events_file=events_file,
            run_id=run_id,
            task_id=task_id,
            source=source,
            target=target,
            session=session,
        )
        if event_callback:
            for event_type, event_data in parsed.get("events", []):
                event_callback(str(event_type), event_data)
        assistant_texts.extend(parsed.get("assistant_texts", []))
        if parsed.get("result_text"):
            result_text = str(parsed["result_text"])
    exit_code = process.wait()
    final_text = result_text or "\n".join(text for text in assistant_texts if text).strip()
    output_file.write_text(final_text, encoding="utf-8")
    return exit_code, final_text, session


def build_claude_exec_command(
    *,
    claude_bin: str,
    model: str | None,
    permission_mode: str,
    session_id: str | None,
) -> list[str]:
    cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose"]
    if model:
        cmd.extend(["--model", model])
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])
    if permission_mode == "plan":
        cmd.extend(["--add-dir", CLAUDE_GLOBAL_READONLY_DIR])
    cmd.extend(["--disallowedTools", claude_disallowed_aha_tools_arg()])
    if session_id:
        cmd.extend(["--resume", session_id])
    return cmd


def claude_runner_command(args, cfg: dict) -> str:
    claude_cfg = cfg.get("claude", {})
    parts = [shlex.quote(sys.executable), "-m", "aha_cli", "claude-runner"]
    parts.extend(["--claude-bin", shlex.quote(args.claude_bin or claude_cfg.get("bin") or "claude")])
    model = args.claude_model if args.claude_model is not None else claude_cfg.get("model")
    if model:
        parts.extend(["--model", shlex.quote(model)])
    sandbox = args.claude_sandbox or claude_cfg.get("sandbox") or "auto"
    parts.extend(["--sandbox", shlex.quote(sandbox)])
    permission_mode = args.claude_permission_mode if args.claude_permission_mode is not None else claude_cfg.get("permission_mode")
    if permission_mode:
        parts.extend(["--permission-mode", shlex.quote(permission_mode)])
    for extra in args.claude_extra_arg or []:
        parts.extend(["--extra-arg", shlex.quote(extra)])
    return " ".join(parts)
