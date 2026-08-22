# AHA Architecture

## Product Shape

Web is the core entry point. CLI remains useful for automation, debugging, and local scripting.

The browser UI now has three operating modes:

```text
First Run bootstrap
  create .aha/config.json
  Core Settings: Default backend (codex/claude), Task concurrency, shared HTTP proxy values, and backend switches
  set workspace roots, Codex bin, and Claude bin
  choose default Codex/Claude model sources from official models or custom env groups
  add named Codex and Claude env groups for third-party compatible providers
  exclude runner command, default mode, and context window overrides from init UI

First Run
  create an initial run by Run name only
  leave task creation to the New Task flow

Settings
  edit the existing .aha/config.json from the Run menu
  reuse the bootstrap config form for future default changes
  keep Run/Task-specific switches outside global AHA config

Run workspace
  switch between local runs
  rename the current run
  create and manage tasks
  edit task main/sub/host backend/model/sandbox/approval/proxy in one agent config editor
  reset and hand off sessions when backend or model changes
  save runtime-only startup settings for next start or save and restart the backend
  select Codex/Claude official models or custom env-group models from one Model control
  chat with task agents
  inspect results, logs, context, sessions, and backend runtime
  import or export run archives
```

The orchestration hierarchy is:

```text
Run
  run-main
  Task task-001
    task-main
    sub-001
    sub-002
  Task task-002
    task-main
```

Intended future role: `run-main` decides whether work is simple enough to handle directly, should become one task, or should be split into multiple tasks. Today, `run-main` is only a reserved identity. `task-main` owns local task context. Sub-agents execute bounded work inside one task.

Current implementation note: `run-main` is reserved, not active. The plan stores a `main_agent` and a run-scoped session for future compatibility, but AHA does not currently dispatch prompts to a run-level agent. The active team unit today is one task: `task-main` owns the task outcome and may request sub-agents for bounded workstreams. AHA itself currently performs the run-level orchestration: task creation, routing, status, backend lifecycle, and result collection.

Activate `run-main` only when the product needs a real project-manager role that can decompose a user goal into multiple tasks, monitor task-main progress, manage cross-task dependencies, and produce a run-level final answer from task finals. Until then, avoid adding behavior that makes users think run-main is already participating.

Task creation is an execution event, not just metadata insertion. When a task is created, AHA writes an AHA-mode assignment to that task's `task-main` inbox. The task-main may answer directly or return structured actions such as `spawn_sub`.

## Module Boundaries

```text
domain/       object construction and protocol defaults
store/        filesystem persistence and compatibility
services/     use cases and long-running loops
backends/     agent providers and runner adapters
web/          HTTP API and browser UI
websocket/    low-level WebSocket stream
cli.py        argparse only
```

Important service responsibilities:

```text
services/backend_runtime.py  managed backend processes and runtime locks
services/agent_backend_switch.py
                             backend switch, session reset, handoff, restart
services/chat.py             backend chat loop and task finalization handling
services/orchestrator.py     AHA action execution and sub-agent coordination
services/proxy.py            core proxy normalization and backend env injection
services/run_archive.py      run import/export archive format
services/onebin.py           single-file zipapp packaging
```

## Context Ownership

AHA owns context assembly. Backend sessions preserve local continuity but do not define task boundaries.

Context layers:

```text
Run summary
Task summary
Agent/session summary
Recent messages
Selected artifacts
Current user message
```

Each task also records a `workspace_path`. Backend agents execute from that workspace when starting a new scoped session, so task context points at the project being worked on rather than the AHA tool repository.

Core proxy values (`HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`) are stored once in `.aha/config.json` under the top-level `proxy` object. `codex.proxy.enabled` and `claude.proxy.enabled` contain only each backend's default switch. `knowledge.git.proxy_enabled` independently applies the same shared values to Knowledge Git remote commands. Tasks and agents store their own `proxy_enabled` switches. Assisted-supervision hosts keep `host_proxy_enabled` separately from the task default for main and future sub-agents. Backend launches and per-turn executions apply the shared values only when the selected task/agent switch is enabled. Existing backend-specific proxy values are migrated in memory, preferring the configured default backend, while old run/task fields remain archive/runtime compatibility fallbacks.

Agent `sandbox`, `approval`, and `proxy_enabled` are backend startup settings. Updating them does not mutate a running child process. The UI therefore lets users either save the new value for the next backend start or save and restart the current backend immediately.

If run-main is activated later, it should work from summaries and decision records, not every task's full message history.

## Persistence Model

The active data root is an AHA home, not necessarily the repository root:

```text
$AHA_HOME
repo/.aha when initialized with --portable
~/.aha by default
```

A run is persisted as append-only event and message logs plus JSON snapshots:

```text
runs/<run-id>/
  plan.json
  plan.json.bak
  events.jsonl
  inbox/<agent-id>.jsonl
  sessions/main.json
  recovery/
  runtime/
  tasks/<task-id>/
    task.json
    messages.jsonl
    sessions/<agent-id>.json
    rounds/<round-id>/round.json
```

Plan read-modify-write operations use both the host advisory lock and an
`O_EXCL` sidecar lock so Windows and WSL writers serialize through the same
directory entry. Each replacement verifies the persisted JSON and retains the
previous valid snapshot as `plan.json.bak`.

Run discovery scans run directories rather than only `*/plan.json`. Missing or
invalid plans are restored from the backup when possible; otherwise AHA
reconstructs the plan from `tasks/*/task.json` plus durable run events. Empty
runs can be reconstructed from their `plan_created` event alone. Invalid and
reconstructed evidence is kept under `recovery/`.

`runtime/` contains process locks, offsets, and backend state. It is intentionally local-only and is excluded from run archives. Plan backup and recovery sidecars are also excluded from portable run archives.

Task finals are lifecycle artifacts, not just the latest result file. A finalized round writes:

```text
tasks/<task-id>/rounds/<round-id>/final.md
tasks/<task-id>/rounds/<round-id>/final.meta.json
```

Reopening a finalized task starts the next `round-NNN` and preserves the previous final.

## Backend Model

All backends are addressed through a registry. A backend may be stateless or session-capable. If a backend cannot resume sessions, AHA still keeps the logical session scope and falls back to fresh calls.

Agent backends are valid choices for task-main and sub-agent sessions:

```text
stub
codex
claude
opencode (experimental)
```

Runner backends execute tasks as batch jobs and are not valid task-main or sub-agent choices:

```text
command
```

Managed chat backends are registered through the internal plugin contract in
`backends/plugin.py`; architecture and compatibility rules are documented in
`docs/backend-plugin-architecture.md`. They are started through
`services/backend_runtime.py`. In source checkouts the runtime launches the
plugin-declared chat command, such as:

```text
python -m aha_cli codex-chat ...
python -m aha_cli claude-chat ...
python -m aha_cli opencode-chat ...
```

In a one-bin zipapp it launches the current one-bin artifact instead, so a packaged dashboard does not require `aha_cli` to be installed as an importable Python module. External backend CLIs such as `codex` and `claude` are still resolved from the target machine.

Codex and Claude use the same AHA task/session model. Their Model selectors can point at an official model or at a custom env group. Env-group selections are stored as `env:<group-name>`. Codex env groups target OpenAI-compatible Responses providers: AHA passes the selected group's `OPENAI_MODEL` to Codex, adds a temporary Codex `model_provider` override for `OPENAI_BASE_URL`, and uses `CODEX_WIRE_API=responses` plus `CODEX_ENV_KEY` for provider-specific authentication. Chat Completions-only endpoints are not supported by current Codex CLI provider config. Claude env groups inject `ANTHROPIC_*` / `CLAUDE_*` values and launch Claude without a CLI `--model` argument, so `ANTHROPIC_MODEL` is the effective model. Secrets must not be written to task journals, exported documentation, or user-visible logs.

Configured Codex context windows use two compatibility paths. When the
installed Codex profile contains a complete, schema-compatible
`~/.codex/models_cache.json` entry, AHA clones that full metadata into its
provider-scoped `AHA_HOME/codex-models.json` and changes only the model identity
and window. On a fresh installation that has not logged in or populated the
cache, AHA does not generate a partial model catalog; it passes
`model_context_window` directly and calculates
`model_auto_compact_token_limit` from the configured window and threshold.
Thus an AHA Env Group/API-key model does not require an unrelated Codex login,
while existing complete-catalog behavior and official-model authentication
remain unchanged.

Claude's streamed `assistant.text` blocks are public task-scoped Agent updates;
`thinking` stays private and `tool_use/tool_result` remain command activity.
The prompt context asks Claude to emit concise text blocks between work stages,
and a managed backend's `aha send` fallback inherits `AHA_TASK_ID` and
`AHA_AGENT_ID` so explicitly emitted messages cannot silently fall out of the
Task filter.

Both backend runners treat Codex `turn.completed` and Claude `result` as trusted
native turn boundaries. After that boundary, stdout receives a short grace
period to close. If an agent-started long-running descendant inherited the
backend pipe and prevents EOF, AHA records `backend_completion_grace_exceeded`
and completes the logical turn instead of leaving the Task permanently
`running`. AHA stops only a still-running backend parent; it does not tree-kill
an already detached server. No timeout applies before the native completion
event, so active commands and model work retain their existing lifecycle.
Persistent servers, watchers, tunnels, and monitors can be started through
`aha managed-process start`. The CLI calls the loopback-only/authenticated
`/api/managed-processes` endpoint, so the long-lived AHA Web process performs
the spawn instead of the current Codex/Claude turn. Runtime JSON is stored under
`runs/<run-id>/runtime/`, output under `runs/<run-id>/logs/`, and start/status/
list/stop are scoped by task and agent. Commands use an argv array without an
implicit shell, cwd is constrained to the task workspace, and Web shutdown
or a terminal task stops the owned process tree. This provides turn persistence without creating
system orphans; services that must survive an AHA Web restart still belong in
an external service host.

OpenCode is an experimental built-in plugin. Each turn starts a loopback-only
OpenCode Server with generated Basic auth, reuses the OpenCode session id stored
in the AHA session, subscribes to SSE for live text/tool events, and treats the
REST message response as the authoritative completion/final-usage value. AHA
generates session permission rules, disables OpenCode native task subagents,
and reports its sandbox equivalence as tool-policy rather than OS-enforced.
OpenCode models may come from its native catalog or from the same AHA
`providers/configured_models` used by Codex and Claude. AHA Provider credentials
are injected into the isolated OpenCode Server through temporary environment
variables; generated OpenCode config contains only environment references.
On Windows, Detect Models prefers a Windows OpenCode binary and falls back to a
native WSL OpenCode helper when Windows has none. WSL UNC workspaces execute in
their owning distro; Windows workspaces execute on Windows unless
`opencode.wsl_distro` explicitly selects WSL.

Changing a task `main`, `sub-*`, or assisted-supervision `host` backend or model is a lifecycle operation. AHA stops an active old backend, builds a compact handoff summary, archives and resets the backend session id, updates the agent backend/model, appends a handoff message for the new backend, and restarts the new backend when the old one was active. The supervision policy stores the host's selected `host_model`, so a Codex/Claude host does not have to inherit the task-main model. This keeps the logical AHA agent identity stable while making the backend session boundary explicit.

## Distribution And Portability

Run archive import/export is handled by `services/run_archive.py`.

Export:

```text
include run metadata, events, messages, tasks, sessions, prompts, results, and optionally logs
exclude runtime state and transient lock/tmp files
redact proxy values
clear backend_session_id while preserving imported_backend_session_id
```

Import:

```text
safe-extract the archive
assign a new run id by default
rewrite run_id and session scope references
mark sessions as imported
append a run_imported event
```

`aha package onebin` builds a Python zipapp containing `aha_cli` and `web/static`. The artifact still stores data in `.aha/` or the selected `--home`, and still depends on external backend CLIs such as `codex` or `claude`.

On Windows, `scripts/install_windows.ps1` defaults to a `Full` one-command setup: it creates or repairs the dedicated Python environment, verifies the onebin against `SHA256SUMS`, installs the serial/Feishu Python modules, installs Git, and ensures a selected Codex or Claude backend without performing third-party login. The large Playwright/Chromium payload is opt-in through `-WithBrowser` or `-Modules Browser`; `-SkipBrowserDownload` can install only the Playwright Python module. `Minimal` retains the core-only path, while `Offline` accepts a local onebin, wheelhouse, optional Chromium payload, and optional Python installer and never falls through to `winget`, `npm`, or Web downloads. Dependency results and remaining user actions are recorded in `%LOCALAPPDATA%\AHA\install-report.json`; optional failures preserve the installed core unless `-StrictModules` is requested. The installer starts `aha tray` through `pythonw.exe`, writes the initial non-secret tray configuration, extracts the packaged multi-size AHA ICO, and creates a current-user Start Menu shortcut unless `-NoShortcut` is set. Without pre-login startup, the tray process supervises the `aha ui` child as before. With `-EnableStartup`, the installer creates the root-level `\AHA Web` Task Scheduler task with an `AtStartup` trigger and a limited current-user password logon token; the task runs a generated `start-web.ps1` launcher that reads `tray.json` and starts the headless Web service before sign-in. The packaged onebin also contains `assets/configure_windows_startup.ps1`: the tray's “无需解锁开机启动” menu materializes this helper, launches it through `ShellExecuteExW` with the `runas` verb, and lets the elevated helper request the current-user credential before creating or deleting the same task. This lets an existing HKCU-only installation migrate after a normal Web/onebin upgrade without separately downloading the installer. This deliberately avoids SYSTEM so the service retains access to the selected user `AHA_HOME`, user configuration, credentials, and network identity. Task Scheduler stores the supplied password as an LSA-protected task secret. HKCU Run is then used only to display the interactive tray after sign-in; the tray attaches to the scheduled Web service, can restart it through `schtasks.exe`, and does not stop it when the icon exits. Existing scheduled tasks are updated in place, an explicit installer credential refresh can replace a stale password, and `-Uninstall` idempotently removes the task, login startup, shortcut, and install directory while retaining `AHA_HOME`. The supervised local Web subtree is assigned to a kill-on-close Windows Job Object and explicitly tree-killed during restart or exit, so a venv `pythonw.exe` redirector, its base interpreter, and Web self-replacements cannot retain the listening port. Settings can change AHA home, bind address, port, and Web token, then transactionally restart either the attached task or local Web child and roll back when the new service fails startup; when login startup is enabled, its tray command is rewritten with the new settings in either service mode. Non-secret launch settings live in `%LOCALAPPDATA%\AHA\tray.json`; the token remains in `<AHA_HOME>\web-token`. Web-triggered helper and background processes share `platform.hidden_subprocess_kwargs()`, which adds `CREATE_NO_WINDOW` on Windows so Git status checks, backend discovery, usage refreshes, diagnostics, and managed children do not flash console windows. Linux continues to use the user systemd installers.

## Run Management And Onebin Smoke

AHA keeps run management operations conservative:

```text
aha runs diagnose --json
aha runs cleanup --dry-run --json
aha runs cleanup --apply
aha runs lifecycle <run-id> hidden|archived|active
aha runs delete <run-id> [--force]
aha runs retention <run-id> --json
aha runs retention <run-id> --max-candidate-bytes BYTES --apply-if-over-limit
aha runs retention-policy --max-candidate-bytes BYTES --json
aha runs retention-policy --max-candidate-bytes BYTES --write-report --json
aha runs retention-policy --max-candidate-bytes BYTES --apply-if-over-limit
aha runs retention <run-id> --apply [--archive-dir PATH]
aha runs retention <run-id> --apply --force [--archive-dir PATH]
aha runs retention-archive list [<run-id>] [--archive-dir PATH]
aha runs retention-archive inspect PATH
aha runs retention-archive restore PATH [--run-id RUN] [--force]
aha runs recover <run-id> --json
aha runs recover <run-id> --task-id TASK --agent-id AGENT --apply
aha runs recover <run-id> --task-id TASK --agent-id AGENT --apply --restart-backend
```

`runs diagnose` is read-only. It reports visible runs, heartbeat-active runs,
cleanup protection reasons, best-effort AHA service/process/listener probes, and
stale running-agent candidates. It does not call recovery APIs, stop services, or
rewrite run/task/agent state.

`runs cleanup` defaults to dry-run/list mode. It only deletes with explicit
`--apply`, and still protects the current run, heartbeat-active runs, and normal
non-temporary user runs. Cleanup scan roots are guarded: `--tmp-root` must be
inside the system temporary directory unless `--allow-non-temp-root` is
explicit, and symlink run directories, symlink `.aha` homes, and configured
portable `.aha` homes without temporary runs are protected.

`runs lifecycle` writes only soft lifecycle state. `hidden` and `archived` runs
remain on disk and can be restored with `active`. The guarded action refuses the
current run, heartbeat-active runs, and missing runs.

`runs delete` physically removes one non-current run directory. It refuses the
current run in all modes and refuses heartbeat-active runs unless `--force` is
passed. This is intentionally separate from `cleanup`, which remains conservative
and temporary-run oriented.

`runs retention` defaults to read-only dry-run reporting. It reports run storage
usage by top-level group, age bucket, largest files, and explicit retention
candidates. The default candidate policy only includes `logs/` and `prompts/`;
`chat/` requires `--include-chat`, while `runtime/`, `results/`, `tasks/`,
`sessions/`, `inbox/`, and top-level metadata are preserved by default.
Optional policy thresholds (`--max-total-bytes`, `--max-candidate-bytes`, and
`--min-candidate-files`) add machine-readable alerts, and
`--apply-if-over-limit` turns those alerts into guarded archive creation without
deleting originals unless `--force` is also present.

`runs retention-policy` applies the same thresholds across every run with a
dry-run-first aggregate report. It lists per-run alerts, candidate totals, and
apply guards. With `--apply-if-over-limit`, it only calls guarded retention
apply for runs that are over threshold, have candidates, are not the current
run, and do not have an active heartbeat; protected runs remain report-only.
`--write-report` persists that aggregate report under
`AHA_HOME/reports/retention-policy/` and updates `latest.json`. The Web UI
starts a read-only retention-policy reporter after initialization and writes the
same report on the configured `retention_policy.report_interval_seconds`
schedule. The scheduled reporter never passes apply flags, so it cannot delete
or compact files.

`runs retention --apply` writes a compressed retention archive and keeps the
original files. Deleting originals requires both `--apply` and `--force`, and
only after the archive is written successfully. Apply mode refuses the current
run and heartbeat-active runs in all modes, so the command cannot compact the
run currently serving the UI or an open browser heartbeat.

`runs retention-archive` is the recovery side of that compaction path. `list`
finds retention archives in a run's default `retention/` directory or an
explicit archive directory, `inspect` validates the manifest without extracting
files, and `restore` writes manifest-listed members back into the source run or
an explicit `--run-id`. Restore refuses the current run and heartbeat-active
runs, skips existing files by default, and overwrites only with `--force`.

`runs recover` defaults to dry-run stale runtime detection. It reports agents
whose task and AHA agent state are still `running` while their backend process
status is `stopped`. Apply mode requires exact `--task-id` and `--agent-id`
targeting, rechecks the backend before mutation, marks that agent
`interrupted`, records recovery context, and moves the task back to
`awaiting_user` when no other agent is still running. With `--restart-backend`,
recovery also enqueues a resume prompt containing the recovery context, moves
the agent back to `pending`, and starts the matching chat backend.

The Web API exposes the same read-only visibility through
`GET /api/runs/<run-id>/retention`, `GET /api/runs/<run-id>/recovery`, and
`GET /api/runs/<run-id>/maintenance`. Guarded `POST` actions on
`/api/runs/<run-id>/retention`, `/api/runs/<run-id>/recovery`, and
`/api/runs/<run-id>/retention-archive/restore` require explicit confirmation
tokens and reuse the CLI safety checks for current runs, heartbeat-active runs,
and exact stale-recovery targets. Run-scoped archive inspection and restore use
`GET /api/runs/<run-id>/retention-archives`,
`GET /api/runs/<run-id>/retention-archives/<archive-name>`, and
`POST /api/runs/<run-id>/retention-archives/<archive-name>/restore`; these
routes accept archive basenames only and reject source-run mismatches.

`GET /api/access-control` reports token-auth mode, the request Host header, and
the server's configured bind host/port when the UI process knows it. The browser
can show the current access address from `window.location.host` alongside the
actual bind address, so side-by-side source and onebin dashboards do not display
the same stale endpoint. It can also surface local vs network-exposed bind risk
without storing secrets.
Dashboard state that should survive browser/device changes lives outside the
plan protocol. `.aha/ui_state.json` stores `last_selected_run_id`, while
`.aha/runs/<run-id>/ui_state.json` stores that run's `last_selected_task_id`.
`GET /api/ui-state` returns the global state, and `GET
/api/ui-state?run_id=<run-id>` includes the run-scoped task selection.
`PATCH /api/ui-state` updates either field, using the same run-scoped `run_id`
query or JSON body convention for task selection that the rest of the UI APIs
use. The browser still keeps a localStorage fallback for offline or legacy
task state, but URL run/task parameters remain the highest-priority selection.

When token auth is enabled, `GET /` and `GET /static/*` remain readable so the
browser can render the login shell. Data APIs and WebSocket handshakes still
require the token. `POST /api/login` validates the configured Web token and
sets the same HttpOnly cookie as the `?token=...` bootstrap path; `POST
/api/logout` clears that cookie. This is intentionally a single-token local
tool flow, not a multi-user account, role, or permission system.

Realtime debug persistence is plan-backed. WebSocket heartbeat logs, task
messaging debug logs, and browser `/api/debug/realtime` telemetry all use the
same writer, which only appends `logs/realtime-debug.log` when
`runs/<id>/plan.json` exists. Missing, deleted, or plan-less run ids still print
process diagnostics, but they do not recreate `runs/<id>/` directories.

`scripts/smoke_onebin_cli.py` is the release-facing CLI smoke for packaged
artifacts. It builds or reuses the onebin, then runs help, portable init,
diagnose JSON, cleanup dry-run JSON, retention dry-run/apply/force JSON,
retention-policy persisted report JSON, retention archive list/inspect/restore
JSON, recover dry-run JSON, and run delete JSON using temporary `HOME`, temporary
portable `.aha`, and a temporary cleanup scan root.
The smoke must not depend on the developer's real `~/.aha`, an open 8788 page,
systemd, or existing user runs. On success it deletes the temporary run it
created before returning.

`scripts/smoke_dual_ui_homes.py` starts a source UI and a onebin UI on temporary
ports with different AHA homes, then checks each `/api/bootstrap` and
`/api/health` response. It guards against the common local-development failure
mode where source and onebin instances accidentally share the same run store,
and deletes both temporary smoke runs before returning.

`scripts/smoke_playwright_ui.py` is an optional browser interaction smoke. It
uses Python Playwright and Chromium when available, opens a token-protected
temporary source UI, verifies bootstrap rendering, task-create modal behavior,
run selection, maintenance refresh, and console errors, then deletes the smoke
run. If Playwright or Chromium is unavailable it exits successfully with
`status=skipped` unless `--require-playwright` is passed.

For persistent release installs, `scripts/install_user_service.sh` downloads the
published onebin artifact by default and installs `aha.service` as a user
systemd service. The default release source is
`https://github.com/ChinaKai/AHA/releases/latest/download/aha`; `--repo`,
`--version`, `--asset-name`, `--download-url`, and `--artifact` can override the
source. `--build-from-source` keeps the legacy source-checkout build path
explicit for local development. The generated unit records `AHA_INSTALL_BIN`,
`AHA_SERVICE_NAME`, and release metadata so Web-triggered upgrades can run from
the installed onebin instead of locating a source checkout.

For persistent local development, `scripts/install_source_user_service.sh`
installs `aha-src.service` as a user systemd service. Its defaults mirror the
source checkout convention: `PYTHONPATH=repo/src`, working directory set to the
repo root, `--home repo/.aha`, and port 8766. This keeps source UI state separate
from the release onebin user service, which normally uses `~/.aha` on port 8788.

Both service installers support `--dry-run`. Dry-run mode prints the generated
unit and install plan without downloading/building a onebin, writing
`~/.config/systemd/user`, or calling `systemctl`/`loginctl`, so automated tests
can verify service content without touching the developer's real user services.
`scripts/smoke_service_installers.py` runs those dry-runs under temporary homes
and asserts the generated release/source units, health check URLs, entrypoint
version validation, token-file auth wiring, release metadata, and no-write
behavior.
`scripts/preflight_service_upgrade.py` is the release-machine preflight wrapper:
it validates the source entrypoint, optionally builds and checks a temporary
onebin, reuses the installer dry-run smoke, and asserts the current user's
`~/.aha/runs` set did not change.

The Web service exposes `GET /api/health` as a run-independent readiness check.
It returns `ok`, `aha_home`, `aha_version`, initialization state, bind metadata,
and the selected default run id when one is available. It also reports
`auth_required` so service monitors can distinguish protected dashboards. It
intentionally avoids task data, backend status, and secrets. The service
installers use this endpoint only after a real restart to verify that systemd is
serving the expected AHA home and, when a version is known, the expected AHA
build.

Onebin service updates validate the downloaded or copied release artifact with
`aha --version` before writing/restarting the service. Source service updates
validate `python -m aha_cli --version`. Both installers generate or reuse
`AHA_HOME/web-token` and pass it to `aha ui --auth-token-file` by default; dry
runs print the token file path but do not create it. These checks can be disabled
with `--skip-upgrade-validation` for onebin or `--skip-version-validation` for
source, and the post-restart health poll can be disabled with
`--no-health-check`.

Opening the Web upgrade panel performs no remote request. The user explicitly
clicks `Check version` to call `GET /api/web/upgrade/status`. Its per-upgrade
`Use proxy` switch applies the shared Core proxy to both that version check
and the confirmed install; turning it off strips inherited proxy environment
variables for those subprocesses. Its initial state follows the default backend's
proxy switch; `/api/bootstrap.web_upgrade` includes the local
`proxy_configured` and `proxy_enabled` capability fields required to render
that switch before any remote check. A check-only
`aha service upgrade-user` subprocess downloads and validates the configured
release without replacing the installed file, then reports the current and
latest versions plus the runtime platform. Version ordering compares release
semver first and build date second so a newer local build is not offered an
older release as an update.

Confirmed upgrades call `POST /api/web/upgrade` with
`{"confirm":"upgrade","proxy_enabled":<boolean>}`.
The current Python interpreter launches the extensionless onebin on both Linux
and Windows, atomically replaces `AHA_INSTALL_BIN` without invoking systemd,
then requests the same process-level restart used by Restart Web. `cmd_ui`
reloads the replaced zipapp with its original arguments. A Linux user systemd
unit continues tracking the process, while a console-started Windows instance
recovers without a platform-specific service manager. Source checkouts keep the
separate publish flow and are not part of the onebin Web upgrade path.

AHA Web UI is a local tool with optional token auth. `aha ui --auth-token` and
`aha ui --auth-token-file` protect the UI, APIs, and WebSocket while leaving
`/api/health` public for readiness. A request with `?token=...` sets an HttpOnly
cookie for browser sessions, and the frontend removes the URL token after load.
The CLI and service installers bind to `127.0.0.1` by default. Binding to
`0.0.0.0` without token auth is appropriate only for trusted hosts or controlled
networks. Personal machines, cloud hosts, and shared networks should keep the
loopback bind or use token auth plus an SSH tunnel, VPN, or authenticated TLS
reverse proxy for remote access.
