from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from aha_cli.backends.registry import normalize_model_selector, resolve_model
from aha_cli.backends.codex_litellm_bridge import start_litellm_responses_bridge
from aha_cli.domain.models import utc_now
from aha_cli.services.backend_paths import add_user_backend_paths
from aha_cli.services.output_artifacts import save_command_output_artifact
from aha_cli.services.proxy import apply_proxy_environment
from aha_cli.store.filesystem import append_event_to_file
from aha_cli.store.sessions import force_full_prompt_marker, set_force_full_prompt_next_turn

OUTPUT_TAIL_LIMIT = 1200
CONTEXT_OVERFLOW_MARKERS = (
    "context window",
    "context length",
    "out of room",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)
BACKEND_AUTO_CONTEXT_COMPACT_TYPE_MARKERS = ("compact", "compaction")
BACKEND_AUTO_CONTEXT_COMPACT_SCOPE_MARKERS = ("context", "conversation", "session", "thread", "window", "auto")
BACKEND_AUTO_CONTEXT_COMPACT_MESSAGE_MARKERS = (
    "auto compact",
    "auto-compact",
    "automatic compact",
    "context compact",
    "context compaction",
    "context was compact",
    "context window compact",
    "conversation compact",
    "conversation was compact",
    "conversation was summarized",
    "summarized the conversation",
)
CODEX_ENV_MODEL_PREFIX = "env:"
CODEX_PROVIDER_OVERRIDE_KEY = "_provider_override"
CODEX_PROVIDER_DEFAULT_WIRE_API = "responses"
CODEX_DISABLE_ENV_KEY = "_aha_disable_env"
CODEX_LITELLM_RESPONSES_BRIDGE_KEY = "_litellm_responses_bridge"
CODEX_ENV_GROUP_FIELDS = (
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_API_KEY",
    "CODEX_WIRE_API",
    "CODEX_ENV_KEY",
)
CODEX_CONFIG_ENV_ALIASES = {
    "api_key": "OPENAI_API_KEY",
    "auth_token": "OPENAI_API_KEY",
    "base_url": "OPENAI_BASE_URL",
    "api_url": "OPENAI_BASE_URL",
    "model": "OPENAI_MODEL",
    "wire_api": "CODEX_WIRE_API",
    "env_key": "CODEX_ENV_KEY",
    "anthropic_api_key": "OPENAI_API_KEY",
    "anthropic_auth_token": "OPENAI_API_KEY",
    "anthropic_base_url": "OPENAI_BASE_URL",
    "anthropic_model": "OPENAI_MODEL",
}


def tail_text(value: str, limit: int = OUTPUT_TAIL_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def is_context_overflow_message(message: object) -> bool:
    text = str(message or "").casefold()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def _compact_detection_values(event: dict, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = event.get(key)
        if value is not None:
            values.append(str(value).casefold())
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value is not None:
                values.append(str(value).casefold())
    return values


def is_backend_auto_context_compact_event(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    typed_values = _compact_detection_values(event, ("type", "subtype", "event", "name", "reason", "status"))
    for value in typed_values:
        has_compact = any(marker in value for marker in BACKEND_AUTO_CONTEXT_COMPACT_TYPE_MARKERS)
        if has_compact and (
            any(marker in value for marker in BACKEND_AUTO_CONTEXT_COMPACT_SCOPE_MARKERS)
            or value in {"compact", "compacted", "compaction"}
        ):
            return True
    raw_type = str(event.get("type") or "").casefold()
    if raw_type in {"system", "status", "progress", "event"} or any("compact" in value for value in typed_values):
        for value in _compact_detection_values(event, ("message", "detail", "details", "description")):
            if any(marker in value for marker in BACKEND_AUTO_CONTEXT_COMPACT_MESSAGE_MARKERS):
                return True
            if "context" in value and ("summarized" in value or "summary" in value) and "conversation" in value:
                return True
    return False


def mark_backend_auto_context_compact(session: dict | None, event: dict) -> dict:
    raw_type = str(event.get("type") or "").strip()
    subtype = str(event.get("subtype") or event.get("event") or event.get("name") or "").strip()
    if session is not None:
        return set_force_full_prompt_next_turn(
            session,
            "backend_auto_context_compact",
            raw_type=raw_type,
            subtype=subtype,
        )
    return force_full_prompt_marker(
        "backend_auto_context_compact",
        raw_type=raw_type,
        subtype=subtype,
    )


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


def codex_config_env(codex_config: dict | None) -> dict[str, str]:
    group = codex_active_env_group(codex_config)
    if not group:
        return {}
    api_key = _codex_group_value(group, "OPENAI_API_KEY")
    env_key = _codex_group_value(group, "CODEX_ENV_KEY") or "OPENAI_API_KEY"
    env: dict[str, str] = {}
    if api_key:
        env[env_key] = api_key
        if env_key != "OPENAI_API_KEY":
            env["OPENAI_API_KEY"] = api_key
    return env


def codex_config_for_model(codex_config: dict | None, model: str | None) -> dict:
    cfg = dict(codex_config or {})
    if cfg.get(CODEX_PROVIDER_OVERRIDE_KEY):
        return cfg
    model = normalize_model_selector("codex", model, {"codex": cfg})
    env_group = codex_env_group_from_model(model)
    if env_group:
        cfg["env_active"] = env_group
        provider = codex_provider_override_from_env_group(cfg)
        if provider:
            cfg[CODEX_PROVIDER_OVERRIDE_KEY] = provider
    elif codex_cli_model(cfg, model):
        cfg["env_active"] = None
        cfg[CODEX_DISABLE_ENV_KEY] = True
    return cfg


def codex_cli_model(codex_config: dict | None, model: str | None) -> str | None:
    model = normalize_model_selector("codex", model, {"codex": codex_config or {}})
    if codex_env_group_from_model(model):
        group = codex_active_env_group(codex_config_for_model(codex_config, model))
        group_model = _codex_group_value(group, "OPENAI_MODEL")
        return group_model or None
    value = str(model or "").strip()
    return value or None


def codex_resolved_model(codex_config: dict | None, model: str | None) -> str | None:
    return resolve_model("codex", codex_cli_model(codex_config, model))


def apply_codex_environment(env: dict[str, str], codex_config: dict | None = None) -> dict[str, str]:
    env.update(codex_config_env(codex_config))
    return env


def _normalize_codex_env_key(key: str) -> str | None:
    cleaned = key.strip()
    if not cleaned:
        return None
    alias = CODEX_CONFIG_ENV_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    upper = cleaned.upper()
    if upper in CODEX_ENV_GROUP_FIELDS or upper.startswith(("OPENAI_", "CODEX_")):
        return upper
    if upper.startswith("ANTHROPIC_"):
        return CODEX_CONFIG_ENV_ALIASES.get(upper.lower())
    return None


def _codex_group_value(group: dict | None, canonical_key: str) -> str:
    if not isinstance(group, dict):
        return ""
    for raw_key, raw_value in group.items():
        if _normalize_codex_env_key(str(raw_key)) == canonical_key:
            value = str(raw_value or "").strip()
            if value:
                return value
    return ""


def _codex_responses_base_url(base_url: str) -> str:
    value = str(base_url or "").strip()
    trimmed = value.rstrip("/")
    match = re.fullmatch(r"(https?://api\.minimax(?:i\.com|\.io))/anthropic", trimmed)
    if match:
        return f"{match.group(1)}/v1"
    return value


def _kimi_coding_base_url(base_url: str) -> str:
    value = str(base_url or "").strip()
    trimmed = value.rstrip("/")
    if re.fullmatch(r"https?://api\.kimi\.com/coding(?:/v1)?", trimmed):
        scheme = trimmed.split("://", 1)[0]
        return f"{scheme}://api.kimi.com/coding/v1"
    return ""


def _kimi_coding_upstream_model(model: str) -> str:
    value = str(model or "").strip()
    if value == "kimi-for-coding":
        return value
    if value.lower().startswith("kimi-"):
        return "kimi-for-coding"
    return value or "kimi-for-coding"


def _codex_litellm_responses_bridge(base_url: str, model: str) -> dict:
    kimi_base_url = _kimi_coding_base_url(base_url)
    if not kimi_base_url:
        return {}
    client_model = str(model or "").strip() or "kimi-for-coding"
    return {
        "provider": "kimi",
        "upstream_base_url": kimi_base_url,
        "upstream_model": _kimi_coding_upstream_model(client_model),
        "client_model": client_model,
        "display_name": f"{client_model} via AHA Kimi bridge",
        "context_window": 262144,
        "max_output_tokens": 32768,
    }


def _codex_env_groups(codex_config: dict | None) -> list[dict]:
    if not isinstance(codex_config, dict):
        return []
    configured = codex_config.get("env")
    if isinstance(configured, dict):
        return [configured]
    if isinstance(configured, list):
        return [item for item in configured if isinstance(item, dict)]
    return []


def codex_active_env_group(codex_config: dict | None) -> dict:
    if not isinstance(codex_config, dict) or codex_config.get(CODEX_DISABLE_ENV_KEY):
        return {}
    groups = _codex_env_groups(codex_config)
    if not groups:
        return {}
    active_configured = "env_active" in codex_config
    active = str(codex_config.get("env_active") or "").strip()
    if active_configured and not active:
        return {}
    selected = next((item for item in groups if active and str(item.get("name") or "").strip() == active), None)
    return selected or (groups[0] if not active_configured else {})


def _safe_codex_provider_id(name: str, base_url: str) -> str:
    seed = f"{name}\0{base_url}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower()).strip("_")
    if slug and not slug[0].isdigit():
        return f"aha_codex_{slug}_{digest}"[:63]
    return f"aha_codex_env_{digest}"


def codex_provider_override_from_env_group(codex_config: dict | None) -> dict:
    group = codex_active_env_group(codex_config)
    if not group:
        return {}
    raw_base_url = _codex_group_value(group, "OPENAI_BASE_URL")
    base_url = _codex_responses_base_url(raw_base_url)
    model = _codex_group_value(group, "OPENAI_MODEL")
    if not base_url or not model:
        return {}
    bridge = _codex_litellm_responses_bridge(raw_base_url, model)
    if bridge:
        base_url = bridge["upstream_base_url"]
    name = str(group.get("name") or model or "Codex provider").strip()
    env_key = _codex_group_value(group, "CODEX_ENV_KEY") or "OPENAI_API_KEY"
    provider = {
        "provider_id": _safe_codex_provider_id(name, base_url),
        "name": name,
        "base_url": base_url,
        "wire_api": _codex_group_value(group, "CODEX_WIRE_API") or CODEX_PROVIDER_DEFAULT_WIRE_API,
        "requires_openai_auth": False,
        "env_key": env_key,
    }
    if bridge:
        provider[CODEX_LITELLM_RESPONSES_BRIDGE_KEY] = bridge
    return provider


def _toml_string(value: str) -> str:
    return json.dumps(str(value))


def _toml_bool(value: object) -> str:
    return "true" if bool(value) else "false"


def _provider_override(codex_config: dict | None) -> dict:
    if not isinstance(codex_config, dict):
        return {}
    value = codex_config.get(CODEX_PROVIDER_OVERRIDE_KEY)
    return value if isinstance(value, dict) else {}


def _provider_litellm_bridge(codex_config: dict | None) -> dict:
    bridge = _provider_override(codex_config).get(CODEX_LITELLM_RESPONSES_BRIDGE_KEY)
    return bridge if isinstance(bridge, dict) else {}


def codex_litellm_responses_bridge_config(codex_config: dict | None, model: str | None = None) -> dict:
    cfg = codex_config_for_model(codex_config, model)
    return _provider_litellm_bridge(cfg)


def _codex_config_with_bridge_base_url(codex_config: dict | None, base_url: str) -> dict:
    cfg = dict(codex_config or {})
    provider = dict(_provider_override(cfg))
    provider["base_url"] = str(base_url or "").strip()
    cfg[CODEX_PROVIDER_OVERRIDE_KEY] = provider
    return cfg


def _codex_bridge_api_key(codex_config: dict | None, bridge: dict) -> str:
    env = codex_config_env(codex_config)
    env_key = str(_provider_override(codex_config).get("env_key") or "").strip()
    bridge_env_key = str(bridge.get("env_key") or "").strip()
    for key in (bridge_env_key, env_key, "OPENAI_API_KEY"):
        if key and env.get(key):
            return env[key]
    return ""


def codex_config_with_provider_override(
    codex_config: dict | None,
    *,
    provider_id: str,
    name: str,
    base_url: str,
    wire_api: str = CODEX_PROVIDER_DEFAULT_WIRE_API,
    requires_openai_auth: bool = False,
    env_key: str | None = None,
) -> dict:
    cfg = codex_config_for_model(codex_config, None)
    cfg[CODEX_PROVIDER_OVERRIDE_KEY] = {
        "provider_id": str(provider_id or "aha_provider").strip() or "aha_provider",
        "name": str(name or provider_id or "AHA provider").strip() or "AHA provider",
        "base_url": str(base_url or "").strip(),
        "wire_api": str(wire_api or CODEX_PROVIDER_DEFAULT_WIRE_API).strip() or CODEX_PROVIDER_DEFAULT_WIRE_API,
        "requires_openai_auth": bool(requires_openai_auth),
    }
    if env_key:
        cfg[CODEX_PROVIDER_OVERRIDE_KEY]["env_key"] = str(env_key).strip()
    return cfg


def codex_config_with_observe_provider_override(
    codex_config: dict | None,
    *,
    provider_id: str,
    name: str,
    base_url: str,
    wire_api: str = CODEX_PROVIDER_DEFAULT_WIRE_API,
    model: str | None = None,
) -> dict:
    cfg = codex_config_for_model(codex_config, model)
    original = dict(_provider_override(cfg))
    provider = {
        "provider_id": str(provider_id or "aha_observe").strip() or "aha_observe",
        "name": str(name or provider_id or "AHA Observe Proxy").strip() or "AHA Observe Proxy",
        "base_url": str(base_url or "").strip(),
        "wire_api": str(original.get("wire_api") or wire_api or CODEX_PROVIDER_DEFAULT_WIRE_API).strip() or CODEX_PROVIDER_DEFAULT_WIRE_API,
        "requires_openai_auth": bool(original.get("requires_openai_auth")) if original else True,
    }
    env_key = str(original.get("env_key") or "").strip()
    if env_key:
        provider["env_key"] = env_key
    cfg[CODEX_PROVIDER_OVERRIDE_KEY] = provider
    return cfg


def codex_config_overrides(codex_config: dict | None) -> list[str]:
    provider = _provider_override(codex_config)
    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        return []
    provider_id = str(provider.get("provider_id") or "aha_provider").strip() or "aha_provider"
    name = str(provider.get("name") or provider_id).strip() or provider_id
    wire_api = str(provider.get("wire_api") or CODEX_PROVIDER_DEFAULT_WIRE_API).strip() or CODEX_PROVIDER_DEFAULT_WIRE_API
    overrides = [
        "-c",
        f"model_provider={_toml_string(provider_id)}",
        "-c",
        f"model_providers.{provider_id}.name={_toml_string(name)}",
        "-c",
        f"model_providers.{provider_id}.base_url={_toml_string(base_url)}",
        "-c",
        f"model_providers.{provider_id}.wire_api={_toml_string(wire_api)}",
        "-c",
        f"model_providers.{provider_id}.requires_openai_auth={_toml_bool(provider.get('requires_openai_auth'))}",
    ]
    env_key = str(provider.get("env_key") or "").strip()
    if env_key:
        overrides.extend(["-c", f"model_providers.{provider_id}.env_key={_toml_string(env_key)}"])
    return overrides


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
    if is_backend_auto_context_compact_event(event):
        compact_marker = mark_backend_auto_context_compact(session, event)
        append_event_to_file(
            events_file,
            run_id,
            "backend_auto_context_compact",
            data | compact_marker,
        )
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
        backend_session_id = str(event.get("thread_id") or (session or {}).get("backend_session_id") or "").strip()
        if backend_session_id:
            data["backend_session_id"] = backend_session_id
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


def codex_callback_events(line: str) -> list[tuple[str, dict]]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []
    raw_type = event.get("type")
    events: list[tuple[str, dict]] = []
    if raw_type == "thread.started":
        events.append(("agent_thread", {"thread_id": event.get("thread_id")}))
    elif raw_type == "error":
        events.append(("agent_error", {"message": event.get("message", "")}))
    elif raw_type == "turn.completed":
        usage = event.get("usage", {})
        events.append(("agent_usage", {"usage": usage if isinstance(usage, dict) else {}}))
    elif raw_type in {"item.started", "item.completed"}:
        item = event.get("item", {})
        if not isinstance(item, dict):
            return events
        item_type = item.get("type")
        if item_type == "agent_message" and raw_type == "item.completed":
            events.append(("agent_message", {"text": item.get("text", "")}))
        elif item_type == "command_execution":
            data = {
                "command": item.get("command", ""),
                "status": item.get("status", ""),
                "exit_code": item.get("exit_code"),
            }
            if raw_type == "item.completed":
                output = str(item.get("aggregated_output") or "")
                data["output_chars"] = len(output)
                data["output_tail"] = tail_text(output)
                events.append(("agent_command_finished", data))
            else:
                events.append(("agent_command_started", data))
    elif raw_type == "response_item":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        payload_type = payload.get("type")
        if payload_type == "function_call":
            tool_name = str(payload.get("name") or "tool")
            args = str(payload.get("arguments") or "").strip()
            command = f"{tool_name} {args}".strip()
            events.append(("agent_command_started", {"tool_name": tool_name, "command": command, "status": "in_progress"}))
        elif payload_type == "function_call_output":
            output = str(payload.get("output") or "")
            events.append(("agent_command_finished", {"output_tail": tail_text(output), "output_chars": len(output)}))
        elif payload_type == "message":
            texts: list[str] = []
            for item in payload.get("content") or []:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("transcript")
                    if text:
                        texts.append(str(text))
            events.append(("agent_message", {"text": "\n".join(texts).strip()}))
        elif payload_type == "reasoning":
            events.append(("agent_activity", {"message": "Agent is reasoning"}))
    elif raw_type == "event_msg":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            events.append(("agent_usage", {"usage": info}))
    return events


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
    event_callback: Callable[[str, dict], None] | None = None,
    start_new_session: bool = False,
) -> tuple[int, str, dict | None]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    requested_model = session.get("requested_model") if session is not None and "requested_model" in session else model
    codex_config = codex_config_for_model(codex_config, model)
    bridge_runtime = None
    try:
        bridge_config = _provider_litellm_bridge(codex_config)
        if bridge_config:
            bridge_runtime = start_litellm_responses_bridge(
                bridge_config=bridge_config,
                api_key=_codex_bridge_api_key(codex_config, bridge_config),
            ).__enter__()
            codex_config = _codex_config_with_bridge_base_url(codex_config, bridge_runtime.base_url)
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
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=start_new_session,
        )
        if event_callback:
            event_callback(
                "backend_process_started",
                {"pid": process.pid, "process_group": process.pid if start_new_session else None},
            )
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass
        for raw_line in process.stdout:
            print(raw_line, end="", flush=True)
            line = raw_line.strip()
            handle_codex_event(
                line,
                events_file=events_file,
                run_id=run_id,
                task_id=task_id,
                source=source,
                target=target,
                session=session,
            )
            if event_callback:
                for event_type, event_data in codex_callback_events(line):
                    event_callback(event_type, event_data)
        exit_code = process.wait()
        final_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        return exit_code, final_text, session
    except OSError as exc:
        exit_code = 127 if isinstance(exc, FileNotFoundError) else 1
        binary = exc.filename or codex_bin
        message = f"Failed to start Codex backend command `{binary}`: {exc.strerror or exc}"
        append_event_to_file(
            events_file,
            run_id,
            "agent_error",
            {"source": source, "task_id": task_id, "target": target, "message": message, "reason": "backend_start_failed"},
        )
        output_file.write_text(message, encoding="utf-8")
        return exit_code, message, session
    finally:
        if bridge_runtime is not None:
            bridge_runtime.__exit__(None, None, None)


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
