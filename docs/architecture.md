# AHA Architecture

## Product Shape

Web is the core entry point. CLI remains useful for automation, debugging, and local scripting.

The orchestration hierarchy is:

```text
Run
  run-main
  Task task-001
    task-main
    sub-001
    sub-002
  Task task-002
    task-main
```

Intended future role: `run-main` decides whether work is simple enough to handle directly, should become one task, or should be split into multiple tasks. Today, `run-main` is only a reserved identity. `task-main` owns local task context. Sub-agents execute bounded work inside one task.

Current implementation note: `run-main` is reserved, not active. The plan stores a `main_agent` and a run-scoped session for future compatibility, but AHA does not currently dispatch prompts to a run-level agent. The active team unit today is one task: `task-main` owns the task outcome and may request sub-agents for bounded workstreams. AHA itself currently performs the run-level orchestration: task creation, routing, status, backend lifecycle, and result collection.

Activate `run-main` only when the product needs a real project-manager role that can decompose a user goal into multiple tasks, monitor task-main progress, manage cross-task dependencies, and produce a run-level final answer from task finals. Until then, avoid adding behavior that makes users think run-main is already participating.

Task creation is an execution event, not just metadata insertion. When a task is created, AHA writes an AHA-mode assignment to that task's `task-main` inbox. The task-main may answer directly or return structured actions such as `spawn_sub`.

## Module Boundaries

```text
domain/       object construction and protocol defaults
store/        filesystem persistence and compatibility
services/     use cases and long-running loops
backends/     agent providers and runner adapters
web/          HTTP API and browser UI
websocket/    low-level WebSocket stream
cli.py        argparse only
```

## Context Ownership

AHA owns context assembly. Backend sessions preserve local continuity but do not define task boundaries.

Context layers:

```text
Run summary
Task summary
Agent/session summary
Recent messages
Selected artifacts
Current user message
```

Each task also records a `workspace_path`. Backend agents execute from that workspace when starting a new scoped session, so task context points at the project being worked on rather than the AHA tool repository.

Task-scoped proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`) are stored on the task so users can update them after creation. Each agent stores only a `proxy_enabled` switch. Backend launches and per-turn Codex executions read the latest task proxy values and apply them only when the selected agent switch is enabled.

If run-main is activated later, it should work from summaries and decision records, not every task's full message history.

## Backend Model

All backends are addressed through a registry. A backend may be stateless or session-capable. If a backend cannot resume sessions, AHA still keeps the logical session scope and falls back to fresh calls.

Agent backends are valid choices for task-main and sub-agent sessions:

```text
stub
codex
```

Runner backends execute tasks as batch jobs and are not valid task-main or sub-agent choices:

```text
command
```
