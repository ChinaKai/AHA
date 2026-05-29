# AHA CLI

`aha` means `agent help agent`.

This repository is a prototype CLI for the workflow:

```text
main agent = planner + dispatcher + collector + merger
sub agents = independent researchers or bounded workers
```

AHA keeps the orchestration model backend-aware but loosely coupled. It can write deterministic stub results, run any external command through `--runner-command`, or use the local Codex CLI for task and chat backends.

## What It Does

`aha` manages a local `.aha/` directory:

```text
.aha/
  config.json
  workspaces/
    ws-001.json
  runs/
    <run-id>/
      plan.json
      events.jsonl
      sessions/
        main.json
      prompts/
      inbox/
      results/
      logs/
      runtime/
      tasks/
        task-001/
          task.json
          messages.jsonl
          sessions/
            main.json
          rounds/
            round-001/
              round.json
              final.md
              final.meta.json
      merged-report.md
```

Supported commands:

```bash
aha init
aha plan "goal" --agents 4
aha run <run-id>
aha status <run-id>
aha task add <run-id> "extra task" --workspace-path /path/to/project --max-sub-agents 3
aha task proxy <run-id> task-001 --enable-proxy --http-proxy http://127.0.0.1:7890
aha agent add <run-id> task-001 --backend codex
aha agent set <run-id> task-001 main --sandbox workspace-write --approval never
aha session list <run-id> --task-id task-001
aha workspace add /path/to/project --name firmware
aha workspace list
aha watch <run-id>
aha send <run-id> task-001 "please inspect the package flow"
aha chat <run-id> task-001
aha auto-reply <run-id> main
aha codex-chat <run-id> main
aha serve <run-id>
aha ui <run-id>
aha run export <run-id> --output run.tar.gz --no-logs
aha run import run.tar.gz --run-id restored-demo
aha package onebin --output dist/aha
aha commit --type feat --scope web --summary "add lazy loading"
aha commit-check .git/COMMIT_EDITMSG
aha collect <run-id>
aha merge <run-id>
aha list
```

Optional dashboard workspace discovery can be configured in `.aha/config.json`:

```json
{
  "workspace_roots": ["/path/to/projects"]
}
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

Every task has one `task-main`. Sub-agents are optional and can be 0..n per task. User-facing task setup uses a collaboration mode (`auto`, `solo`, `pair`, or `team`), which AHA maps to delegation policy and sub-agent limits internally. Backend sessions are scoped to task/agent boundaries so task context does not bleed across unrelated work.

Creating a task automatically dispatches an AHA-mode assignment to that task's `task-main`. The assignment asks `task-main` to judge complexity and, if needed, return structured `spawn_sub` actions. AHA executes those actions by creating sub-agents scoped to the current task.

Useful commands:

```bash
aha task add <run-id> "Analyze package rules" \
  --workspace-path /home/kaikai/kk-workspace/hl_project/fw_omni_builder \
  --backend codex \
  --model gpt-5.2 \
  --collaboration-mode auto \
  --max-sub-agents 3
aha task list <run-id>
aha task show <run-id> task-001
aha task final <run-id> task-001
aha task reopen <run-id> task-001
aha task proxy <run-id> task-001 \
  --enable-proxy \
  --http-proxy http://127.0.0.1:7890 \
  --https-proxy http://127.0.0.1:7890 \
  --no-proxy localhost,127.0.0.1,::1

aha agent add <run-id> task-001 --role sub --backend codex
aha agent set <run-id> task-001 main --enable-proxy
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

For task-level parallelism, use task-scoped Codex chat workers. They share the same logical inbox but keep independent offsets and only process messages for their task:

```bash
aha codex-chat <run-id> main --task-id task-001
aha codex-chat <run-id> main --task-id task-002
```

This keeps each `task_id + agent_id` serial while allowing different tasks to run at the same time.

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

Send a message to a named inbox target:

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

The CLI `send` and `chat` commands are simple inbox tools. The browser dashboard uses the newer task-scoped route by sending messages to an agent target such as `main` or `sub-001` while carrying `task_id`, `from_agent`, and `to_agent`.

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

Reconnect with a saved event cursor to replay missed events before tailing live updates:

```text
ws://127.0.0.1:8765?last_event_id=<event-id>
ws://127.0.0.1:8765?after_event_id=<event-id>
```

Without a cursor, the WebSocket starts from the current tail. Invalid cursors return HTTP 400 and close before upgrade.

The WebSocket server sends JSON messages:

```json
{"type":"status","data":{}}
{"type":"event","data":{"event_id":123}}
```

Clients can send:

```json
{"type":"send","target":"main","task_id":"task-001","message":"please check logs","sender":"web"}
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

The dashboard prefers the same-origin WebSocket endpoint:

```text
GET /ws?run_id=<run-id>&last_event_id=<event-id>
```

It stores the latest `event_id` in browser local storage, reconnects with that cursor after refresh or disconnect, and falls back to `/api/events` polling when WebSocket is unavailable. Use `?transport=polling` or `?ws=0` to force HTTP polling during debugging.

The dashboard uses local HTTP endpoints:

```text
GET  /api/bootstrap
GET  /api/runs
POST /api/runs
PATCH /api/runs/<run-id>
GET  /api/run/export?run_id=<run-id>&no_logs=1
POST /api/run/import
GET  /api/status
GET  /api/backends
GET  /api/models?backend=codex
GET  /api/workspaces
POST /api/workspaces
GET  /api/events?offset=<byte-offset>
GET  /api/events?last_event_id=<event-id>
GET  /api/conversation-events?task_id=<task-id>&target=<agent-id>&limit=50&before_offset=<byte-offset>
GET  /api/task/<task-id>
GET  /api/task/<task-id>/logs?limit=200&before_offset=<byte-offset>&source=auto|file|events
GET  /api/task/<task-id>/final
GET  /api/task/<task-id>/context
GET  /api/backend?target=<agent-id>&task_id=<task-id>
POST /api/task/<task-id>/final
POST /api/task/<task-id>/reopen
POST /api/task/<task-id>/hide
POST /api/task/<task-id>/restore
POST /api/task/<task-id>/delete
POST /api/task/<task-id>/proxy
POST /api/task-config
POST /api/tasks
POST /api/agents
POST /api/agent-config
POST /api/send
```

There is no explicit HTTP start/stop backend endpoint yet. `POST /api/send` autostarts a stopped Codex backend for the addressed task agent when possible.

Task proxy configuration lives on the task (`http_proxy`, `https_proxy`, `no_proxy`, and the default enable flag). Agents only store a `proxy_enabled` switch. When an enabled Codex agent runs, AHA injects `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` plus their lowercase variants into the backend command environment; disabled agents have those proxy variables removed from the child environment.

It shows a task list on the left, a task workspace in the center, and task agents on the right. Selecting a task opens that task's conversation, result, logs, and prompt context.

```text
left: task list and task creation with workspace/backend/model/collaboration mode
center: conversation/result/logs/context
right: task-main/sub agents and backend session details
```

Messages still use JSONL inbox/event files, but browser messages carry `task_id`, `from_agent`, and `to_agent` so the backend can keep task conversations separated.

The task form dispatches the assignment immediately. Users should not need to repeat the task in the conversation box after creation.

When AHA starts without `.aha/config.json`, the dashboard shows a bootstrap form for the AHA config file. Its Core Settings section contains only Default backend and Task concurrency; the backend selector exposes `codex` and `claude`. The same form can set workspace roots, Codex `bin` and `model` defaults, and the Claude `bin`. Claude env is configured as named groups with fixed Anthropic fields (`ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`), while the active env selector also offers Claude official mode to avoid applying env values. Runner command, default mode, and context window overrides are not part of the init UI. After initialization, an empty dashboard shows a First Run form with only Run name; task creation stays in the New Task flow. The Run menu exposes Settings to edit the same AHA config defaults later. When creating a Claude task, a configured active env group supplies the model through `ANTHROPIC_MODEL`; if no env is configured, the task form falls back to a Claude model selector.

## Run Import And Export

Export one run as a tar archive:

```bash
aha run export <run-id> -o run.tar.gz
aha run export <run-id> -o run.tar.gz --no-logs
```

Import creates a new run id by default:

```bash
aha run import run.tar.gz
aha run import run.tar.gz --run-id restored-demo
aha run import run.tar.gz --preserve-id --force
```

The archive includes `aha-run-manifest.json` plus the run directory under `run/`. Runtime files are excluded, log files can be excluded with `--no-logs`, proxy fields are redacted, and backend session ids are cleared and marked as imported. The browser dashboard exposes the same flow through `/api/run/export` and `/api/run/import`.

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

## Commit Message Policy

AHA commits use a Conventional Commit subject plus a compact generator trailer:

```text
feat(web): add lazy loading for logs

Generated-by: AHA Codex GPT-5.5
```

Prefer `aha commit` over raw `git commit` so the message is generated consistently. Task, agent, and scope tracking stays in the AHA journal instead of the Git commit body:

```bash
aha commit \
  --type feat \
  --scope web \
  --summary "add lazy loading for logs" \
  --add README.md src/aha_cli
```

Validate a commit message file with:

```bash
aha commit-check .git/COMMIT_EDITMSG
```

Enable the repository hook locally:

```bash
git config core.hooksPath .githooks
```

## Single-File Executable

Build a one-bin executable zipapp:

```bash
python3 -m aha_cli package onebin --output dist/aha
```

From a source checkout, the wrapper script does the same thing without installing the package:

```bash
python3 scripts/build_onebin.py --output dist/aha
```

Run it directly on a machine with Python 3.10+:

```bash
./dist/aha --help
./dist/aha init --portable
./dist/aha ui <run-id> --host 127.0.0.1 --port 8766
```

The artifact includes the CLI modules and dashboard static files. It still uses the local filesystem for `.aha/` data and external agent backends such as `codex`.

When the one-bin dashboard autostarts a backend, AHA launches the child backend through the same one-bin artifact instead of requiring `python -m aha_cli` to be importable on the target machine. External backend commands such as `codex` still need to be installed and authenticated separately.

Install the one-bin artifact into `~/.local/bin/aha` and enable the dashboard as a user systemd service:

```bash
scripts/install_user_service.sh
```

By default the service runs `aha --home ~/.aha ui --host 0.0.0.0 --port 8788`. The script enables `aha.service`, starts or restarts it immediately, and tries to enable user lingering for boot-time startup. Override the defaults when needed:

```bash
scripts/install_user_service.sh --port 8788 --run-id <run-id>
systemctl --user status aha.service
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
