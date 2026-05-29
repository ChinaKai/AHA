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

`backend_session_id` stores the backend-native resumable session identifier. For Codex this is the Codex thread id. For Claude this is the Claude session id. The durable field stays backend-neutral so task and session storage do not need provider-specific columns.

Some events still expose the backend-native id as `thread_id` for compatibility with older logs and UI replay code. Treat that event field as a legacy transport name, not as a Codex-only concept.

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

For source checkouts, managed chat backends are launched with backend-specific commands:

```text
<python> -m aha_cli --home <aha-home> codex-chat <run-id> <agent-id> --task-id <task-id> ...
<python> -m aha_cli --home <aha-home> claude-chat <run-id> <agent-id> --task-id <task-id> ...
```

For a packaged one-bin zipapp, AHA launches the current artifact instead:

```text
<python> <path-to-aha-onebin> --home <aha-home> codex-chat <run-id> <agent-id> --task-id <task-id> ...
<python> <path-to-aha-onebin> --home <aha-home> claude-chat <run-id> <agent-id> --task-id <task-id> ...
```

This keeps one-bin deployments from depending on an installed `aha_cli` Python module. External tools such as `codex` and `claude` are still resolved from the target machine.

Claude backend launches receive `ANTHROPIC_*` and `CLAUDE_*` variables from the process environment plus `claude.env` in the AHA config. Store only configuration shape in docs and logs; never persist real secret values in task-visible output.

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

## Backend Switching

AHA agent identity stays stable when an agent changes backend. The backend
session does not. Switching a task `main`, `sub-*`, or assisted-supervision
`host` backend stops any active old backend process, archives the old
`backend_session_id`, clears the active id, and appends a handoff message for
the new backend.

The handoff message points at a compact summary stored under the task compact
directory. The new backend should read that summary before continuing so it
inherits the task intent, decisions, and open work without pretending to resume
the old provider's native session.

Startup settings such as `sandbox`, `approval`, and `proxy_enabled` are applied
when a backend process starts. If those values change while a backend is running,
the UI can either save them for the next start or request an immediate backend
restart.
