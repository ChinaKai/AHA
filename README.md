# AHA CLI

`aha` means `agent help agent`.

This repository is a prototype CLI for the workflow:

```text
main agent = planner + dispatcher + collector + merger
sub agents = independent researchers or bounded workers
```

The first version intentionally stays backend-neutral. It does not assume a specific agent runtime. Instead, it creates task prompts and can run any external command you provide with `--runner-command`.

## What It Does

`aha` manages a local `.aha/` directory:

```text
.aha/
  config.json
  runs/
    <run-id>/
      plan.json
      events.jsonl
      sessions/
      prompts/
      inbox/
      results/
      logs/
      tasks/
        task-001/
          task.json
          messages.jsonl
          sessions/
      merged-report.md
```

Supported commands:

```bash
aha init
aha plan "goal" --agents 4
aha run <run-id>
aha status <run-id>
aha task add <run-id> "extra task" --workspace-path /path/to/project --max-sub-agents 3
aha agent add <run-id> task-001 --backend codex
aha session list <run-id> --task-id task-001
aha watch <run-id>
aha send <run-id> task-001 "please inspect the package flow"
aha chat <run-id> task-001
aha auto-reply <run-id> main
aha codex-chat <run-id> main
aha serve <run-id>
aha ui <run-id>
aha collect <run-id>
aha merge <run-id>
aha list
```

## Quick Start

From this project:

```bash
python3 -m aha_cli init
python3 -m aha_cli plan "Create a Buildroot learning guide" --agents 4
python3 -m aha_cli run
python3 -m aha_cli status
python3 -m aha_cli merge
```

Without a runner, `aha run` writes stub results. This lets you test the orchestration flow before integrating a real agent backend.

## Running With A Real Agent Command

Use `--runner-command` with placeholders:

```bash
aha run <run-id> --runner-command 'your-agent --prompt-file {prompt_file} --output {output_file}'
```

Available placeholders:

```text
{root}
{run_id}
{run_dir}
{task_id}
{prompt_file}
{output_file}
{log_file}
{inbox_file}
{events_file}
```

The same values are also exported as environment variables:

```text
AHA_ROOT
AHA_RUN_ID
AHA_RUN_DIR
AHA_TASK_ID
AHA_PROMPT_FILE
AHA_OUTPUT_FILE
AHA_LOG_FILE
AHA_INBOX_FILE
AHA_EVENTS_FILE
```

For example, a simple shell runner can write a Markdown result to `$AHA_OUTPUT_FILE`.

## Agent And Session Model

AHA separates logical agents from backend sessions:

```text
run-main: run:<run-id>:agent:main
task-main: run:<run-id>:task:<task-id>:agent:main
sub-agent: run:<run-id>:task:<task-id>:agent:<sub-id>
```

`run-main` is currently a reserved identity. AHA stores run-level metadata for it, but the active team model today is task-scoped: one `task-main` plus optional sub-agents. AHA itself handles run-level orchestration until a real run-main project-manager workflow is implemented.

Every task has one `task-main`. Sub-agents are optional and can be 0..n per task. Backend sessions are scoped to task/agent boundaries so task context does not bleed across unrelated work.

Creating a task automatically dispatches an AHA-mode assignment to that task's `task-main`. The assignment asks `task-main` to judge complexity and, if needed, return structured `spawn_sub` actions. AHA executes those actions by creating sub-agents scoped to the current task.

Useful commands:

```bash
aha task add <run-id> "Analyze package rules" \
  --workspace-path /home/kaikai/kk-workspace/hl_project/fw_omni_builder \
  --backend codex \
  --model gpt-5.2 \
  --delegation-policy auto \
  --max-sub-agents 3
aha task list <run-id>
aha task show <run-id> task-001

aha agent add <run-id> task-001 --role sub --backend codex
aha agent list <run-id> task-001

aha session list <run-id> --task-id task-001
aha session reset <run-id> sub-001 --task-id task-001
```

## Running With Codex

If the local `codex` CLI is installed and authenticated, run pending tasks through Codex:

```bash
aha run <run-id> --backend codex --parallel 2
```

Useful options:

```bash
aha run <run-id> --backend codex \
  --codex-model gpt-5.2 \
  --codex-sandbox read-only \
  --codex-approval never
```

`--codex-sandbox auto` uses `read-only` for research plans and `workspace-write` for implementation plans. The Codex backend uses `codex exec --skip-git-repo-check` and writes the final response to each task result file.

Start a real Codex-backed browser chat responder:

```bash
aha codex-chat <run-id> main
```

When the dashboard sends a message to `main`, `codex-chat` reads `.aha/runs/<run-id>/inbox/main.jsonl`, calls `codex exec`, and writes the model response back as a `main -> browser` message event.

## Realtime Status And Conversation

`aha` writes JSONL events while tasks run:

```text
.aha/runs/<run-id>/events.jsonl
```

Watch them from the terminal:

```bash
aha watch <run-id>
aha watch <run-id> --once
```

Send a message to a task inbox:

```bash
aha send <run-id> task-001 "Can you narrow this to the package layer?"
```

Open an interactive line-based chat:

```bash
aha chat <run-id> task-001
```

Messages are appended to:

```text
.aha/runs/<run-id>/inbox/task-001.jsonl
```

Important: realtime conversation requires the runner/agent backend to cooperate. `aha` can write the inbox and expose the path through `$AHA_INBOX_FILE`, but the actual agent must read that file or implement its own watch loop.

For a local demo feedback loop, start an automatic responder:

```bash
aha auto-reply <run-id> main
```

It watches `.aha/runs/<run-id>/inbox/main.jsonl` and writes reply events such as `main -> browser`. This is only a local responder; a real integration should replace it with the actual agent backend reading its assigned inbox.

Start the minimal WebSocket server:

```bash
aha serve <run-id> --host 127.0.0.1 --port 8765
```

The WebSocket server sends JSON messages:

```json
{"type":"status","data":{}}
{"type":"event","data":{}}
```

Clients can send:

```json
{"type":"send","target":"task-001","message":"please check logs","sender":"web"}
{"type":"status"}
```

This WebSocket server is intentionally minimal and dependency-free. It is suitable for local dashboards and prototypes, not exposed production use.

Start the browser dashboard:

```bash
aha ui <run-id> --host 0.0.0.0 --port 8766
```

Open:

```text
http://<host-lan-ip>:8766
```

The dashboard uses local HTTP endpoints:

```text
GET  /api/status
GET  /api/backends
GET  /api/events?offset=<byte-offset>
GET  /api/task/<task-id>
POST /api/tasks
POST /api/agents
POST /api/send
```

It shows a task list on the left, a task workspace in the center, and task agents on the right. Selecting a task opens that task's conversation, result, logs, and prompt context.

```text
left: task list and task creation with workspace/backend/model/delegation policy
center: conversation/result/logs/context
right: task-main/sub agents and backend session details
```

Messages still use JSONL inbox/event files, but browser messages carry `task_id`, `from_agent`, and `to_agent` so the backend can keep task conversations separated.

The task form dispatches the assignment immediately. Users should not need to repeat the task in the conversation box after creation.

## Suggested Operating Model

Use `research` mode when sub agents should not modify files:

```bash
aha plan "Understand the firmware build flow" --mode research --agents 4
```

Use `implementation` mode only with clear write scopes:

```bash
aha plan "Refactor module boundaries" \
  --mode implementation \
  --task "Update parser module" --write-scope src/parser \
  --task "Update renderer module" --write-scope src/renderer
```

Recommended rule:

```text
sub agents collect facts or edit isolated scopes;
main agent makes final decisions and merges output.
```

## Why This Exists

The CLI captures the multi-agent project-manager pattern:

```text
1. create plan
2. dispatch independent tasks
3. run in parallel
4. collect structured outputs
5. merge into one report
```

Future versions can add native integrations for specific agent CLIs, git worktree isolation for worker tasks, conflict-aware merge, and richer planning.

## Development

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Run the CLI without installing:

```bash
PYTHONPATH=src python3 -m aha_cli --help
```
