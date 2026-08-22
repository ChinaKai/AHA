"""Backend turn preparation and plugin dispatch.

This module keeps provider-specific optional integrations out of the generic
chat lifecycle.  AHA still owns checkpoints, task state, actions, Git gates,
and finals; this layer prepares one backend turn and delegates it to the
registered plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aha_cli.backends.claude import claude_permission_mode, run_claude_exec
from aha_cli.backends.codex import run_codex_exec
from aha_cli.backends.plugin import (
    BackendPlugin,
    BackendResolvedTurn,
    BackendTurnRequest,
    BackendTurnResult,
)
from aha_cli.services.headroom_integration import prepare_headroom_codex_runtime
from aha_cli.services.observe_proxy import (
    prepare_observe_claude_runtime,
    prepare_observe_codex_runtime,
)
from aha_cli.store.filesystem import append_event


@dataclass
class BackendTurnExecution:
    root: Path
    config: dict
    run_id: str
    task_id: str | None
    agent_id: str
    backend_name: str
    task: dict
    plugin: BackendPlugin
    prompt: str
    workspace: Path
    output_file: Path
    events_file: Path
    source: str
    target: str
    session: dict
    sandbox: str
    approval: str
    extra_args: list[str]
    binary: str
    json_events: bool
    resolved: BackendResolvedTurn
    proxy_env: dict[str, str] | None
    event_callback: Callable[[str, dict], None] | None = None
    turn_runner: Callable | None = None
    prepare_headroom: Callable | None = None
    prepare_observe_codex: Callable | None = None
    prepare_observe_claude: Callable | None = None


def _record_optional_runtime(
    context: BackendTurnExecution,
    runtime: dict,
    *,
    ready_event: str,
    skipped_event: str,
) -> None:
    if not runtime.get("enabled"):
        return
    append_event(
        context.root,
        context.run_id,
        ready_event if runtime.get("ready") else skipped_event,
        {
            "source": context.source,
            "target": context.target,
            "task_id": context.task_id,
            "agent_id": context.agent_id,
            "backend": context.backend_name,
            "ready": bool(runtime.get("ready")),
            "reason": runtime.get("reason"),
            "port": runtime.get("port"),
            "scope": runtime.get("scope"),
            "mode": runtime.get("mode"),
            "upstream_base_url": runtime.get("upstream_base_url"),
            "local_base_url": runtime.get("local_base_url"),
        },
    )


def _prepare_claude(context: BackendTurnExecution) -> BackendResolvedTurn:
    prepare = context.prepare_observe_claude or prepare_observe_claude_runtime
    proxy_env, runtime = prepare(
        context.root,
        config=context.config,
        task=context.task,
        backend_name=context.backend_name,
        claude_config=context.resolved.backend_config,
        proxy_env=context.proxy_env,
        run_id=context.run_id,
        task_id=context.task_id,
        agent_id=context.agent_id,
        workspace=context.workspace,
    )
    _record_optional_runtime(
        context,
        runtime,
        ready_event="observe_proxy_ready",
        skipped_event="observe_proxy_skipped",
    )
    resolved = context.resolved
    resolved.proxy_env = proxy_env
    resolved.extras.update({
        "permission_mode": claude_permission_mode("research", context.sandbox),
        # Compatibility injection point used by existing tests/extensions.
        "turn_runner": context.turn_runner or run_claude_exec,
    })
    return resolved


def _prepare_codex(context: BackendTurnExecution) -> BackendResolvedTurn:
    prepare_headroom = context.prepare_headroom or prepare_headroom_codex_runtime
    config, proxy_env, headroom = prepare_headroom(
        context.root,
        config=context.config,
        task=context.task,
        backend_name=context.backend_name,
        codex_config=context.resolved.backend_config,
        proxy_env=context.proxy_env,
        run_id=context.run_id,
        task_id=context.task_id,
        agent_id=context.agent_id,
        workspace=context.workspace,
    )
    _record_optional_runtime(
        context,
        headroom,
        ready_event="headroom_integration_ready",
        skipped_event="headroom_integration_skipped",
    )
    prepare_observe = context.prepare_observe_codex or prepare_observe_codex_runtime
    config, proxy_env, runtime = prepare_observe(
        context.root,
        config=context.config,
        task=context.task,
        backend_name=context.backend_name,
        codex_config=config,
        proxy_env=proxy_env,
        run_id=context.run_id,
        task_id=context.task_id,
        agent_id=context.agent_id,
        workspace=context.workspace,
    )
    _record_optional_runtime(
        context,
        runtime,
        ready_event="observe_proxy_ready",
        skipped_event="observe_proxy_skipped",
    )
    resolved = context.resolved
    resolved.backend_config = config
    resolved.proxy_env = proxy_env
    resolved.extras["turn_runner"] = context.turn_runner or run_codex_exec
    # Codex exec accepts the normalized selector (including env:<group>) and
    # resolves the final CLI model after provider overrides are applied.
    resolved.extras["execution_model"] = resolved.extras.get("normalized_model")
    return resolved


def execute_backend_turn(context: BackendTurnExecution) -> BackendTurnResult:
    if context.backend_name == "claude":
        resolved = _prepare_claude(context)
    elif context.backend_name == "codex":
        resolved = _prepare_codex(context)
    else:
        resolved = context.resolved
        resolved.proxy_env = context.proxy_env
    return context.plugin.run_turn(
        BackendTurnRequest(
            prompt=context.prompt,
            cwd=context.workspace,
            output_file=context.output_file,
            events_file=context.events_file,
            run_id=context.run_id,
            task_id=context.task_id,
            source=context.source,
            target=context.target,
            session=context.session,
            sandbox=context.sandbox,
            approval=context.approval,
            extra_args=context.extra_args,
            binary=context.binary,
            json_events=context.json_events,
            resolved=resolved,
            aha_home=context.root,
            config=context.config,
            event_callback=context.event_callback,
        )
    )


__all__ = ["BackendTurnExecution", "execute_backend_turn"]
