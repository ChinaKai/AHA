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
run_proxy_config_updated
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

Assisted supervision can create a task-scoped `host` agent. Task creation and
`POST /api/task/<task-id>/supervision` accept `host_backend`, `host_model`, and
`host_proxy_enabled` in the `supervision` object so the host can use the model
and proxy switch for its own backend instead of inheriting the task-main
defaults.

## Agent Backend And Runtime Config

`POST /api/agent-config` updates task agent configuration. It accepts the task
and agent identity plus any supported fields:

```json
{
  "task_id": "task-001",
  "agent_id": "main",
  "backend": "claude",
  "model": "env:work",
  "sandbox": "workspace-write",
  "approval": "never",
  "proxy_enabled": true,
  "restart_backend": true
}
```

Changing `backend` or `model` is a backend/model switch. AHA stops an active old
backend process, resets the backend session id, writes a compact handoff summary,
appends a handoff message for the new backend/model, and restarts the new backend
if the old one was active. For Codex and Claude, `model` may be an official
model id or an env-group selector such as `env:work`.

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

New tasks also expose a `workflow_template` efficiency hint. It defaults to `auto` and does not by itself choose an agent count. Supported values are `auto`, `bugfix`, `feature`, `review`, `embedded-driver`, `fault-debug`, `hil-regression`, and `release`. The template gives `task-main` a domain-specific splitting strategy while `max_sub_agents` remains the hard concurrency/cost cap. The web UI treats execution as `auto` by default and keeps legacy `solo` / `pair` / `team` values as protocol-compatible options rather than the primary user-facing choice.

Tasks may also carry optional task skills under `task_skills`. AHA discovers
selectable skills only from the current AHA service home at
`aha_home_path(root)/skills/<name>/SKILL.md` (normally
`<project>/.aha/skills/<name>/SKILL.md`); tasks store only the enabled skill
paths:

```json
{
  "task_skills": {
    "enabled_paths": ["/repo/.aha/skills/board-debug/SKILL.md"]
  }
}
```

When a selected skill is relevant, the backend prompt tells the agent to read
the referenced `SKILL.md` before acting. Skills are independent from device
configuration and can be reused for non-hardware capabilities.

The Web Knowledge surface manages service-home skills through `/api/skills`.
The API lists skills discovered under `aha_home_path(root)/skills`,
reads a skill's `SKILL.md` plus optional `agents/openai.yaml`, saves or creates a
skill at `/api/skills/<id>`, and deletes managed skill directories. Skill ids
must use lowercase letters, digits, and hyphens so writes remain confined to the
AHA skills root. Skill details also enumerate bundled files such as scripts and
executables. Skill instructions resolve those tools relative to their selected
`SKILL.md` parent directory rather than assuming a knowledge-base install path.

Tasks may also carry optional board-side automation context under
`hardware_debug`. The setting is disabled by default for archive and old-plan
compatibility. New tasks select `off`, `serial`, `network`, or `both`. The task
stores only the connection facts needed to reach the board: the host serial
device and baudrate, the board IP, optional board login credentials, and a task
access level. New tasks default to `read_only`; existing active v2 records that
predate the field retain write-compatible behavior during normalization.
Hardware operation instructions and tools such as NFS belong in task-level
skills under `task_skills`. AHA injects a compact terminal summary into task
assignment and chat prompts; the password value is never included. Web task
and status responses return an empty password plus `password_configured`:

```json
{
  "hardware_debug": {
    "mode": "both",
    "serial": {
      "device": "/dev/ttyUSB0",
      "baudrate": 115200
    },
    "network": {
      "device_ip": "192.168.1.20"
    },
    "credentials": {
      "username": "root",
      "password": "secret"
    },
    "permissions": {
      "access": "read_only"
    }
  }
}
```

`read_only` permits live terminal observation, replay, bridge status, bounded
service discovery, pause/stop, and skill query/dry-run flows. `read_write` also
permits terminal input, automatic response rules, serial takeover, board/NFS
writes, flashing, and relay operations. REST, CLI, and Hardware WebSocket input
all enforce the terminal write boundary; enabled skill instructions apply the
same access level to their state-changing tools.

Serial and Network are terminal categories. A bounded skill tool probes SSH 22
and Telnet 23 for the configured board IP, preferring SSH when both respond.
The current shared Network bridge implements Telnet; an SSH result uses the
recommended system `ssh` command in Local Terminal. The Web Terminal view and the
`hardware-attach`/`hardware-send` helpers use the same machine-level bridge for
agent and manual interaction. In `both` mode the view can switch between Serial
and Network. Future network transports can be added without changing the task's
board IP.

The browser renders this shared stream with xterm.js and connects through
`/ws/hardware-terminal?task_id=<id>&transport=serial|network`. Its realtime path
is event-driven: `xterm -> WebSocket -> 0600 Unix socket -> machine bridge ->
UART/Telnet`. WebSocket `input` frames are forwarded as literal terminal bytes
(without CLI-style backslash escape decoding), and bridge RX is pushed directly
back as `output` frames with ANSI sequences intact. JSONL `control.jsonl` remains
the compatible CLI/agent command inbox; `stream.jsonl` remains the audit log and
reconnect history, but neither is polled for live Web Terminal I/O.

Each IPC client first receives the exact stream byte offset captured when the
bridge accepts it. The WebSocket replays recent RX only through that boundary,
then consumes offset-tagged live frames, preventing gaps or duplicates at the
history/live transition. IPC frames and per-client buffers are bounded; a slow
client is disconnected instead of blocking the physical bridge. `resize` frames
update the local xterm geometry and, for Telnet, send NAWS window-size
negotiation; terminal type negotiation reports `xterm-256color`. The WebSocket
never opens a second physical UART or Telnet connection.

Serial TX is also bounded and event-driven. The UART descriptor is non-blocking,
so one `write()` may accept only part of an input frame or temporarily return no
progress. The bridge retains unsent bytes in order (up to 256 KiB), includes the
UART fd in writable readiness, and records TX only after the complete segment is
accepted. Pause/stop discards any remaining queue with an explicit system audit
record rather than silently reporting unsent bytes as transmitted. Opening a
Serial transport explicitly disables XON/XOFF and RTS/CTS so settings left by a
previous terminal program cannot stall input. The bridge sends the first byte
immediately and paces subsequent queued bytes at 1 ms intervals; this protects
small bootloader/console receivers from host-side paste and key-repeat bursts
without adding perceptible single-key latency.

Physical Serial ownership is coordinated with the traditional UUCP lock
`/run/lock/LCK..<tty>`. AHA atomically acquires this lock before opening the tty
and refuses to compete with a live minicom, picocom, flasher, or another AHA
bridge; otherwise two readers would split RX bytes and create apparent random
output loss. Pause, stop, and exceptional transport close release only AHA's own
PID-matching lock. The Web panel reports the owning process/PID and offers an
explicit, confirmed **Take over** action that sends `SIGTERM` and waits briefly;
it never escalates to `SIGKILL`, and permission failures are surfaced to the
operator. Resume waits in a blocked state until the external owner releases the
device, then re-acquires it automatically.

NFS is a network debugging tool rather than a terminal transport. Its server,
export path, board mount path, module-loading requirements, and cleanup workflow
belong in an enabled board skill. Reset, entering U-Boot, relay control,
flashing, and env inspection likewise remain skill/tool workflows.

Legacy `enabled`/`devices`/`channels` payloads remain accepted as compatibility
input. UART becomes `serial`; Telnet becomes `network`; an old NFS server is
used only as a migration fallback for the board IP. Newly saved task state uses
only `mode`/`serial`/`network`/`credentials`.

Terminal streams are stored at machine scope because a physical serial device
or Telnet endpoint can outlive and be shared by task views. Serial streams live
under `hardware/devices/`; network streams live under `hardware/network/`.
Opening the Web Terminal view lazily starts the selected bridge, and terminal
tasks expose the saved stream read-only.

Tools can additionally mirror milestones to task-local hardware I/O records.
AHA stores those records under
`runs/<run-id>/tasks/<task-id>/hardware_io.jsonl` and also appends a
`hardware_io` event to the run event stream for realtime WebSocket updates:

```json
{
  "type": "hardware_io",
  "data": {
    "task_id": "task-087",
    "agent_id": "main",
    "channel": "serial",
    "endpoint": "/dev/ttyUSB0@115200",
    "direction": "tx",
    "encoding": "text",
    "data": "reset\\r"
  }
}
```

Agents and channel operation skills should use the helper entrypoint when they
want user-visible TX/RX traces:

```text
aha hardware-io <run-id> <task-id> --agent-id main --channel serial --endpoint /dev/ttyUSB0@115200 --direction tx --data 'reset\r'
```

The legacy `delegation_policy` and `max_sub_agents` fields remain as the hard execution controls. If `task-main` needs sub-agents or must route follow-up work to an existing owner, it can include a JSON action payload in its response:

```json
{
  "complexity": "medium",
  "actions": [
    {
      "type": "spawn_sub",
      "agent_id": null,
      "scope_id": "optional stable scope id when continuing the same scope",
      "title": "Inspect package rules. Read docs/package-rules.md and src/package_rules.py first. Focus only on validation behavior and report risks plus test suggestions.",
      "backend": "codex",
      "model": null,
      "sandbox": null,
      "approval": null,
      "main_followup": "Continue the main implementation path in src/package_rules.py while the sub-agent checks package-rule edge cases.",
      "reason": "independent research slice"
    },
    {
      "type": "route_to_agent",
      "agent_id": "sub-001",
      "message": "Please continue the package-rule follow-up in your owned scope.",
      "main_followup": "Continue integrating the main package-rule API changes while sub-001 handles the follow-up.",
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

`spawn_sub` creates a new task-scoped sub-agent or reassigns a terminal sub-agent when `agent_id` names a specific reusable `sub-*`. For a brand-new sub-agent, omit `agent_id` or set it to `null`; do not invent `sub-001` / `sub-002` names. Use a concrete `agent_id` only when that sub-agent already appears in the task's agents list. Use `scope_id` only when intentionally continuing the same scope; omit it or change it for a fresh scope. `sandbox` and `approval` may be `null` to inherit the task defaults. `route_to_agent` sends a concrete follow-up message to an existing sub-agent and is used when ownership already belongs to that agent.

Sub-agent handoffs should be complete enough for independent work: include relevant files or commands already inspected, key facts, ownership boundaries, expected output, and validation target in `title`/`prompt` or `message`. When task-main should keep working after delegation, include `main_followup`; AHA will queue that message back to `main` after the sub-agent is started or routed. If `main_followup` is omitted, task-main waits for sub-agent results before the round summary.

`spawn_sub.backend` may explicitly choose the child agent backend:

```json
{
  "type": "spawn_sub",
  "agent_id": null,
  "scope_id": "claude-behavior-check",
  "title": "Check Claude-specific behavior in src/aha_cli/backends/claude.py and tests/test_backend_runners.py. Report behavior gaps and the exact tests main should run.",
  "backend": "claude",
  "model": null,
  "sandbox": "read-only",
  "approval": "never",
  "reason": "independent cross-backend validation"
}
```

When `backend` is omitted, AHA uses `preferred_sub_backend`, then `preferred_backend`, then `codex`. When `model` is omitted or `null`, a newly created sub-agent uses `preferred_sub_model`; a reused sub-agent keeps its model for same-scope continuation and uses `preferred_sub_model` for fresh-scope reuse when one is configured. `spawn_sub.model` may be an official model id or an env-group selector such as `env:work`; AHA also normalizes UI/task-side aliases such as `gpt5.5`, `kimi`, or `minimax` to the configured backend selector before launching the sub-agent. This allows a Codex task-main to start a Claude sub-agent, or a Claude task-main to start a Codex sub-agent. `route_to_agent` does not choose a new backend or model; it starts the target agent with that agent's stored backend/model.

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

`/aha final` and `POST /api/task/<task-id>/final` ask task-main to produce the final answer. A finalized round stores `final.md` and `final.meta.json`, updates `last_final_round_id`, and marks the task terminal when the backend result is completed.

`/aha complete`, `aha task complete`, and `POST /api/task/<task-id>/complete` mark the task `completed` without asking task-main to generate a Final. This direct completion path does not write `final.md`; reopen the task before sending follow-up work.

`/aha reopen`, `aha task reopen`, and `POST /api/task/<task-id>/reopen` reopen the task for follow-up. If the previous round was finalized, AHA starts the next round and keeps the old final.

## Proxy Configuration

Proxy values live in the AHA Core Settings config, split by backend:

```json
{
  "codex": {
    "proxy": {
      "http_proxy": "http://127.0.0.1:7890",
      "https_proxy": "http://127.0.0.1:7890",
      "no_proxy": "localhost,127.0.0.1,::1"
    }
  },
  "claude": {
    "proxy": {
      "http_proxy": "http://127.0.0.1:7891",
      "https_proxy": "http://127.0.0.1:7891",
      "no_proxy": "localhost,127.0.0.1,::1"
    }
  }
}
```

Tasks store only the default switch for new task agents:

```json
{"preferred_proxy_enabled": true}
```

Agents store only:

```json
{"proxy_enabled": true}
```

Assisted supervision hosts also mirror their agent proxy switch in
`supervision.host_proxy_enabled`, keeping the host proxy setting independent
from `preferred_proxy_enabled` for task-main and future sub-agents.

When the selected backend has Core Settings proxy values and the agent switch is enabled, AHA injects `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and lowercase variants into the child backend environment. Old global/run/task-level proxy value fields are still read as a config/archive/runtime compatibility fallback.

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

## Retention Archives

Retention archives are tar files with:

```text
aha-run-retention-manifest.json
run/
  logs/...
  prompts/...
  chat/...   # only when requested
```

The manifest has kind `aha.run.retention`, schema `1`, `source_run_id`, creation
time, selected policy groups, `min_age_seconds`, `delete_after_archive`, and a
file list with relative path, size, mtime, and group. Restore reads only
manifest-listed `run/` members, rejects unsafe paths, refuses current or active
heartbeat runs, skips existing files by default, and overwrites only with
`--force`.
