from __future__ import annotations

from pathlib import Path

from aha_cli.services.orchestrator import dispatch_task_to_main
from aha_cli.store.filesystem import add_task, ensure_task_supervision_host_agent


def create_task_and_dispatch(
    root: Path,
    run_id: str,
    title: str,
    backend: str = "codex",
    model: str | None = None,
    workspace_path: str | None = None,
    workspace_id: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    collaboration_mode: str | None = None,
    workflow_template: str | None = None,
    delegation_policy: str | None = "auto",
    max_sub_agents: int | None = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
    description: str | None = None,
    supervision: dict[str, object] | None = None,
    context_management: dict[str, object] | None = None,
    task_skills: dict[str, object] | None = None,
    hardware_debug: dict[str, object] | None = None,
    dispatch: bool = True,
) -> dict:
    task = add_task(
        root,
        run_id,
        title,
        backend=backend,
        model=model,
        workspace_path=workspace_path,
        workspace_id=workspace_id,
        sandbox=sandbox,
        approval=approval,
        proxy_enabled=proxy_enabled,
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        collaboration_mode=collaboration_mode,
        workflow_template=workflow_template,
        delegation_policy=delegation_policy,
        max_sub_agents=max_sub_agents,
        preferred_sub_backend=preferred_sub_backend,
        preferred_sub_model=preferred_sub_model,
        description=description,
        supervision=supervision,
        context_management=context_management,
        task_skills=task_skills,
        hardware_debug=hardware_debug,
    )
    policy = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    if policy.get("mode") == "assisted" and policy.get("real_agent_enabled") and policy.get("host_backend") != "stub":
        task = ensure_task_supervision_host_agent(
            root,
            run_id,
            task["id"],
            backend=str(policy.get("host_backend") or "codex"),
            model=policy.get("host_model"),
            proxy_enabled=bool(policy.get("host_proxy_enabled")),
        )["task"]
    if dispatch:
        dispatch_task_to_main(root, run_id, task)
    return task
