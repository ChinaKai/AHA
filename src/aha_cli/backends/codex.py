from __future__ import annotations

import json
import os
from pathlib import Path
import hashlib
import re
import shlex
import subprocess
import sys

from aha_cli.backends.registry import normalize_model_selector, resolve_model
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_paths import add_user_backend_paths
from aha_cli.services.output_artifacts import save_command_output_artifact
from aha_cli.services.proxy import apply_proxy_environment
from aha_cli.store.filesystem import append_event_to_file

OUTPUT_TAIL_LIMIT = 1200
CONTEXT_OVERFLOW_MARKERS = (
    "context window",
    "context length",
    "out of room",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)
CODEX_CONFIG_ENV_ALIASES = {
    "api_key": "OPENAI_API_KEY",
    "base_url": "OPENAI_BASE_URL",
    "env_key": "CODEX_ENV_KEY",
    "model": "OPENAI_MODEL",
    "wire_api": "CODEX_WIRE_API",
}
CODEX_ENV_GROUP_FIELDS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "CODEX_WIRE_API", "CODEX_ENV_KEY")
CODEX_ENV_MODEL_PREFIX = "env:"
CODEX_PROVIDER_DEFAULT_WIRE_API = "responses"
CODEX_PROVIDER_WIRE_APIS = {"responses"}


def tail_text(value: str, limit: int = OUTPUT_TAIL_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def is_context_overflow_message(message: object) -> bool:
    text = str(message or "").casefold()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def codex_sandbox(mode: str, requested: str) -> str:
    if requested != "auto":
        return requested
    return "read-only" if mode == "research" else "workspace-write"


def codex_env_model_value(group_name: str) -> str:
    name = str(group_name or "").strip()
    return f"{CODEX_ENV_MODEL_PREFIX}{name}" if name else ""


def codex_env_group_from_model(model: str | None) -> str | None:
    value = str(model or "").strip()
    if not value.startswith(CODEX_ENV_MODEL_PREFIX):
        return None
    name = value[len(CODEX_ENV_MODEL_PREFIX) :].strip()
    return name or None


def _normalize_codex_env_key(key: str) -> str | None:
    cleaned = key.strip()
    if not cleaned:
        return None
    alias = CODEX_CONFIG_ENV_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    upper = cleaned.upper()
    if upper.startswith("OPENAI_"):
        return upper
    return None


def codex_config_env(codex_config: dict | None) -> dict[str, str]:
    group = codex_selected_env_group(codex_config)
    env: dict[str, str] = {}
    api_key = str(group.get("OPENAI_API_KEY") or group.get("api_key") or "").strip()
    if api_key:
        env[codex_provider_env_key(codex_config)] = api_key
    return env


def codex_selected_env_group(codex_config: dict | None) -> dict:
    if not isinstance(codex_config, dict):
        return {}
    configured = codex_config.get("env")
    if isinstance(configured, list):
        active_configured = "env_active" in codex_config
        active = str(codex_config.get("env_active") or "").strip()
        if active_configured and not active:
            configured = {}
        else:
            groups = [item for item in configured if isinstance(item, dict)]
            selected = next((item for item in groups if active and str(item.get("name") or "").strip() == active), None)
            selected_group = selected or (groups[0] if groups else {})
            configured = {"name": selected_group.get("name")}
            configured.update({key: selected_group.get(key) for key in CODEX_ENV_GROUP_FIELDS})
    elif not isinstance(configured, dict):
        configured = {}
    return configured


def codex_config_for_model(codex_config: dict | None, model: str | None) -> dict:
    cfg = dict(codex_config or {})
    model = normalize_model_selector("codex", model, {"codex": cfg})
    env_group = codex_env_group_from_model(model)
    if env_group:
        cfg["env_active"] = env_group
    elif str(model or "").strip():
        cfg["env_active"] = None
    return cfg


def codex_cli_model(codex_config: dict | None, model: str | None) -> str | None:
    model = normalize_model_selector("codex", model, {"codex": codex_config or {}})
    env_group = codex_env_group_from_model(model)
    if env_group:
        group = codex_selected_env_group(codex_config_for_model(codex_config, model))
        return str(group.get("OPENAI_MODEL") or group.get("model") or "").strip() or None
    value = str(model or "").strip()
    return value or None


def codex_resolved_model(codex_config: dict | None, model: str | None) -> str | None:
    return resolve_model("codex", codex_cli_model(codex_config, model))


def apply_codex_environment(env: dict[str, str], codex_config: dict | None = None) -> dict[str, str]:
    env.update(codex_config_env(codex_config))
    return env


def _toml_string(value: str) -> str:
    return json.dumps(str(value))


def codex_provider_id(codex_config: dict | None) -> str:
    group = codex_selected_env_group(codex_config)
    name = str(group.get("name") or "custom").strip()
    base_url = str(group.get("OPENAI_BASE_URL") or group.get("base_url") or "").strip()
    digest = hashlib.sha256(f"{name}\0{base_url}".encode("utf-8")).hexdigest()[:10]
    return f"aha_codex_env_{digest}"


def codex_provider_wire_api(codex_config: dict | None) -> str:
    group = codex_selected_env_group(codex_config)
    value = str(group.get("CODEX_WIRE_API") or group.get("wire_api") or CODEX_PROVIDER_DEFAULT_WIRE_API).strip().lower()
    return value if value in CODEX_PROVIDER_WIRE_APIS else CODEX_PROVIDER_DEFAULT_WIRE_API


def codex_provider_env_key(codex_config: dict | None) -> str:
    group = codex_selected_env_group(codex_config)
    value = str(group.get("CODEX_ENV_KEY") or group.get("env_key") or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
    value = value.upper()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        return "OPENAI_API_KEY"
    return value


def codex_config_overrides(codex_config: dict | None) -> list[str]:
    group = codex_selected_env_group(codex_config)
    base_url = str(group.get("OPENAI_BASE_URL") or group.get("base_url") or "").strip()
    api_key = str(group.get("OPENAI_API_KEY") or group.get("api_key") or "").strip()
    if not base_url:
        return []
    provider_id = codex_provider_id(codex_config)
    name = str(group.get("name") or provider_id).strip() or provider_id
    env_key = codex_provider_env_key(codex_config)
    args = [
        "-c",
        f"model_provider={_toml_string(provider_id)}",
        "-c",
        f"model_providers.{provider_id}.name={_toml_string(name)}",
        "-c",
        f"model_providers.{provider_id}.base_url={_toml_string(base_url)}",
        "-c",
        f"model_providers.{provider_id}.wire_api={_toml_string(codex_provider_wire_api(codex_config))}",
        "-c",
        f"model_providers.{provider_id}.requires_openai_auth=false",
    ]
    if api_key:
        args.extend(
            [
                "-c",
                f"model_providers.{provider_id}.env_key={_toml_string(env_key)}",
            ]
        )
    return args


def handle_codex_event(
    line: str,
    *,
    events_file: Path | None,
    run_id: str,
    task_id: str | None,
    source: str,
    target: str | None = None,
    session: dict | None = None,
) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    raw_type = event.get("type")
    data: dict = {"source": source, "raw_type": raw_type}
    if task_id:
        data["task_id"] = task_id
    if target:
        data["target"] = target
    if raw_type == "thread.started":
        thread_id = event.get("thread_id")
        data["thread_id"] = thread_id
        if session is not None and thread_id:
            if not session.get("backend_session_id"):
                session["backend_session_id"] = thread_id
            if session.get("backend_session_id") == thread_id and session.get("status") == "reset":
                session["status"] = "active"
                session["updated_at"] = utc_now()
        append_event_to_file(events_file, run_id, "agent_thread", data)
    elif raw_type == "error":
        data["message"] = event.get("message", "")
        append_event_to_file(events_file, run_id, "agent_error", data)
        if is_context_overflow_message(data["message"]):
            append_event_to_file(events_file, run_id, "agent_context_overflow", data | {"reason": "context_window"})
    elif raw_type == "turn.completed":
        usage = event.get("usage", {})
        data["usage"] = usage if isinstance(usage, dict) else {}
        append_event_to_file(events_file, run_id, "agent_usage", data)
    elif raw_type in {"item.started", "item.completed"}:
        item = event.get("item", {})
        if not isinstance(item, dict):
            return
        data["item_type"] = item.get("type")
        if item.get("type") == "agent_message" and raw_type == "item.completed":
            data["text"] = item.get("text", "")
            append_event_to_file(events_file, run_id, "agent_message", data)
        elif item.get("type") == "command_execution":
            data["command"] = item.get("command", "")
            data["status"] = item.get("status", "")
            data["exit_code"] = item.get("exit_code")
            if raw_type == "item.completed":
                output = str(item.get("aggregated_output") or "")
                data["output_tail"] = tail_text(output)
                data["output_chars"] = len(output)
                if len(output) > OUTPUT_TAIL_LIMIT:
                    output_ref = save_command_output_artifact(
                        events_file,
                        task_id=task_id,
                        target=target,
                        output=output,
                    )
                    if output_ref:
                        data["output_ref"] = output_ref
            append_event_to_file(
                events_file,
                run_id,
                "agent_command_started" if raw_type == "item.started" else "agent_command_finished",
                data,
            )


def run_codex_exec(
    prompt: str,
    *,
    cwd: Path,
    output_file: Path,
    codex_bin: str = "codex",
    model: str | None = None,
    sandbox: str = "read-only",
    approval: str = "never",
    json_events: bool = True,
    extra_args: list[str] | None = None,
    events_file: Path | None = None,
    run_id: str = "",
    task_id: str | None = None,
    source: str = "codex",
    target: str | None = None,
    session: dict | None = None,
    proxy_env: dict[str, str] | None = None,
    codex_config: dict | None = None,
) -> tuple[int, str, dict | None]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    requested_model = session.get("requested_model") if session is not None and "requested_model" in session else model
    codex_config = codex_config_for_model(codex_config, model)
    model = codex_resolved_model(codex_config, model)
    if session is not None:
        session["requested_model"] = requested_model
        session["resolved_model"] = model
        session["model"] = model
    session_id = session.get("backend_session_id") if session else None
    cmd = build_codex_exec_command(
        codex_bin=codex_bin,
        model=model,
        approval=approval,
        sandbox=sandbox,
        cwd=cwd,
        output_file=output_file,
        json_events=json_events,
        session_id=session_id,
        config_overrides=codex_config_overrides(codex_config),
    )
    for raw in extra_args or []:
        insert_at = -1 if cmd[-1] == "-" else len(cmd)
        for part in shlex.split(raw):
            cmd.insert(insert_at, part)
            insert_at += 1

    env = os.environ.copy()
    add_user_backend_paths(env)
    apply_codex_environment(env, codex_config)
    apply_proxy_environment(env, proxy_env)

    print(f"Running Codex backend: {' '.join(shlex.quote(part) for part in cmd[:-1])} -", flush=True)
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
        message = f"Failed to start Codex backend command `{binary}`: {exc.strerror or exc}"
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
    for raw_line in process.stdout:
        print(raw_line, end="", flush=True)
        handle_codex_event(
            raw_line.strip(),
            events_file=events_file,
            run_id=run_id,
            task_id=task_id,
            source=source,
            target=target,
            session=session,
        )
    exit_code = process.wait()
    final_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
    return exit_code, final_text, session


def build_codex_exec_command(
    *,
    codex_bin: str,
    model: str | None,
    approval: str | None,
    sandbox: str,
    cwd: Path,
    output_file: Path,
    json_events: bool,
    session_id: str | None,
    config_overrides: list[str] | None = None,
) -> list[str]:
    model = resolve_model("codex", model)
    cmd = [codex_bin]
    for item in config_overrides or []:
        cmd.append(item)
    if model:
        cmd.extend(["-m", model])
    if approval:
        cmd.extend(["-a", approval])

    cmd.extend(["exec", "--skip-git-repo-check", "--sandbox", sandbox, "-C", str(cwd)])
    if session_id:
        cmd.append("resume")
    if json_events:
        cmd.append("--json")
    cmd.extend(["-o", str(output_file)])
    if session_id:
        cmd.extend([session_id, "-"])
    else:
        cmd.append("-")
    return cmd


def codex_runner_command(args, cfg: dict) -> str:
    codex_cfg = cfg.get("codex", {})
    parts = [shlex.quote(sys.executable), "-m", "aha_cli", "codex-runner"]
    parts.extend(["--codex-bin", shlex.quote(args.codex_bin or codex_cfg.get("bin") or "codex")])
    model = resolve_model("codex", args.codex_model if args.codex_model is not None else codex_cfg.get("model"))
    if model:
        parts.extend(["--model", shlex.quote(model)])
    sandbox = args.codex_sandbox or codex_cfg.get("sandbox") or "auto"
    parts.extend(["--sandbox", shlex.quote(sandbox)])
    approval = args.codex_approval or codex_cfg.get("approval") or "never"
    parts.extend(["--approval", shlex.quote(approval)])
    if args.no_codex_json or not codex_cfg.get("json", True):
        parts.append("--no-json")
    for extra in args.codex_extra_arg or []:
        parts.extend(["--extra-arg", shlex.quote(extra)])
    return " ".join(parts)
