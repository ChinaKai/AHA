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

`event_id` is the byte offset returned after appending the JSONL record. HTTP polling and WebSocket reconnects use it as a cursor:

```text
GET /api/events?last_event_id=<event-id>
GET /api/events?after_event_id=<event-id>
GET /ws?run_id=<run-id>&last_event_id=<event-id>
```

Important event families include:

```text
plan_created
message
task_dispatched
task_started
task_finished
task_status_changed
task_round_started
task_round_recorded
task_result_written
task_final_requested
task_round_summary_requested
task_reopened
task_completed
task_hidden
task_restored
task_deleted
task_proxy_config_updated
agent_created
agent_config_updated
agent_backend_switched
agent_backend_restarted
agent_started
agent_finished
agent_command_started
agent_command_finished
agent_message
agent_message_routed
backend_started
backend_session_reset
backend_start_failed
backend_stopped
run_imported
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

Task-scoped messages are also mirrored to:

```text
.aha/runs/<run-id>/tasks/<task-id>/messages.jsonl
```

Special fields used by AHA control flows:

```text
command_namespace  aha|agent command routing
original_command   original slash command text
result_policy      finalize|journal|overview
reply_target       browser or another agent target
coordination       round/final coordination marker
```

## Task Agents

Every task has a logical `main` agent. A task may have zero or more sub-agents:

```json
{
  "id": "sub-001",
  "role": "sub",
  "backend": "claude",
  "status": "pending"
}
```

The backend is stored per agent. Valid chat backends include `codex` and `claude`, so one task may contain agents backed by different providers.

## Agent Backend And Runtime Config

`POST /api/agent-config` updates task agent configuration. It accepts the task
and agent identity plus any supported fields:

```json
{
  "task_id": "task-001",
  "agent_id": "main",
  "backend": "claude",
  "sandbox": "workspace-write",
  "approval": "never",
  "proxy_enabled": true,
  "restart_backend": true
}
```

Changing `backend` is a backend switch. AHA stops an active old backend process,
resets the backend session id, writes a compact handoff summary, appends a
handoff message for the new backend, and restarts the new backend if the old one
was active.

Changing `sandbox`, `approval`, or `proxy_enabled` changes backend startup
configuration. Existing backend processes are not hot-patched. If
`restart_backend` is true, AHA saves the config and restarts the current backend
so the startup settings apply immediately. If it is false or omitted, the values
apply on the next backend start.

Relevant events:

```text
backend_session_reset
agent_backend_switched
agent_backend_restarted
agent_config_updated
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

Tasks expose a `collaboration_mode` intent:

- `auto`: AHA asks `task-main` to create sub-agents only when parallel speedup should beat startup, coordination, and merge cost.
- `solo`: no sub-agents; `task-main` handles the work directly.
- `pair`: at most one sub-agent for a parallel implementation, research, or review responsibility.
- `team`: up to two sub-agents for parallel responsibility areas, with `task-main` leading and merging.

The legacy `delegation_policy` and `max_sub_agents` fields remain as the hard execution controls. If `task-main` needs sub-agents or must route follow-up work to an existing owner, it can include a JSON action payload in its response:

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
    },
    {
      "type": "record_task_update",
      "summary": "Implemented the package rule check.",
      "changed_files": ["src/package_rules.py"],
      "verification": ["python3 -m unittest tests.test_package_rules"],
      "risks": []
    }
  ],
  "response": "I will delegate one slice."
}
```

`spawn_sub` creates a new task-scoped sub-agent. `sandbox` and `approval` may be `null` to inherit the task defaults. `route_to_agent` sends a concrete follow-up message to an existing sub-agent and is used when ownership already belongs to that agent.

`spawn_sub.backend` may explicitly choose the child agent backend:

```json
{
  "type": "spawn_sub",
  "title": "Check Claude-specific behavior",
  "backend": "claude",
  "sandbox": "read-only",
  "approval": "never",
  "reason": "independent cross-backend validation"
}
```

When `backend` is omitted, AHA uses `preferred_sub_backend`, then `preferred_backend`, then `codex`. This allows a Codex task-main to start a Claude sub-agent, or a Claude task-main to start a Codex sub-agent. `route_to_agent` does not choose a new backend; it starts the target agent with that agent's stored backend.

`record_task_update` writes a durable task journal row in:

```text
.aha/runs/<run-id>/tasks/<task-id>/rounds.jsonl
```

Use it only after completed work, validation, decisions, commits, or meaningful follow-up state.

## Task Rounds And Finals

Every task starts with `round-001`:

```json
{
  "task_id": "task-001",
  "round_id": "round-001",
  "sequence": 1,
  "status": "active",
  "started_at": "2026-05-14T00:00:00+00:00",
  "finalized_at": null,
  "final_path": null,
  "final_meta_path": null,
  "reopened_from_round_id": null
}
```

`/aha final`, `/aha complete`, and `POST /api/task/<task-id>/final` ask task-main to produce the final answer. A finalized round stores `final.md` and `final.meta.json`, updates `last_final_round_id`, and marks the task terminal when the backend result is completed.

`/aha reopen`, `aha task reopen`, and `POST /api/task/<task-id>/reopen` reopen the task for follow-up. If the previous round was finalized, AHA starts the next round and keeps the old final.

## Proxy Configuration

Proxy values live on the task:

```json
{
  "preferred_proxy_enabled": true,
  "preferred_http_proxy": "http://127.0.0.1:7890",
  "preferred_https_proxy": "http://127.0.0.1:7890",
  "preferred_no_proxy": "localhost,127.0.0.1,::1"
}
```

Agents store only:

```json
{"proxy_enabled": true}
```

When both the task has proxy values and the agent switch is enabled, AHA injects `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and lowercase variants into the child backend environment.

## Run Archives

Run archives are tar files with:

```text
aha-run-manifest.json
run/
  plan.json
  events.jsonl
  ...
```

Export excludes `runtime/`, lock/pid/tmp files, and optionally `logs/`. It redacts proxy fields and clears `backend_session_id`. Import safe-extracts the archive, creates a new run id unless `--preserve-id` or `--run-id` is used, rewrites run references, marks sessions as `imported`, and appends `run_imported`.
