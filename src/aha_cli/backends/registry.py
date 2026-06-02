from __future__ import annotations

import re

CODEX_DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MODEL_OPTION = {"name": "", "label": "default"}

MODEL_OPTIONS = [
    {"name": "", "label": f"default ({CODEX_DEFAULT_MODEL})"},
    {"name": "gpt-5.5", "label": "gpt-5.5"},
    {"name": "gpt-5.4", "label": "gpt-5.4"},
    {"name": "gpt-5.4-mini", "label": "gpt-5.4-mini"},
    {"name": "gpt-5.3-codex", "label": "gpt-5.3-codex"},
    {"name": "gpt-5.3-codex-spark", "label": "gpt-5.3-codex-spark"},
    {"name": "gpt-5.2", "label": "gpt-5.2"},
]

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
    "codex": {"name": "codex", "kind": "agent", "models": MODEL_OPTIONS, "commands": CODEX_AGENT_COMMANDS, "native_commands": []},
    "claude": {"name": "claude", "kind": "agent", "models": CLAUDE_MODEL_OPTIONS, "commands": CLAUDE_AGENT_COMMANDS, "native_commands": []},
    "stub": {"name": "stub", "kind": "agent", "models": DEFAULT_MODEL_OPTIONS, "commands": STUB_AGENT_COMMANDS, "native_commands": []},
    "command": {"name": "command", "kind": "runner", "label": "Shell command runner", "models": DEFAULT_MODEL_OPTIONS},
}


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
        for option in model_options(backend):
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


def agent_backends() -> list[dict]:
    return [
        {
            "name": name,
            "models": BACKENDS[name].get("models", DEFAULT_MODEL_OPTIONS),
            "commands": BACKENDS[name].get("commands", []),
            "native_commands": BACKENDS[name].get("native_commands", []),
        }
        for name in agent_backend_names()
    ]


def agent_commands(backend: str = "codex") -> list[dict]:
    return list(BACKENDS.get(backend, {}).get("commands", []))


def model_options(backend: str = "codex") -> list[dict]:
    return list(BACKENDS.get(backend, {}).get("models", DEFAULT_MODEL_OPTIONS))


def ensure_agent_backend(name: str) -> str:
    if name not in agent_backend_names():
        raise SystemExit(f"Unknown agent backend: {name}")
    return name


def agent_backend_or_default(name: str | None, default: str = "codex") -> str:
    return name if name in agent_backend_names() else default


def require_backend(name: str) -> dict:
    if name not in BACKENDS:
        raise SystemExit(f"Unknown backend: {name}")
    return BACKENDS[name]
