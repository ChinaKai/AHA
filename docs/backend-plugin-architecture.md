# AHA Backend Plugin Architecture

Status: implemented foundation; OpenCode backend is experimental

## 1. Purpose

AHA currently exposes a backend registry, but the execution path is still
implemented with Codex/Claude conditionals across the registry, chat loop,
runtime launcher, CLI parser, provider configuration, session inspection, and
context-pressure logic.

Adding another agent backend by copying those branches would increase coupling
and make lifecycle fixes diverge between providers. The backend layer should
become an internal plugin system before adding OpenCode.

The first implementation is an **internal built-in plugin architecture**. It
does not load arbitrary Python packages from user directories. External plugin
installation, signing, isolation, and compatibility negotiation are separate
product concerns and are not required to validate the design.

## Implementation status

Implemented on 2026-08-22:

- typed internal plugin contracts and deterministic registration
- plugin-backed compatibility facade for backend/model/reasoning APIs
- descriptor-driven process backend set, worker commands, binary flags, CLI
  chat parser generation, runner dispatch, proxy defaults, and WSL probing
- Codex and Claude model resolution and turn execution through plugins
- backend-specific optional turn preparation split from the generic chat loop
  into `services/backend_turn.py`
- generic fake plugin contract tests
- experimental OpenCode plugin with:
  - dynamic model catalog
  - loopback Server with per-turn Basic auth
  - persistent OpenCode session reuse across server restarts
  - REST message result as the authoritative completion value
  - SSE text/tool/error/compaction event translation
  - final-response event recovery and deduplication
  - AHA-generated session permission rules
  - WSL backend discovery
  - unified AHA `providers/configured_models` support with responses,
    chat-completions, and Anthropic Messages translation
  - generated OpenCode provider config references temporary environment
    variables; provider credentials are never copied into generated config,
    task state, command lines, or logs
  - canonical AHA token-usage normalization for input/output/reasoning/cache
  - plugin-owned SQLite session artifact inspection with session/message/part
    metadata; credential and message content are not exposed
- real local OpenCode validation:
  - two direct turns reused one OpenCode session
  - one complete AHA worker turn processed prompt, checkpoint, action JSON,
    session persistence, task state, message, usage, and final response

Current experimental limitations:

- OpenCode model discovery is based on its CLI catalog; an account/provider can
  still reject a catalog entry at execution time. The backend reports the
  server error and does not silently switch models.
- OpenCode Server is restarted for each AHA turn while the OpenCode session is
  reused. A later optimization may keep one task-scoped server alive.
- context-pressure inspection uses canonicalized OpenCode usage plus the exact
  AHA configured-model context window. The Session panel reads the AHA-scoped
  OpenCode SQLite store instead of treating the backend as a Codex/Claude JSONL.
- OpenCode native catalog and native credential-store models remain available,
  but catalog entries can differ from account availability. AHA Provider-backed
  models are authoritative when their `env:<binding>` selector is used.

## 2. Goals

- Keep AHA as the owner of task, agent, prompt, checkpoint, Git, supervision,
  action, and finalization semantics.
- Move backend-specific model selection, command construction, session
  handling, event translation, and turn execution behind a typed interface.
- Preserve all existing Codex and Claude behavior and public CLI compatibility.
- Make adding a fake backend in tests possible through registration alone.
- Add OpenCode as an experimental plugin without adding OpenCode branches to
  generic chat/runtime code.
- Allow a future ACP adapter to reuse the same AHA plugin contract.

## 3. Non-goals

- Dynamically importing arbitrary third-party Python code.
- Removing OpenCode's native credential store; AHA Provider configuration and
  native OpenCode configuration remain selectable sources.
- Exposing native backend subagents as AHA subagents.
- Replacing AHA action JSON, task coordination, managed processes, hardware,
  browser, Knowledge, or Git completion gates.
- Claiming identical security semantics when a backend only provides
  tool-policy permissions instead of an operating-system sandbox.
- Removing `codex-chat`, `claude-chat`, `codex-runner`, or `claude-runner`
  compatibility commands.

## 4. Current coupling

The principal hard-coded backend decisions are:

- `backends/registry.py`
  - static `BACKENDS`
  - Codex-only dynamic model catalog
  - backend-specific reasoning levels and model normalization
- `services/chat.py`
  - model/config resolution
  - Codex observe/headroom preparation
  - direct `run_codex_exec()` / `run_claude_exec()` dispatch
  - Codex-only runtime compaction checks
- `services/backend_runtime.py`
  - `PROCESS_AGENT_BACKENDS`
  - worker command names and binary flags
  - WSL backend binary selection
  - backend runtime/session inspection
- `services/run_tasks.py` and `cli.py`
  - runner command dispatch
- `cli_parser.py`
  - backend-specific worker/runner parsers
- `services/provider_config.py`
  - configured-model backend allow-list
- Web routes and static UI
  - backend choices come from the registry, but some defaults still assume
    Codex or Claude.

The generic chat loop is already responsible for the correct shared concerns:

- inbox consumption and checkpoint recovery
- AHA prompt construction and sticky fingerprints
- task/agent lifecycle
- proxy resolution
- action parsing and execution
- Git completion gates
- result/final persistence
- watchdog recovery

Those concerns remain outside plugins.

## 5. Package layout

```text
src/aha_cli/backends/
  plugin.py              typed plugin contracts and registration
  registry.py            compatibility facade over registered plugins
  codex.py               existing Codex implementation
  claude.py              existing Claude implementation
  builtin_plugins.py     Codex/Claude/Stub/Command plugin objects
  opencode.py            experimental OpenCode plugin
```

`registry.py` remains the public compatibility module used by existing code,
but it delegates to plugin objects.

## 6. Core types

### 6.1 Backend descriptor

```python
@dataclass(frozen=True)
class BackendDescriptor:
    name: str
    kind: Literal["agent", "runner"]
    label: str
    process_chat: bool
    chat_command: str | None
    runner_command: str | None
    config_key: str | None
    binary_option: str | None
    default_binary: str | None
    supports_sessions: bool
    supports_reasoning_effort: bool
    supports_runtime_context: bool
```

The descriptor is data only. CLI/Web option discovery must not execute a
backend.

### 6.2 Resolved turn

```python
@dataclass
class BackendResolvedTurn:
    requested_model: str | None
    command_model: str | None
    resolved_model: str | None
    reasoning_effort: str | None
    backend_config: dict
    proxy_env: dict[str, str] | None = None
    extras: dict[str, object] = field(default_factory=dict)
```

This replaces Codex/Claude model/config branches in `chat.py` and
`backend_runtime.py`.

### 6.3 Turn request/result

```python
@dataclass
class BackendTurnRequest:
    prompt: str
    cwd: Path
    output_file: Path
    events_file: Path | None
    run_id: str
    task_id: str | None
    source: str
    target: str | None
    session: dict
    sandbox: str
    approval: str
    extra_args: list[str]
    resolved: BackendResolvedTurn
    aha_home: Path
    config: dict


@dataclass
class BackendTurnResult:
    exit_code: int
    reply: str
    session: dict | None
```

The first migration may continue using the existing Codex/Claude event writers
inside `run_codex_exec()` and `run_claude_exec()`. A later cleanup can replace
those direct writes with a standard event sink without blocking pluginization.

## 7. Plugin interface

```python
class AgentBackendPlugin(Protocol):
    descriptor: BackendDescriptor

    def model_options(self, config: dict | None) -> list[dict]: ...
    def reasoning_effort_options(self) -> list[dict]: ...
    def normalize_reasoning_effort(self, value: object) -> str | None: ...
    def normalize_model_selector(
        self,
        value: object,
        config: dict | None,
    ) -> str | None: ...
    def resolve_turn(
        self,
        *,
        config: dict,
        model: str | None,
        reasoning_effort: str | None,
        task_scoped: bool,
    ) -> BackendResolvedTurn: ...
    def run_turn(self, request: BackendTurnRequest) -> BackendTurnResult: ...
    def runner_command(self, args, config: dict) -> str | None: ...
    def normalize_usage(self, usage: dict) -> dict: ...
    def session_artifact_info(
        self,
        *,
        aha_home: Path,
        run_id: str,
        task_id: str | None,
        target: str | None,
        session_id: str,
    ) -> dict: ...
    def session_resumable(self, session: dict, runtime: dict) -> bool: ...
    def inspect_runtime_context(self, context: BackendRuntimeContext) -> dict: ...
```

Optional behavior uses capabilities or default methods; generic code must not
use `hasattr()` as an implicit compatibility contract.

## 8. Registration

```python
register_backend(plugin)
get_backend_plugin(name)
agent_backend_plugins()
runner_backend_plugins()
```

Rules:

- duplicate names fail during import/test startup
- built-in registration order is deterministic
- descriptors are immutable
- registry responses are copied before returning to callers
- unavailable external binaries do not remove a backend choice; launch failure
  is reported through the normal backend lifecycle
- the deterministic stub remains available for tests

The initial registry imports only built-in plugin objects. Dynamic entry points
can be designed later without changing the plugin interface.

## 9. Generic execution flow

```text
AHA chat worker
  -> get_backend_plugin(task.backend)
  -> plugin.resolve_turn(...)
  -> update AHA session requested/resolved model
  -> construct BackendTurnRequest
  -> plugin.run_turn(request)
  -> save checkpoint/session
  -> run AHA actions, Git gate, status and finalization
```

Backend plugins cannot:

- advance inbox offsets
- mark tasks complete
- execute AHA coordination actions directly
- bypass checkpoint recovery
- write task finals
- change the task workspace

## 10. Worker process launch

The long-running AHA worker remains an AHA process. It consumes the inbox and
invokes a backend turn.

Phase 1 preserves:

```text
codex-chat
claude-chat
```

Both commands call one generic `agent_chat(..., backend_name=...)` path.
Runtime command construction moves from backend-name conditionals to descriptor
fields:

- `chat_command`
- `binary_option`
- `default_binary`
- `supports_no_json`

OpenCode may initially use `opencode-chat`. A later internal generic
`backend-chat --backend <name>` command can replace the aliases after run
archives and older onebins no longer need the old command names.

## 11. WSL and cross-platform behavior

- AHA Web and installed onebin remain authoritative on Windows.
- A WSL workspace prefers a native backend executable discovered inside the
  selected distro.
- OpenCode model discovery runs in the Windows Web control plane. It uses a
  Windows OpenCode executable when configured/available; otherwise it executes
  a hidden native helper through the authoritative onebin in a WSL distro with
  detected OpenCode. The credential is passed through stdin, never argv.
- WSL UNC workspaces always select the owning distro. Windows workspaces stay
  Windows-native by default; `opencode.wsl_distro` explicitly opts them into a
  selected WSL distro, where drive paths are mapped to `/mnt/<drive>/...`.
- The plugin descriptor identifies the configured binary and CLI flag; generic
  WSL launch code performs path mapping and environment scrubbing.
- Provider credentials and full Windows `PATH` are not forwarded through
  `wsl.exe`.
- Plugins may add explicitly declared WSL environment keys, but cannot replace
  AHA identity variables or proxy policy.

OpenCode validation starts in WSL because it is already installed there and
its recommended Windows workflow is WSL. Windows-native OpenCode support is a
separate acceptance target.

## 12. Permission and sandbox contract

AHA exposes:

- `read-only`
- `workspace-write`
- `danger-full-access`

Plugins return a permission mapping plus an equivalence level:

```python
Literal["os-enforced", "tool-policy", "unsupported"]
```

If a backend only supplies tool-policy enforcement, AHA must not represent it
as equivalent to an OS sandbox in diagnostics.

OpenCode policy for the first plugin:

- use an isolated AHA-owned OpenCode configuration
- disable native `task` subagents
- disable interactive `question` requests
- deny external directories by default
- avoid blanket `--auto` in production
- `read-only`: deny edit/write and unsafe shell commands
- `workspace-write`: allow workspace edits, deny external directories
- `danger-full-access`: allow tools subject to AHA approval policy

## 13. Session contract

AHA owns the logical session. Plugins own the backend session identifier.

```text
AHA session
  backend
  requested_model
  resolved_model
  backend_session_id
  runtime identity
  archived backend sessions
```

Plugin responsibilities:

- determine whether a backend session is resumable in the current runtime
- start a fresh backend session when the stored ID is unavailable
- update `backend_session_id` only from an authoritative backend response
- expose enough metadata for context pressure and session debug

Session artifacts are backend-specific. Codex and Claude currently expose
JSONL transcripts. AHA Provider-backed OpenCode exposes a task/agent-scoped
SQLite database under `runs/<run>/runtime/opencode/<scope>/data/opencode/`.
The OpenCode plugin returns only file metadata, message/part counts, model
identity, cumulative token counters, cost, and timestamps. It does not return
stored prompts, assistant text, tool payloads, or credentials.
- support abort/reset without deleting AHA history

## 14. Standard AHA backend events

Plugins translate native events to:

- `agent_thread`
- `agent_message`
- `agent_command_started`
- `agent_command_finished`
- `agent_usage`
- `agent_error`
- `agent_context_overflow`
- `backend_auto_context_compact`

Completion is a plugin-owned predicate, but final turn acceptance remains
generic:

- backend completion observed
- process/protocol result indicates success
- non-empty final reply, unless the operation explicitly allows empty output
- checkpoint persisted before actions are applied

## 15. OpenCode validation plugin

### 15.1 Transport

Production-oriented validation uses:

```text
opencode serve
  REST session APIs
  global SSE event stream
```

Do not depend solely on `opencode run --format json`; current OpenCode releases
have reported missing/silent JSON output and event-ordering issues for resumed
sessions.

ACP is a future alternative after its experimental status and resume behavior
are acceptable.

### 15.2 Lifecycle

- launch a loopback-only task-scoped OpenCode server
- use a generated per-task password
- create one OpenCode session per AHA agent
- persist the OpenCode session ID in `backend_session_id`
- send prompts asynchronously
- subscribe to SSE and filter by session
- on idle/completion, query authoritative session messages/status before
  accepting the turn
- abort the session when AHA stops the backend
- restart the server and resume the session when possible

### 15.3 Model catalog

OpenCode models use `provider/model`.

The plugin should query the OpenCode provider/model API and expose:

- provider/model identifier
- label
- context/output limits when available
- variants as AHA reasoning-effort options

OpenCode credential storage remains authoritative for the first version. AHA
does not copy credentials into task state or logs.

### 15.4 Native feature boundaries

- OpenCode native subagents are disabled
- OpenCode custom commands do not replace AHA commands
- AHA prompt/action protocol remains authoritative
- OpenCode plugins and project configuration are disabled or isolated for
  deterministic backend behavior
- file attachments are added only after text/tool/session reliability passes

## 16. Migration phases

### Phase A: contract and compatibility facade

- add typed plugin contracts and built-in registration
- make `registry.py` delegate metadata/model/effort queries
- add fake plugin registration tests
- no execution behavior changes

### Phase B: execution migration

- implement Codex and Claude plugin wrappers
- move turn configuration and dispatch out of `chat.py`
- make runtime worker command construction descriptor-driven
- keep old CLI commands
- preserve all existing session/runtime tests

### Phase C: runtime inspection

- move backend runtime context/session inspection behind optional plugin methods
- keep AHA context-pressure calculation generic
- eliminate Codex/Claude checks from generic runtime status where practical

### Phase D: OpenCode experimental plugin

- implement server lifecycle and event mapping
- add model/provider discovery
- add permission mapping
- run deterministic fake-server tests
- run an opt-in local smoke against a pinned OpenCode version

### Phase E: external plugin loading, optional

Only after the built-in contract has survived OpenCode:

- define package entry-point discovery
- declare AHA API compatibility versions
- isolate plugin import failures
- document trust and upgrade policy

## 17. Compatibility

- Existing run/task/session JSON remains valid.
- Existing backend values `codex`, `claude`, `stub`, and `command` remain
  unchanged.
- Existing CLI commands remain callable.
- Existing Codex/Claude config blocks and env groups remain unchanged.
- Existing backend runtime state using `codex-chat`/`claude-chat` remains
  readable.
- Unknown backend names remain validation errors.
- Run archive import does not require the selected backend binary to exist.

## 18. Test strategy

### Contract tests

- duplicate registration rejected
- backend metadata copied, not shared mutably
- fake agent plugin appears in backend/model APIs
- fake runner plugin is excluded from task-agent choices
- plugin default methods are deterministic

### Codex/Claude parity

- command lines unchanged
- model and reasoning selection unchanged
- env groups unchanged
- session IDs and completion events unchanged
- WSL launch and PATH scrubbing unchanged
- context pressure unchanged

### Generic chat/runtime

- adding a fake process plugin requires no backend-name branch
- checkpoint recovery works with the fake plugin
- backend switch/handoff works across plugins
- watchdog detects stopped plugin workers
- unsupported runtime-context capability returns unknown, not a crash

### OpenCode

- server startup/health/auth
- session create/resume/abort
- SSE reconnect and deduplication
- final reply recovery after missing/out-of-order stream events
- tool start/finish mapping
- usage/context extraction
- permission mapping
- server restart recovery

## 19. Acceptance criteria

Pluginization is complete when:

- Codex and Claude pass the full existing suite without behavior changes.
- Generic chat/runtime code dispatches through plugin methods.
- Adding the fake backend does not require editing generic execution code.
- Registry, CLI, and Web choices come from plugin descriptors.
- Existing public CLI commands and persisted states remain compatible.
- OpenCode can be added without a new Codex/Claude-style branch in
  `chat.py` or `backend_runtime.py`.

OpenCode validation is complete when:

- two consecutive turns reuse the same session
- tool events appear in AHA Chat/Commands
- final text and usage are recovered reliably
- stop/reset/restart do not duplicate a completed turn
- AHA action JSON remains functional
- permission behavior is accurately reported

## 20. Rollout

- ship pluginization first with only existing built-ins enabled
- keep OpenCode behind an experimental setting
- pin the validated OpenCode version and disable its auto-update in AHA launches
- collect runtime/event compatibility diagnostics
- promote OpenCode only after session, completion, permission, and recovery
  tests pass on Windows/WSL deployments
