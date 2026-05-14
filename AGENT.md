# AHA Agent Guide

This repository implements AHA: a local multi-agent orchestration prototype where Web is the primary operating surface and CLI is an auxiliary control surface.

## Engineering Rules

- Keep every source file below 1000 lines. Split before a file becomes a mixed responsibility bucket.
- `src/aha_cli/cli.py` only wires command-line arguments to service functions.
- Persistence belongs in `src/aha_cli/store/`.
- Domain object construction belongs in `src/aha_cli/domain/`.
- Backend integrations belong in `src/aha_cli/backends/`.
- Long-running behavior belongs in `src/aha_cli/services/`.
- HTTP/WebSocket serving belongs in `src/aha_cli/web/` and `src/aha_cli/websocket/`.
- Browser assets must live in `src/aha_cli/web/static/`; do not embed large HTML, CSS, or JS strings in Python.
- Events and inbox messages are append-only JSONL. Preserve old run compatibility unless a migration is implemented.

## Agent And Session Model

- `run-main` is the global controller for a run.
- Every task has an independent `task-main` session.
- Every sub-agent has its own session scoped to one task.
- Backend sessions must never be shared across task boundaries, except for `run-main`, which only works from summaries and decisions.
- Session keys must be scoped by `run_id`, optional `task_id`, `agent_id`, and optional `phase_id`.

Recommended defaults:

```text
run-main: sticky session
task-main: sticky session per task
sub-agent: sticky session per task-agent
```

## Code Layout

```text
src/aha_cli/
  cli.py
  domain/
  store/
  services/
  backends/
  web/
  websocket/
```

## Commit Convention

Use concise Conventional Commit style:

```text
feat: add task agent management
fix: preserve task_id in message replies
refactor: split web server from cli parser
docs: document session boundaries
test: cover backend registry
chore: update packaging metadata
```

## Testing Expectations

- Add tests for protocol changes.
- Add tests for old `.aha/runs/<run-id>/plan.json` compatibility.
- Add tests for task/agent/session routing.
- Keep stub backend usable for deterministic tests.
