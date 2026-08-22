"""Typed internal backend plugin contracts.

The first plugin layer is intentionally built-in only.  It separates backend
metadata and behavior from AHA's generic task/chat lifecycle without importing
arbitrary user Python packages.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


BackendKind = Literal["agent", "runner"]
SandboxEquivalence = Literal["os-enforced", "tool-policy", "unsupported"]


@dataclass(frozen=True)
class BackendDescriptor:
    name: str
    kind: BackendKind
    label: str = ""
    process_chat: bool = False
    chat_command: str | None = None
    runner_command: str | None = None
    config_key: str | None = None
    binary_option: str | None = None
    default_binary: str | None = None
    supports_sessions: bool = False
    supports_reasoning_effort: bool = False
    supports_runtime_context: bool = False
    supports_no_json: bool = False
    supports_requested_model_override: bool = False
    sandbox_equivalence: SandboxEquivalence = "unsupported"
    commands: tuple[dict, ...] = ()
    native_commands: tuple[dict, ...] = ()


@dataclass
class BackendResolvedTurn:
    requested_model: str | None = None
    command_model: str | None = None
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    backend_config: dict = field(default_factory=dict)
    proxy_env: dict[str, str] | None = None
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class BackendTurnRequest:
    prompt: str
    cwd: Path
    output_file: Path
    events_file: Path | None
    run_id: str
    task_id: str | None
    source: str
    target: str | None
    session: dict
    sandbox: str
    approval: str
    extra_args: list[str]
    binary: str
    json_events: bool
    resolved: BackendResolvedTurn
    aha_home: Path
    config: dict
    event_callback: Callable[[str, dict], None] | None = None


@dataclass
class BackendTurnResult:
    exit_code: int
    reply: str
    session: dict | None


ModelOptionsLoader = Callable[[dict | None], list[dict]]
ReasoningOptionsLoader = Callable[[], list[dict]]
ReasoningNormalizer = Callable[[object], str | None]
ModelSelectorNormalizer = Callable[[object, dict | None], str | None]
ModelResolver = Callable[[str | None], str | None]
RunnerCommandBuilder = Callable[[object, dict], str]
TurnResolver = Callable[..., BackendResolvedTurn]
TurnRunner = Callable[[BackendTurnRequest], BackendTurnResult]
UsageNormalizer = Callable[[dict], dict]
SessionArtifactInfoLoader = Callable[..., dict]


class BackendPlugin:
    """Base class for built-in backend plugins.

    Optional behavior has explicit default methods so callers do not infer
    capabilities through ``hasattr``.
    """

    descriptor: BackendDescriptor

    def model_options(self, config: dict | None = None) -> list[dict]:
        return [{"name": "", "label": "default"}]

    def reasoning_effort_options(self) -> list[dict]:
        return [{"name": "", "label": "default"}]

    def normalize_reasoning_effort(self, value: object) -> str | None:
        text = str(value or "").strip().lower()
        return None if not text or text in {"default", "none", "null"} else text

    def normalize_model_selector(
        self,
        value: object,
        config: dict | None = None,
    ) -> str | None:
        text = str(value or "").strip()
        return None if not text or text.lower() == "default" else text

    def resolve_model(self, model: str | None) -> str | None:
        text = str(model or "").strip()
        return text or None

    def runner_command(self, args, config: dict) -> str | None:
        return None

    def resolve_turn(self, **kwargs) -> BackendResolvedTurn:
        raise NotImplementedError(f"backend {self.descriptor.name} does not implement turn resolution")

    def run_turn(self, request: BackendTurnRequest) -> BackendTurnResult:
        raise NotImplementedError(f"backend {self.descriptor.name} does not implement agent turns")

    def normalize_usage(self, usage: dict) -> dict:
        return dict(usage)

    def session_artifact_info(
        self,
        *,
        aha_home: Path,
        run_id: str,
        task_id: str | None,
        target: str | None,
        session_id: str,
    ) -> dict:
        del aha_home, run_id, task_id, target, session_id
        return {}


class FunctionalBackendPlugin(BackendPlugin):
    """Small adapter used while migrating existing built-in implementations."""

    def __init__(
        self,
        descriptor: BackendDescriptor,
        *,
        model_options: ModelOptionsLoader | None = None,
        reasoning_options: ReasoningOptionsLoader | None = None,
        normalize_reasoning: ReasoningNormalizer | None = None,
        normalize_model: ModelSelectorNormalizer | None = None,
        resolve_model: ModelResolver | None = None,
        runner_command: RunnerCommandBuilder | None = None,
        resolve_turn: TurnResolver | None = None,
        run_turn: TurnRunner | None = None,
        normalize_usage: UsageNormalizer | None = None,
        session_artifact_info: SessionArtifactInfoLoader | None = None,
    ) -> None:
        self.descriptor = descriptor
        self._model_options = model_options
        self._reasoning_options = reasoning_options
        self._normalize_reasoning = normalize_reasoning
        self._normalize_model = normalize_model
        self._resolve_model = resolve_model
        self._runner_command = runner_command
        self._resolve_turn = resolve_turn
        self._run_turn = run_turn
        self._normalize_usage = normalize_usage
        self._session_artifact_info = session_artifact_info

    @staticmethod
    def _copy_options(options: list[dict]) -> list[dict]:
        return [dict(option) for option in options]

    def model_options(self, config: dict | None = None) -> list[dict]:
        if self._model_options is None:
            return super().model_options(config)
        return self._copy_options(self._model_options(config))

    def reasoning_effort_options(self) -> list[dict]:
        if self._reasoning_options is None:
            return super().reasoning_effort_options()
        return self._copy_options(self._reasoning_options())

    def normalize_reasoning_effort(self, value: object) -> str | None:
        if self._normalize_reasoning is None:
            return super().normalize_reasoning_effort(value)
        return self._normalize_reasoning(value)

    def normalize_model_selector(
        self,
        value: object,
        config: dict | None = None,
    ) -> str | None:
        if self._normalize_model is None:
            return super().normalize_model_selector(value, config)
        return self._normalize_model(value, config)

    def resolve_model(self, model: str | None) -> str | None:
        if self._resolve_model is None:
            return super().resolve_model(model)
        return self._resolve_model(model)

    def runner_command(self, args, config: dict) -> str | None:
        if self._runner_command is None:
            return super().runner_command(args, config)
        return self._runner_command(args, config)

    def resolve_turn(self, **kwargs) -> BackendResolvedTurn:
        if self._resolve_turn is None:
            return super().resolve_turn(**kwargs)
        return self._resolve_turn(**kwargs)

    def run_turn(self, request: BackendTurnRequest) -> BackendTurnResult:
        if self._run_turn is None:
            return super().run_turn(request)
        return self._run_turn(request)

    def normalize_usage(self, usage: dict) -> dict:
        if self._normalize_usage is None:
            return super().normalize_usage(usage)
        return self._normalize_usage(usage)

    def session_artifact_info(
        self,
        *,
        aha_home: Path,
        run_id: str,
        task_id: str | None,
        target: str | None,
        session_id: str,
    ) -> dict:
        if self._session_artifact_info is None:
            return super().session_artifact_info(
                aha_home=aha_home,
                run_id=run_id,
                task_id=task_id,
                target=target,
                session_id=session_id,
            )
        return self._session_artifact_info(
            aha_home=aha_home,
            run_id=run_id,
            task_id=task_id,
            target=target,
            session_id=session_id,
        )


_PLUGINS: OrderedDict[str, BackendPlugin] = OrderedDict()


def register_backend(plugin: BackendPlugin) -> BackendPlugin:
    name = str(plugin.descriptor.name or "").strip()
    if not name:
        raise ValueError("backend plugin name is required")
    if name in _PLUGINS:
        raise ValueError(f"backend plugin already registered: {name}")
    _PLUGINS[name] = plugin
    return plugin


def get_backend_plugin(name: str) -> BackendPlugin:
    try:
        return _PLUGINS[str(name or "").strip()]
    except KeyError as exc:
        raise KeyError(f"unknown backend plugin: {name}") from exc


def maybe_backend_plugin(name: str) -> BackendPlugin | None:
    return _PLUGINS.get(str(name or "").strip())


def backend_plugins() -> list[BackendPlugin]:
    return list(_PLUGINS.values())


def agent_backend_plugins() -> list[BackendPlugin]:
    return [plugin for plugin in _PLUGINS.values() if plugin.descriptor.kind == "agent"]


def runner_backend_plugins() -> list[BackendPlugin]:
    return [plugin for plugin in _PLUGINS.values() if plugin.descriptor.kind == "runner"]


def process_backend_plugins() -> list[BackendPlugin]:
    return [
        plugin
        for plugin in agent_backend_plugins()
        if plugin.descriptor.process_chat
    ]


def backend_plugin_for_chat_command(command: str) -> BackendPlugin | None:
    value = str(command or "").strip()
    return next(
        (
            plugin
            for plugin in process_backend_plugins()
            if plugin.descriptor.chat_command == value
        ),
        None,
    )


def registered_backend_names() -> list[str]:
    return list(_PLUGINS)


def clear_backend_plugins_for_tests() -> None:
    _PLUGINS.clear()


def unregister_backend_for_tests(name: str) -> None:
    _PLUGINS.pop(str(name or "").strip(), None)


__all__ = [
    "AgentBackendPlugin",
    "BackendDescriptor",
    "BackendPlugin",
    "BackendResolvedTurn",
    "BackendTurnRequest",
    "BackendTurnResult",
    "FunctionalBackendPlugin",
    "agent_backend_plugins",
    "backend_plugins",
    "backend_plugin_for_chat_command",
    "clear_backend_plugins_for_tests",
    "get_backend_plugin",
    "maybe_backend_plugin",
    "process_backend_plugins",
    "register_backend",
    "registered_backend_names",
    "runner_backend_plugins",
    "unregister_backend_for_tests",
]
