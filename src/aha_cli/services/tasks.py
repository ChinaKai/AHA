from __future__ import annotations

from pathlib import Path

from aha_cli.services.orchestrator import dispatch_task_to_main
from aha_cli.store.filesystem import add_task


def create_task_and_dispatch(
    root: Path,
    run_id: str,
    title: str,
    backend: str = "codex",
    model: str | None = None,
    workspace_path: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    delegation_policy: str = "auto",
    max_sub_agents: int = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
    dispatch: bool = True,
) -> dict:
    task = add_task(
        root,
        run_id,
        title,
        backend=backend,
        model=model,
        workspace_path=workspace_path,
        sandbox=sandbox,
        approval=approval,
        delegation_policy=delegation_policy,
        max_sub_agents=max_sub_agents,
        preferred_sub_backend=preferred_sub_backend,
        preferred_sub_model=preferred_sub_model,
    )
    if dispatch:
        dispatch_task_to_main(root, run_id, task)
    return task
