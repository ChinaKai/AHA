# AHA Protocol

## Events

Run events are append-only JSONL:

```text
.aha/runs/<run-id>/events.jsonl
```

Each event should include:

```json
{
  "ts": "2026-05-14T00:00:00+00:00",
  "run_id": "run-id",
  "type": "message",
  "data": {}
}
```

## Messages

Messages are also append-only JSONL. New messages should include explicit routing fields:

```json
{
  "run_id": "run-id",
  "task_id": "task-001",
  "sender": "browser",
  "target": "main",
  "from_agent": "browser",
  "to_agent": "main",
  "role": "main",
  "message": "..."
}
```

Old messages with only `sender`, `target`, and `message` remain valid.

## Task Agents

Every task has a logical `main` agent. A task may have zero or more sub-agents:

```json
{
  "id": "sub-001",
  "role": "sub",
  "backend": "codex",
  "status": "pending"
}
```

## Task Assignment

Creating a task appends an AHA-mode assignment message:

```json
{
  "sender": "system",
  "from_agent": "system",
  "target": "main",
  "to_agent": "main",
  "task_id": "task-001",
  "role": "main",
  "message": "You are now running in AHA mode..."
}
```

If `task-main` needs sub-agents or must route follow-up work to an existing owner, it can include a JSON action payload in its response:

```json
{
  "complexity": "medium",
  "actions": [
    {
      "type": "spawn_sub",
      "title": "Inspect package rules",
      "backend": "codex",
      "model": null,
      "sandbox": null,
      "approval": null,
      "reason": "independent research slice"
    },
    {
      "type": "route_to_agent",
      "agent_id": "sub-001",
      "message": "Please continue the package-rule follow-up in your owned scope.",
      "reason": "sub-001 owns package-rule analysis"
    }
  ],
  "response": "I will delegate one slice."
}
```

`spawn_sub` creates a new task-scoped sub-agent. `sandbox` and `approval` may be `null` to inherit the task defaults. `route_to_agent` sends a concrete follow-up message to an existing sub-agent and is used when ownership already belongs to that agent.
