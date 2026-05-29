# AHA Architecture

## Product Shape

Web is the core entry point. CLI remains useful for automation, debugging, and local scripting.

The browser UI now has three operating modes:

```text
First Run bootstrap
  create .aha/config.json
  Core Settings: Default backend (codex/claude) and Task concurrency
  set workspace roots, Codex bin/model defaults, and Claude bin
  add named Claude env groups with fixed Anthropic fields or choose Claude official with no env
  exclude runner command, default mode, and context window overrides from init UI

First Run
  create an initial run by Run name only
  leave task creation to the New Task flow

Settings
  edit the existing .aha/config.json from the Run menu
  reuse the bootstrap config form for future default changes
  keep Run/Task-specific fields outside global AHA config

Run workspace
  switch between local runs
  rename the current run
  create and manage tasks
  show the active Claude env group's ANTHROPIC_MODEL when env is configured
  fall back to official Claude model choices when no Claude env is configured
  chat with task agents
  inspect results, logs, context, sessions, and backend runtime
  import or export run archives
```

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

Important service responsibilities:

```text
services/backend_runtime.py  managed backend processes and runtime locks
services/chat.py             backend chat loop and task finalization handling
services/orchestrator.py     AHA action execution and sub-agent coordination
services/proxy.py            task proxy normalization and backend env injection
services/run_archive.py      run import/export archive format
services/onebin.py           single-file zipapp packaging
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

## Persistence Model

The active data root is an AHA home, not necessarily the repository root:

```text
$AHA_HOME
repo/.aha when initialized with --portable
~/.aha by default
```

A run is persisted as append-only event and message logs plus JSON snapshots:

```text
runs/<run-id>/
  plan.json
  events.jsonl
  inbox/<agent-id>.jsonl
  sessions/main.json
  runtime/
  tasks/<task-id>/
    task.json
    messages.jsonl
    sessions/<agent-id>.json
    rounds/<round-id>/round.json
```

`runtime/` contains process locks, offsets, and backend state. It is intentionally local-only and is excluded from run archives.

Task finals are lifecycle artifacts, not just the latest result file. A finalized round writes:

```text
tasks/<task-id>/rounds/<round-id>/final.md
tasks/<task-id>/rounds/<round-id>/final.meta.json
```

Reopening a finalized task starts the next `round-NNN` and preserves the previous final.

## Backend Model

All backends are addressed through a registry. A backend may be stateless or session-capable. If a backend cannot resume sessions, AHA still keeps the logical session scope and falls back to fresh calls.

Agent backends are valid choices for task-main and sub-agent sessions:

```text
stub
codex
claude
```

Runner backends execute tasks as batch jobs and are not valid task-main or sub-agent choices:

```text
command
```

Managed chat backends are started through `services/backend_runtime.py`. In source checkouts the runtime launches the backend-specific chat command, such as:

```text
python -m aha_cli codex-chat ...
python -m aha_cli claude-chat ...
```

In a one-bin zipapp it launches the current one-bin artifact instead, so a packaged dashboard does not require `aha_cli` to be installed as an importable Python module. External backend CLIs such as `codex` and `claude` are still resolved from the target machine.

Claude uses the same AHA task/session model as Codex. Its backend authentication and provider overrides may come from process environment variables or from `claude.env` in the AHA config. Secrets must not be written to task journals, exported documentation, or user-visible logs.

## Distribution And Portability

Run archive import/export is handled by `services/run_archive.py`.

Export:

```text
include run metadata, events, messages, tasks, sessions, prompts, results, and optionally logs
exclude runtime state and transient lock/tmp files
redact proxy values
clear backend_session_id while preserving imported_backend_session_id
```

Import:

```text
safe-extract the archive
assign a new run id by default
rewrite run_id and session scope references
mark sessions as imported
append a run_imported event
```

`aha package onebin` builds a Python zipapp containing `aha_cli` and `web/static`. The artifact still stores data in `.aha/` or the selected `--home`, and still depends on external backend CLIs such as `codex` or `claude`.
