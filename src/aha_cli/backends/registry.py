from __future__ import annotations

import json
import os
import re
import subprocess
import time

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
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout, env=env)
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
    if backend == "codex":
        return _codex_model_options(config)
    return _copy_model_options(BACKENDS.get(backend, {}).get("models", DEFAULT_MODEL_OPTIONS))


def _backend_reasoning_effort_options(backend: str) -> list[dict]:
    if backend == "codex":
        return _reasoning_effort_options(CODEX_FALLBACK_REASONING_EFFORT_NAMES)
    if backend == "claude":
        return _reasoning_effort_options(CLAUDE_REASONING_EFFORT_NAMES)
    return [dict(DEFAULT_REASONING_EFFORT_OPTION)]


def reasoning_effort_options(backend: str = "codex") -> list[dict]:
    return _backend_reasoning_effort_options(backend)


def normalize_reasoning_effort(value: object, backend: str | None = None) -> str | None:
    effort = str(value or "").strip().lower()
    if not effort or effort in {"default", "none", "null"}:
        return None
    allowed = CLAUDE_REASONING_EFFORT_NAMES if backend == "claude" else REASONING_EFFORT_NAMES
    if effort not in allowed:
        raise ValueError(f"unknown reasoning effort: {value}")
    return effort


def resolve_model(backend: str, model: str | None) -> str | None:
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


def normalize_model_selector(backend: str, model: object, config: dict | None = None) -> str | None:
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


def backend_names() -> list[str]:
    return sorted(BACKENDS)


def agent_backend_names() -> list[str]:
    return [name for name, backend in BACKENDS.items() if backend.get("kind") == "agent"]


def agent_backends(config: dict | None = None) -> list[dict]:
    return [
        {
            "name": name,
            "models": _backend_model_options(name, config),
            "reasoning_efforts": _backend_reasoning_effort_options(name),
            "commands": BACKENDS[name].get("commands", []),
            "native_commands": BACKENDS[name].get("native_commands", []),
        }
        for name in agent_backend_names()
    ]


def agent_commands(backend: str = "codex") -> list[dict]:
    return list(BACKENDS.get(backend, {}).get("commands", []))


def model_options(backend: str = "codex", config: dict | None = None) -> list[dict]:
    return _backend_model_options(backend, config)


def ensure_agent_backend(name: str) -> str:
    if name not in agent_backend_names():
        raise SystemExit(f"Unknown agent backend: {name}")
    return name


def agent_backend_or_default(name: str | None, default: str = "codex") -> str:
    return name if name in agent_backend_names() else default


def require_backend(name: str) -> dict:
    if name not in BACKENDS:
        raise SystemExit(f"Unknown backend: {name}")
    backend = dict(BACKENDS[name])
    backend["models"] = _backend_model_options(name)
    return backend
