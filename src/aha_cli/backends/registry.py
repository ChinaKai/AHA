from __future__ import annotations

MODEL_OPTIONS = [
    {"name": "", "label": "default"},
    {"name": "gpt-5.5", "label": "gpt-5.5"},
    {"name": "gpt-5.4", "label": "gpt-5.4"},
    {"name": "gpt-5.4-mini", "label": "gpt-5.4-mini"},
    {"name": "gpt-5.3-codex", "label": "gpt-5.3-codex"},
    {"name": "gpt-5.3-codex-spark", "label": "gpt-5.3-codex-spark"},
    {"name": "gpt-5.2", "label": "gpt-5.2"},
]

DEFAULT_MODEL_OPTIONS = [MODEL_OPTIONS[0]]
CODEX_AGENT_COMMANDS = [
    {"scope": "agent", "name": "/agent help", "insert": "/agent help", "desc": "Route /help to the selected agent."},
    {"scope": "agent", "name": "/agent status", "insert": "/agent status", "desc": "Route /status to the selected agent."},
    {"scope": "agent", "name": "/agent <command>", "insert": "/agent ", "desc": "Route /<command> to the selected agent."},
]
STUB_AGENT_COMMANDS = [
    {"scope": "agent", "name": "/agent help", "insert": "/agent help", "desc": "Route /help to the selected agent."},
    {"scope": "agent", "name": "/agent <command>", "insert": "/agent ", "desc": "Route /<command> to the selected agent."},
]

BACKENDS = {
    "codex": {"name": "codex", "kind": "agent", "models": MODEL_OPTIONS, "commands": CODEX_AGENT_COMMANDS, "native_commands": []},
    "stub": {"name": "stub", "kind": "agent", "models": DEFAULT_MODEL_OPTIONS, "commands": STUB_AGENT_COMMANDS, "native_commands": []},
    "command": {"name": "command", "kind": "runner", "label": "Shell command runner", "models": DEFAULT_MODEL_OPTIONS},
}


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
