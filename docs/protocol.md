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

If `task-main` needs sub-agents, it can include a JSON action payload in its response:

```json
{
  "complexity": "medium",
  "actions": [
    {
      "type": "spawn_sub",
      "title": "Inspect package rules",
      "backend": "codex",
      "model": null,
      "reason": "independent research slice"
    }
  ],
  "response": "I will delegate one slice."
}
```
