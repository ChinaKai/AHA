# AHA Agent Guide

This repository implements AHA: a local multi-agent orchestration prototype where Web is the primary operating surface and CLI is an auxiliary control surface.

## Engineering Rules

- Keep every source file below 1000 lines. Split before a file becomes a mixed responsibility bucket.
- `src/aha_cli/cli.py` only wires command-line arguments to service functions.
- Persistence belongs in `src/aha_cli/store/`.
- Domain object construction belongs in `src/aha_cli/domain/`.
- Backend integrations belong in `src/aha_cli/backends/`.
- Long-running behavior belongs in `src/aha_cli/services/`.
- Run archive import/export belongs in `src/aha_cli/services/run_archive.py`.
- Single-file executable packaging belongs in `src/aha_cli/services/onebin.py` and `scripts/build_onebin.py`.
- Proxy normalization and backend environment injection belong in `src/aha_cli/services/proxy.py`.
- HTTP/WebSocket serving belongs in `src/aha_cli/web/` and `src/aha_cli/websocket/`.
- Browser assets must live in `src/aha_cli/web/static/`; do not embed large HTML, CSS, or JS strings in Python.
- Events and inbox messages are append-only JSONL. Preserve old run compatibility unless a migration is implemented.
- Documentation should describe the current code behavior, not intended architecture. Update README and `docs/` in the same change when user-facing CLI, Web API, storage layout, run archive, packaging, proxy, or session behavior changes.

## Agent And Session Model

- `run-main` is a reserved run-level identity. It is not an active controller yet; current run-level orchestration is handled by AHA services.
- Every task has an independent `task-main` session.
- Every sub-agent has its own session scoped to one task.
- Backend sessions must never be shared across task boundaries, except for `run-main`, which only works from summaries and decisions.
- Session keys must be scoped by `run_id`, optional `task_id`, `agent_id`, and optional `phase_id`.
- Backend process runtime files are not session metadata. Keep them under `runtime/` and out of exported run archives.
- Imported sessions must not reuse backend-specific session ids from another machine or AHA home.

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
    backend_runtime.py
    onebin.py
    proxy.py
    run_archive.py
  backends/
  web/
    static/
  websocket/
scripts/
  build_onebin.py
```

## Commit Convention

Use a concise Conventional Commit subject plus AHA trailers:

```text
feat(tasks): add task agent management

AHA-Task: task-001
AHA-Agent: main
AHA-Scope: task-agent-management
```

Prefer `aha commit --type <type> --scope <scope> --summary <summary> --task-id <task-id> --agent <agent-id> --aha-scope <short-scope>` over raw `git commit`, and validate hand-written messages with `aha commit-check`.

## Testing Expectations

- Add tests for protocol changes.
- Add tests for old `.aha/runs/<run-id>/plan.json` compatibility.
- Add tests for task/agent/session routing.
- Keep stub backend usable for deterministic tests.
