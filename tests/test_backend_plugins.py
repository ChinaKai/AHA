from __future__ import annotations

from pathlib import Path

import pytest

from aha_cli.backends.plugin import (
    BackendDescriptor,
    BackendResolvedTurn,
    BackendTurnRequest,
    BackendTurnResult,
    FunctionalBackendPlugin,
    get_backend_plugin,
    process_backend_plugins,
    register_backend,
    unregister_backend_for_tests,
)
from aha_cli.backends.registry import (
    agent_backend_names,
    agent_backends,
    backend_names,
    model_options,
    require_backend,
)


def _fake_plugin(name: str = "fake-plugin") -> FunctionalBackendPlugin:
    return FunctionalBackendPlugin(
        BackendDescriptor(
            name=name,
            kind="agent",
            label="Fake Plugin",
            process_chat=True,
            chat_command=f"{name}-chat",
            config_key=name,
            binary_option=f"--{name}-bin",
            default_binary=name,
            supports_sessions=True,
            commands=({"scope": "agent", "name": "/fake", "insert": "/fake "},),
        ),
        model_options=lambda _config: [
            {"name": "", "label": "default"},
            {"name": "fake/model", "label": "Fake Model"},
        ],
        run_turn=lambda request: BackendTurnResult(
            exit_code=0,
            reply=f"fake:{request.prompt}",
            session=request.session,
        ),
    )


def test_builtin_process_plugins_are_registered() -> None:
    assert {"codex", "claude"}.issubset(
        {plugin.descriptor.name for plugin in process_backend_plugins()}
    )
    assert get_backend_plugin("codex").descriptor.chat_command == "codex-chat"
    assert get_backend_plugin("claude").descriptor.binary_option == "--claude-bin"


def test_fake_plugin_flows_through_registry_without_generic_backend_edits() -> None:
    plugin = _fake_plugin()
    register_backend(plugin)
    try:
        assert "fake-plugin" in backend_names()
        assert "fake-plugin" in agent_backend_names()
        assert model_options("fake-plugin")[1]["name"] == "fake/model"
        listed = next(item for item in agent_backends() if item["name"] == "fake-plugin")
        assert listed["commands"][0]["name"] == "/fake"
        assert require_backend("fake-plugin")["label"] == "Fake Plugin"

        # Registry callers receive copies and cannot mutate the descriptor.
        listed["commands"][0]["name"] = "changed"
        assert require_backend("fake-plugin")["commands"][0]["name"] == "/fake"
    finally:
        unregister_backend_for_tests("fake-plugin")


def test_duplicate_plugin_registration_is_rejected() -> None:
    plugin = _fake_plugin("duplicate-plugin")
    register_backend(plugin)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_backend(_fake_plugin("duplicate-plugin"))
    finally:
        unregister_backend_for_tests("duplicate-plugin")


def test_plugin_turn_contract_returns_standard_result(tmp_path: Path) -> None:
    plugin = _fake_plugin("turn-plugin")
    request = BackendTurnRequest(
        prompt="hello",
        cwd=tmp_path,
        output_file=tmp_path / "out.md",
        events_file=None,
        run_id="run-1",
        task_id="task-1",
        source="turn-plugin-chat",
        target="main",
        session={"backend": "turn-plugin"},
        sandbox="workspace-write",
        approval="never",
        extra_args=[],
        binary="turn-plugin",
        json_events=True,
        resolved=BackendResolvedTurn(command_model="fake/model"),
        aha_home=tmp_path / ".aha",
        config={},
    )

    result = plugin.run_turn(request)

    assert result.exit_code == 0
    assert result.reply == "fake:hello"
    assert result.session == {"backend": "turn-plugin"}
