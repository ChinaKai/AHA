from __future__ import annotations

import json
import os
import re
import subprocess
import time

from aha_cli import platform
from aha_cli.backends.plugin import (
    BackendDescriptor,
    BackendResolvedTurn,
    BackendTurnRequest,
    BackendTurnResult,
    FunctionalBackendPlugin,
    agent_backend_plugins,
    backend_plugins,
    get_backend_plugin,
    maybe_backend_plugin,
    register_backend,
)
from aha_cli.services.backend_paths import add_user_backend_paths

CODEX_DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MODEL_OPTION = {"name": "", "label": "default"}
DEFAULT_REASONING_EFFORT_OPTION = {"name": "", "label": "default"}
CODEX_MODEL_CATALOG_TIMEOUT_SECONDS = 3.0
CODEX_MODEL_CATALOG_CACHE_TTL_SECONDS = 300.0

CODEX_FALLBACK_MODEL_NAMES = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
)
CODEX_FALLBACK_REASONING_EFFORT_NAMES = ("low", "medium", "high", "xhigh")
CLAUDE_REASONING_EFFORT_NAMES = ("low", "medium", "high", "xhigh", "max")
REASONING_EFFORT_NAMES = ("low", "medium", "high", "xhigh", "max", "ultra")
_CODEX_MODEL_OPTIONS_CACHE: dict[str, tuple[float, list[dict]]] = {}

DEFAULT_MODEL_OPTIONS = [DEFAULT_MODEL_OPTION]
CLAUDE_MODEL_OPTIONS = [
    {"name": "claude-opus-4-8", "label": "Claude Opus 4.8"},
    {"name": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    {"name": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
]
CODEX_AGENT_COMMANDS = [
    {"scope": "agent", "name": "/agent <command>", "insert": "/agent ", "desc": "Route a command to the selected agent."},
]
STUB_AGENT_COMMANDS = [
    {"scope": "agent", "name": "/agent <command>", "insert": "/agent ", "desc": "Route a command to the selected agent."},
]
CLAUDE_AGENT_COMMANDS = [
    {"scope": "agent", "name": "/agent <command>", "insert": "/agent ", "desc": "Route a command to the selected agent."},
]

BACKENDS = {
    "codex": {"name": "codex", "kind": "agent", "commands": CODEX_AGENT_COMMANDS, "native_commands": []},
    "claude": {"name": "claude", "kind": "agent", "models": CLAUDE_MODEL_OPTIONS, "commands": CLAUDE_AGENT_COMMANDS, "native_commands": []},
    "stub": {"name": "stub", "kind": "agent", "models": DEFAULT_MODEL_OPTIONS, "commands": STUB_AGENT_COMMANDS, "native_commands": []},
    "command": {"name": "command", "kind": "runner", "label": "Shell command runner", "models": DEFAULT_MODEL_OPTIONS},
}


def _copy_model_options(options: list[dict]) -> list[dict]:
    return [dict(option) for option in options]


def _codex_default_model_option() -> dict:
    return {
        "name": "",
        "label": f"default ({CODEX_DEFAULT_MODEL})",
        "reasoning_efforts": _reasoning_effort_options(CODEX_FALLBACK_REASONING_EFFORT_NAMES),
    }


def _codex_fallback_model_options() -> list[dict]:
    return [{"name": name, "label": name} for name in CODEX_FALLBACK_MODEL_NAMES]


def _reasoning_effort_options(names: tuple[str, ...] | list[str]) -> list[dict]:
    seen: set[str] = set()
    options = [dict(DEFAULT_REASONING_EFFORT_OPTION)]
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        options.append({"name": name, "label": name})
    return options


def _codex_reasoning_efforts_from_catalog_item(item: dict) -> list[str]:
    raw_levels = item.get("supported_reasoning_levels") or item.get("supported_reasoning_efforts") or []
    names: list[str] = []
    if isinstance(raw_levels, list):
        for level in raw_levels:
            if isinstance(level, dict):
                name = str(level.get("effort") or level.get("name") or level.get("level") or "").strip()
            else:
                name = str(level or "").strip()
            if name and name in REASONING_EFFORT_NAMES and name not in names:
                names.append(name)
    return names


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _codex_bin_from_config(config: dict | None = None) -> str:
    if not isinstance(config, dict):
        return "codex"
    section = config.get("codex")
    if not isinstance(section, dict):
        section = config
    return str(section.get("bin") or "codex").strip() or "codex"


def _codex_catalog_model_options_from_payload(payload: object) -> list[dict]:
    raw_models = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        return []

    seen: set[str] = set()
    sortable: list[tuple[int, int, dict]] = []
    for index, item in enumerate(raw_models):
        if not isinstance(item, dict):
            continue
        name = str(item.get("slug") or item.get("name") or item.get("id") or "").strip()
        if not name or name in seen:
            continue
        visibility = str(item.get("visibility") or "").strip().lower()
        if visibility and visibility != "list":
            continue
        label = str(item.get("display_name") or item.get("label") or name).strip() or name
        try:
            priority = int(item.get("priority"))
        except (TypeError, ValueError):
            priority = 1000 + index
        seen.add(name)
        option = {"name": name, "label": label}
        reasoning_efforts = _codex_reasoning_efforts_from_catalog_item(item)
        if reasoning_efforts:
            option["reasoning_efforts"] = _reasoning_effort_options(reasoning_efforts)
        default_reasoning = str(item.get("default_reasoning_level") or item.get("default_reasoning_effort") or "").strip()
        if default_reasoning in reasoning_efforts:
            option["default_reasoning_effort"] = default_reasoning
        sortable.append((priority, index, option))

    sortable.sort(key=lambda entry: (entry[0], entry[1]))
    return [option for _priority, _index, option in sortable]


def _load_codex_catalog_model_options(codex_bin: str) -> list[dict]:
    timeout = _float_env("AHA_CODEX_MODEL_CATALOG_TIMEOUT_SECONDS", CODEX_MODEL_CATALOG_TIMEOUT_SECONDS)
    env = dict(os.environ)
    add_user_backend_paths(env)
    commands = (
        [codex_bin, "debug", "models"],
        [codex_bin, "debug", "models", "--bundled"],
    )
    for command in commands:
        try:
            command = platform.spawn_command(command)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
                env=env,
                **platform.hidden_subprocess_kwargs(),
            )
        except Exception:
            continue
        if completed.returncode != 0:
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            continue
        options = _codex_catalog_model_options_from_payload(payload)
        if options:
            return options
    return []


def _codex_model_options(config: dict | None = None) -> list[dict]:
    codex_bin = _codex_bin_from_config(config)
    now = time.monotonic()
    ttl = _float_env("AHA_CODEX_MODEL_CATALOG_CACHE_TTL_SECONDS", CODEX_MODEL_CATALOG_CACHE_TTL_SECONDS)
    cached = _CODEX_MODEL_OPTIONS_CACHE.get(codex_bin)
    if cached and ttl and now - cached[0] < ttl:
        options = _copy_model_options(cached[1])
    else:
        options = _load_codex_catalog_model_options(codex_bin) or _codex_fallback_model_options()
        _CODEX_MODEL_OPTIONS_CACHE[codex_bin] = (now, _copy_model_options(options))
    return [_codex_default_model_option(), *_copy_model_options(options)]


def _backend_model_options(backend: str, config: dict | None = None) -> list[dict]:
    plugin = maybe_backend_plugin(backend)
    if plugin is not None:
        return plugin.model_options(config)
    if backend == "codex":
        return _codex_model_options(config)
    return _copy_model_options(BACKENDS.get(backend, {}).get("models", DEFAULT_MODEL_OPTIONS))


def _backend_reasoning_effort_options(backend: str) -> list[dict]:
    plugin = maybe_backend_plugin(backend)
    if plugin is not None:
        return plugin.reasoning_effort_options()
    if backend == "codex":
        return _reasoning_effort_options(CODEX_FALLBACK_REASONING_EFFORT_NAMES)
    if backend == "claude":
        return _reasoning_effort_options(CLAUDE_REASONING_EFFORT_NAMES)
    return [dict(DEFAULT_REASONING_EFFORT_OPTION)]


def _env_group_model_id(backend: str, group: dict) -> str:
    if backend == "codex":
        return str(group.get("OPENAI_MODEL") or group.get("ANTHROPIC_MODEL") or group.get("model") or "").strip()
    if backend == "claude":
        return str(group.get("ANTHROPIC_MODEL") or group.get("model") or "").strip()
    if backend == "opencode":
        return str(group.get("OPENCODE_MODEL") or group.get("model") or "").strip()
    return ""


def _model_capability_match_values(backend: str, model: str) -> list[str]:
    values: list[str] = []
    for candidate in _candidate_model_values(backend, model):
        for value in (candidate, candidate.rsplit("/", 1)[-1], re.sub(r"\[[^\]]+\]$", "", candidate).strip()):
            if value and value not in values:
                values.append(value)
    return values


def _matching_model_capabilities(backend: str, model: str, options: list[dict]) -> dict | None:
    for candidate in _model_capability_match_values(backend, model):
        candidate_key = _model_alias_key(candidate)
        for option in options:
            name = str(option.get("name") or "").strip()
            label = str(option.get("label") or "").strip()
            if not name:
                continue
            if candidate == name or candidate_key in {_model_alias_key(name), _model_alias_key(label)}:
                return option
    return None


def _backend_env_model_options(backend: str, config: dict | None, catalog_options: list[dict]) -> list[dict]:
    section = (config or {}).get(backend) if isinstance(config, dict) else {}
    groups = section.get("env") if isinstance(section, dict) else []
    if isinstance(groups, dict):
        groups = [groups]
    if not isinstance(groups, list):
        return []
    fallback_efforts = _backend_reasoning_effort_options(backend)
    options: list[dict] = []
    seen: set[str] = set()
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or f"env-{index + 1}").strip()
        if not name:
            continue
        selector = f"env:{name}"
        if selector in seen:
            continue
        seen.add(selector)
        model = _env_group_model_id(backend, group)
        matched = _matching_model_capabilities(backend, model, catalog_options) if model else None
        reasoning_efforts = matched.get("reasoning_efforts") if isinstance(matched, dict) else None
        option = {
            "name": selector,
            "label": f"{model or 'not configured'} ({name})",
            "source": "env",
            "resolved_model": model,
            "reasoning_effort_source": "model_catalog" if reasoning_efforts else "backend_fallback",
            "reasoning_efforts": _copy_model_options(reasoning_efforts or fallback_efforts),
        }
        default_reasoning = str((matched or {}).get("default_reasoning_effort") or "").strip()
        if default_reasoning:
            option["default_reasoning_effort"] = default_reasoning
        options.append(option)
    return options


def reasoning_effort_options(backend: str = "codex") -> list[dict]:
    return _backend_reasoning_effort_options(backend)


def _normalize_reasoning_effort_for_names(
    value: object,
    names: tuple[str, ...] | list[str],
) -> str | None:
    effort = str(value or "").strip().lower()
    if not effort or effort in {"default", "none", "null"}:
        return None
    if effort not in names:
        raise ValueError(f"unknown reasoning effort: {value}")
    return effort


def normalize_reasoning_effort(value: object, backend: str | None = None) -> str | None:
    plugin = maybe_backend_plugin(str(backend or ""))
    if plugin is not None:
        return plugin.normalize_reasoning_effort(value)
    allowed = CLAUDE_REASONING_EFFORT_NAMES if backend == "claude" else REASONING_EFFORT_NAMES
    return _normalize_reasoning_effort_for_names(value, allowed)


def resolve_model(backend: str, model: str | None) -> str | None:
    plugin = maybe_backend_plugin(backend)
    if plugin is not None:
        return plugin.resolve_model(model)
    normalized = str(model or "").strip()
    if backend == "codex" and normalized in {"", "default"}:
        return CODEX_DEFAULT_MODEL
    return normalized or None


def _model_alias_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _candidate_model_values(backend: str, model: str) -> list[str]:
    values = [model]
    lowered = model.lower()
    prefix = f"{backend.lower()}-"
    if lowered.startswith(prefix):
        values.append(model[len(prefix) :])
    return values


def _configured_env_group_names(backend: str, config: dict | None) -> list[str]:
    section = (config or {}).get(backend) if isinstance(config, dict) else {}
    groups = section.get("env") if isinstance(section, dict) else []
    if not isinstance(groups, list):
        return []
    names: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _matching_env_group_name(backend: str, model: str, config: dict | None) -> str | None:
    names = _configured_env_group_names(backend, config)
    if not names:
        return None
    for candidate in _candidate_model_values(backend, model):
        lowered = candidate.lower()
        key = _model_alias_key(candidate)
        for name in names:
            if lowered == name.lower() or key == _model_alias_key(name):
                return name
        fuzzy = [name for name in names if key and key in _model_alias_key(name)]
        if len(fuzzy) == 1:
            return fuzzy[0]
    return None


def _normalize_model_selector_legacy(
    backend: str,
    model: object,
    config: dict | None = None,
) -> str | None:
    raw = str(model or "").strip()
    if not raw or raw.lower() == "default":
        return None
    if raw.lower().startswith("env:"):
        env_name = raw.split(":", 1)[1].strip()
        matched = _matching_env_group_name(backend, env_name, config)
        return f"env:{matched}" if matched else raw

    for candidate in _candidate_model_values(backend, raw):
        key = _model_alias_key(candidate)
        for option in model_options(backend, config):
            name = str(option.get("name") or "").strip()
            label = str(option.get("label") or "").strip()
            if not name:
                continue
            if candidate == name or key in {_model_alias_key(name), _model_alias_key(label)}:
                return name
        matched = _matching_env_group_name(backend, candidate, config)
        if matched:
            return f"env:{matched}"
    return raw


def normalize_model_selector(backend: str, model: object, config: dict | None = None) -> str | None:
    plugin = maybe_backend_plugin(backend)
    if plugin is not None:
        return plugin.normalize_model_selector(model, config)
    return _normalize_model_selector_legacy(backend, model, config)


def backend_names() -> list[str]:
    return sorted(plugin.descriptor.name for plugin in backend_plugins())


def agent_backend_names() -> list[str]:
    return [plugin.descriptor.name for plugin in agent_backend_plugins()]


def agent_backends(config: dict | None = None) -> list[dict]:
    result: list[dict] = []
    for plugin in agent_backend_plugins():
        descriptor = plugin.descriptor
        name = descriptor.name
        catalog_models = plugin.model_options(config)
        result.append(
            {
                "name": name,
                "models": [*catalog_models, *_backend_env_model_options(name, config, catalog_models)],
                "reasoning_efforts": plugin.reasoning_effort_options(),
                "commands": [dict(command) for command in descriptor.commands],
                "native_commands": [dict(command) for command in descriptor.native_commands],
            }
        )
    return result


def agent_commands(backend: str = "codex") -> list[dict]:
    plugin = maybe_backend_plugin(backend)
    return [dict(command) for command in plugin.descriptor.commands] if plugin else []


def model_options(backend: str = "codex", config: dict | None = None) -> list[dict]:
    return _backend_model_options(backend, config)


def ensure_agent_backend(name: str) -> str:
    if name not in agent_backend_names():
        raise SystemExit(f"Unknown agent backend: {name}")
    return name


def agent_backend_or_default(name: str | None, default: str = "codex") -> str:
    return name if name in agent_backend_names() else default


def require_backend(name: str) -> dict:
    plugin = maybe_backend_plugin(name)
    if plugin is None:
        raise SystemExit(f"Unknown backend: {name}")
    descriptor = plugin.descriptor
    backend = {
        "name": descriptor.name,
        "kind": descriptor.kind,
        "label": descriptor.label,
        "commands": [dict(command) for command in descriptor.commands],
        "native_commands": [dict(command) for command in descriptor.native_commands],
    }
    backend["models"] = plugin.model_options()
    return backend


def _run_codex_plugin_turn(request: BackendTurnRequest) -> BackendTurnResult:
    from aha_cli.backends.codex import run_codex_exec

    runner = request.resolved.extras.get("turn_runner")
    if not callable(runner):
        runner = run_codex_exec
    kwargs = {
        "cwd": request.cwd,
        "output_file": request.output_file,
        "codex_bin": request.binary,
        "model": request.resolved.extras.get("execution_model")
        or request.resolved.command_model,
        "sandbox": request.sandbox,
        "approval": request.approval,
        "json_events": request.json_events,
        "reasoning_effort": request.resolved.reasoning_effort,
        "extra_args": request.extra_args,
        "events_file": request.events_file,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "source": request.source,
        "target": request.target,
        "session": request.session,
        "proxy_env": request.resolved.proxy_env,
        "codex_config": request.resolved.backend_config,
        "aha_home": request.aha_home,
        "config": request.config,
    }
    if request.event_callback is not None:
        kwargs["event_callback"] = request.event_callback
    exit_code, reply, session = runner(request.prompt, **kwargs)
    return BackendTurnResult(exit_code=exit_code, reply=reply, session=session)


def _run_claude_plugin_turn(request: BackendTurnRequest) -> BackendTurnResult:
    from aha_cli.backends.claude import run_claude_exec

    runner = request.resolved.extras.get("turn_runner")
    if not callable(runner):
        runner = run_claude_exec
    kwargs = {
        "cwd": request.cwd,
        "output_file": request.output_file,
        "claude_bin": request.binary,
        "model": request.resolved.command_model,
        "permission_mode": str(
            request.resolved.extras.get("permission_mode")
            or request.resolved.backend_config.get("permission_mode")
            or "plan"
        ),
        "reasoning_effort": request.resolved.reasoning_effort,
        "extra_args": request.extra_args,
        "events_file": request.events_file,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "source": request.source,
        "target": request.target,
        "session": request.session,
        "proxy_env": request.resolved.proxy_env,
        "claude_config": request.resolved.backend_config,
    }
    if request.event_callback is not None:
        kwargs["event_callback"] = request.event_callback
    exit_code, reply, session = runner(request.prompt, **kwargs)
    return BackendTurnResult(exit_code=exit_code, reply=reply, session=session)


def _codex_runner_plugin_command(args, config: dict) -> str:
    from aha_cli.backends.codex import codex_runner_command

    return codex_runner_command(args, config)


def _claude_runner_plugin_command(args, config: dict) -> str:
    from aha_cli.backends.claude import claude_runner_command

    return claude_runner_command(args, config)


def _opencode_model_plugin_options(config: dict | None) -> list[dict]:
    from aha_cli.backends.opencode import opencode_model_options

    return opencode_model_options(config)


def _opencode_reasoning_plugin_options() -> list[dict]:
    from aha_cli.backends.opencode import OPENCODE_REASONING_EFFORTS

    return _reasoning_effort_options(OPENCODE_REASONING_EFFORTS)


def _normalize_opencode_plugin_reasoning(value: object) -> str | None:
    from aha_cli.backends.opencode import normalize_opencode_reasoning_effort

    return normalize_opencode_reasoning_effort(value)


def _resolve_opencode_plugin_turn(**kwargs) -> BackendResolvedTurn:
    from aha_cli.backends.opencode import resolve_opencode_turn

    return resolve_opencode_turn(**kwargs)


def _run_opencode_plugin_turn(request: BackendTurnRequest) -> BackendTurnResult:
    from aha_cli.backends.opencode import run_opencode_turn

    return run_opencode_turn(request)


def _normalize_opencode_plugin_usage(usage: dict) -> dict:
    from aha_cli.backends.opencode import normalize_opencode_usage

    return normalize_opencode_usage(usage)


def _opencode_plugin_session_artifact_info(**kwargs) -> dict:
    from aha_cli.backends.opencode import opencode_session_artifact_info

    return opencode_session_artifact_info(**kwargs)


def _resolve_codex_plugin_turn(
    *,
    config: dict,
    model: str | None,
    reasoning_effort: str | None,
    task_scoped: bool,
    session: dict | None = None,
    requested_model_override: str | None = None,
    requested_model_override_set: bool = False,
) -> BackendResolvedTurn:
    from aha_cli.backends.codex import (
        codex_cli_model,
        codex_config_for_model,
        codex_resolved_model,
    )

    codex_cfg = config.get("codex") if isinstance(config.get("codex"), dict) else {}
    configured_model = model
    if not configured_model:
        configured_model = CODEX_DEFAULT_MODEL if task_scoped else codex_cfg.get("model")
    normalized_model = _normalize_model_selector_legacy(
        "codex",
        configured_model or (session or {}).get("model"),
        config,
    )
    requested_model = requested_model_override
    if not requested_model_override_set:
        requested_model = (
            configured_model
            if configured_model is not None
            else (session or {}).get("requested_model", normalized_model)
        )
    backend_config = codex_config_for_model(codex_cfg, normalized_model)
    command_model = codex_cli_model(backend_config, normalized_model)
    resolved_model = codex_resolved_model(backend_config, normalized_model)
    return BackendResolvedTurn(
        requested_model=requested_model,
        command_model=command_model,
        resolved_model=resolved_model,
        reasoning_effort=reasoning_effort,
        backend_config=backend_config,
        extras={
            "configured_model": configured_model,
            "normalized_model": normalized_model,
            "execution_model": normalized_model,
        },
    )


def _resolve_claude_plugin_turn(
    *,
    config: dict,
    model: str | None,
    reasoning_effort: str | None,
    task_scoped: bool,
    session: dict | None = None,
    requested_model_override: str | None = None,
    requested_model_override_set: bool = False,
) -> BackendResolvedTurn:
    from aha_cli.backends.claude import (
        claude_cli_model,
        claude_config_for_model,
        claude_resolved_model,
    )

    del task_scoped
    claude_cfg = config.get("claude") if isinstance(config.get("claude"), dict) else {}
    configured_model = model or claude_cfg.get("model")
    normalized_model = _normalize_model_selector_legacy(
        "claude",
        configured_model or (session or {}).get("model"),
        config,
    )
    requested_model = requested_model_override
    if not requested_model_override_set:
        requested_model = (
            configured_model
            if configured_model is not None
            else (session or {}).get("requested_model", normalized_model)
        )
    backend_config = claude_config_for_model(claude_cfg, normalized_model)
    command_model = claude_cli_model(normalized_model, claude_cfg)
    resolved_model = claude_resolved_model(backend_config, normalized_model)
    return BackendResolvedTurn(
        requested_model=requested_model,
        command_model=command_model,
        resolved_model=resolved_model,
        reasoning_effort=reasoning_effort,
        backend_config=backend_config,
        extras={
            "configured_model": configured_model,
            "normalized_model": normalized_model,
        },
    )


def _register_builtin_plugins() -> None:
    if maybe_backend_plugin("codex") is not None:
        return
    common_agent_commands = tuple(dict(command) for command in CODEX_AGENT_COMMANDS)
    register_backend(
        FunctionalBackendPlugin(
            BackendDescriptor(
                name="codex",
                kind="agent",
                label="Codex",
                process_chat=True,
                chat_command="codex-chat",
                runner_command="codex-runner",
                config_key="codex",
                binary_option="--codex-bin",
                default_binary="codex",
                supports_sessions=True,
                supports_reasoning_effort=True,
                supports_runtime_context=True,
                supports_no_json=True,
                supports_requested_model_override=True,
                sandbox_equivalence="os-enforced",
                commands=common_agent_commands,
            ),
            model_options=_codex_model_options,
            reasoning_options=lambda: _reasoning_effort_options(
                CODEX_FALLBACK_REASONING_EFFORT_NAMES
            ),
            normalize_reasoning=lambda value: _normalize_reasoning_effort_for_names(
                value,
                REASONING_EFFORT_NAMES,
            ),
            normalize_model=lambda value, config: _normalize_model_selector_legacy(
                "codex",
                value,
                config,
            ),
            resolve_model=lambda model: (
                CODEX_DEFAULT_MODEL
                if str(model or "").strip() in {"", "default"}
                else str(model or "").strip() or None
            ),
            runner_command=_codex_runner_plugin_command,
            resolve_turn=_resolve_codex_plugin_turn,
            run_turn=_run_codex_plugin_turn,
        )
    )
    register_backend(
        FunctionalBackendPlugin(
            BackendDescriptor(
                name="claude",
                kind="agent",
                label="Claude",
                process_chat=True,
                chat_command="claude-chat",
                runner_command="claude-runner",
                config_key="claude",
                binary_option="--claude-bin",
                default_binary="claude",
                supports_sessions=True,
                supports_reasoning_effort=True,
                supports_runtime_context=True,
                sandbox_equivalence="tool-policy",
                commands=tuple(dict(command) for command in CLAUDE_AGENT_COMMANDS),
            ),
            model_options=lambda _config: _copy_model_options(CLAUDE_MODEL_OPTIONS),
            reasoning_options=lambda: _reasoning_effort_options(
                CLAUDE_REASONING_EFFORT_NAMES
            ),
            normalize_reasoning=lambda value: _normalize_reasoning_effort_for_names(
                value,
                CLAUDE_REASONING_EFFORT_NAMES,
            ),
            normalize_model=lambda value, config: _normalize_model_selector_legacy(
                "claude",
                value,
                config,
            ),
            runner_command=_claude_runner_plugin_command,
            resolve_turn=_resolve_claude_plugin_turn,
            run_turn=_run_claude_plugin_turn,
        )
    )
    register_backend(
        FunctionalBackendPlugin(
            BackendDescriptor(
                name="opencode",
                kind="agent",
                label="OpenCode (Experimental)",
                process_chat=True,
                chat_command="opencode-chat",
                config_key="opencode",
                binary_option="--opencode-bin",
                default_binary="opencode",
                supports_sessions=True,
                supports_reasoning_effort=True,
                supports_runtime_context=True,
                sandbox_equivalence="tool-policy",
                commands=common_agent_commands,
            ),
            model_options=_opencode_model_plugin_options,
            reasoning_options=_opencode_reasoning_plugin_options,
            normalize_reasoning=_normalize_opencode_plugin_reasoning,
            resolve_turn=_resolve_opencode_plugin_turn,
            run_turn=_run_opencode_plugin_turn,
            normalize_usage=_normalize_opencode_plugin_usage,
            session_artifact_info=_opencode_plugin_session_artifact_info,
        )
    )
    register_backend(
        FunctionalBackendPlugin(
            BackendDescriptor(
                name="stub",
                kind="agent",
                label="Stub",
                commands=tuple(dict(command) for command in STUB_AGENT_COMMANDS),
            ),
            model_options=lambda _config: _copy_model_options(DEFAULT_MODEL_OPTIONS),
        )
    )
    register_backend(
        FunctionalBackendPlugin(
            BackendDescriptor(
                name="command",
                kind="runner",
                label="Shell command runner",
            ),
            model_options=lambda _config: _copy_model_options(DEFAULT_MODEL_OPTIONS),
        )
    )


def _descriptor_metadata(plugin) -> dict:
    descriptor = plugin.descriptor
    return {
        "name": descriptor.name,
        "kind": descriptor.kind,
        **({"label": descriptor.label} if descriptor.label else {}),
        "commands": [dict(command) for command in descriptor.commands],
        "native_commands": [dict(command) for command in descriptor.native_commands],
    }


_register_builtin_plugins()
BACKENDS = {
    plugin.descriptor.name: _descriptor_metadata(plugin)
    for plugin in backend_plugins()
}
