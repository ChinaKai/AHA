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
