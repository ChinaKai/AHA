# Session Model

## Principle

```text
AHA Agent Identity != Backend Session
```

AHA identities are stable logical roles. Backend sessions are execution containers scoped by run, task, agent, and phase.

## Session Keys

```text
run:<run-id>:agent:main
run:<run-id>:task:<task-id>:agent:main
run:<run-id>:task:<task-id>:agent:<sub-id>
```

## Policies

```text
sticky     reuse the scoped backend session when supported
stateless  build explicit context and start a fresh backend call
```

Defaults:

```text
run-main: sticky
task-main: sticky
sub-agent: sticky
```

## Stored Session Metadata

```json
{
  "id": "task-001:main",
  "run_id": "...",
  "task_id": "task-001",
  "agent_id": "main",
  "backend": "codex",
  "policy": "sticky",
  "backend_session_id": "019e...",
  "status": "active"
}
```

Backends that do not expose resumable sessions leave `backend_session_id` empty.

Session files live beside the scope they belong to:

```text
runs/<run-id>/sessions/main.json
runs/<run-id>/tasks/<task-id>/sessions/main.json
runs/<run-id>/tasks/<task-id>/sessions/sub-001.json
```

## Runtime State

Session metadata is durable. Backend process state is runtime-only:

```text
runs/<run-id>/runtime/backend-<task-id>-<agent-id>.json
runs/<run-id>/runtime/backend-<task-id>-<agent-id>.lock
runs/<run-id>/runtime/chat-offset-<task-id>-<agent-id>.json
```

Runtime files contain child process pid, command, sandbox, approval, log path, and managed status. They are excluded from run exports because they are tied to one machine and one process tree.

## Managed Backend Launch

For source checkouts, a managed Codex chat backend is launched as:

```text
<python> -m aha_cli --home <aha-home> codex-chat <run-id> <agent-id> --task-id <task-id> ...
```

For a packaged one-bin zipapp, AHA launches the current artifact instead:

```text
<python> <path-to-aha-onebin> --home <aha-home> codex-chat <run-id> <agent-id> --task-id <task-id> ...
```

This keeps one-bin deployments from depending on an installed `aha_cli` Python module. External tools such as `codex` are still resolved from the target machine.

## Imported Sessions

Run export clears backend session ids because they are not portable. The previous id is preserved as `imported_backend_session_id` when present.

After import, session metadata is marked:

```json
{
  "backend_session_id": null,
  "status": "imported",
  "imported_from_run_id": "20260514-161750-e948e3",
  "imported_at": "2026-05-19T00:00:00+00:00"
}
```

The next backend interaction may create a fresh backend session under the same logical AHA scope.
