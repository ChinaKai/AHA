from __future__ import annotations

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
