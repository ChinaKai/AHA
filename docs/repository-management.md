# Repository Management

This guide defines how to keep AHA changes small, reviewable, and aligned with
the current module boundaries.

## Change Strategy

- Prefer narrow, behavior-preserving moves before functional changes.
- Keep each source file below 1000 lines. Split mixed responsibilities before
  adding more logic.
- Make one ownership move per commit when possible, then run the focused
  verification commands.
- Do not mix refactors, behavior changes, and formatting churn in one commit.
- Preserve old `.aha/runs/<run-id>/plan.json` and task directory compatibility
  unless a migration is implemented.

## Store Ownership

`src/aha_cli/store/filesystem.py` is the compatibility facade. New persistence
logic should live in a focused store module and be re-exported or wrapped from
the facade only when existing callers still need that API.

```text
store/io.py          JSON, JSONL, and text file helpers
store/paths.py       AHA home, run path, and task path helpers
store/config.py      local config and preferred run persistence
store/events.py      run event append and pagination
store/event_views.py read-side task log and conversation event projections
store/runs.py        run plan creation and run lifecycle helpers
store/sessions.py    backend session metadata persistence
store/workspaces.py  workspace registry persistence
store/agents.py      agent creation, status, config, and runtime state
store/tasks.py       task creation, status, coordination, hide/delete state
store/rounds.py      task round lifecycle and final artifact paths
store/finals.py      task final/result writing
store/journal.py     task journal and overview rendering
store/snapshots.py   read-side task/run status projections
```

Rules for store edits:

- Use `store/io.py` helpers for structured persistence instead of ad hoc file
  reads and writes.
- Pass `now_func` and `append_event_func` through wrappers when moving logic
  that tests patch through `filesystem.py`.
- Avoid circular imports. Store modules may import domain construction helpers,
  but they should not import Web or long-running service code.
- Keep append-only logs append-only. Derived snapshots may be rewritten.

## Service Ownership

- `services/backend_runtime.py` owns managed backend processes, locks, and
  runtime cleanup.
- `services/agent_backend_switch.py` owns agent backend switching, backend
  session reset, handoff summaries, and explicit backend restart after startup
  setting changes.
- `services/chat.py` owns backend chat turns and finalization handling.
- `services/chat_offsets.py` owns chat inbox offset path/load/save helpers and
  task-scoped worker exit checks.
- `services/chat_prompt_context.py` owns chat prompt assembly, prompt status
  snapshots, event context filtering, and prompt metrics.
- `services/chat_supervision.py` owns assisted supervision host prompts,
  visibility filtering, host routing, and host decision application.
- `services/orchestrator.py` owns AHA action execution and sub-agent
  coordination.
- `services/proxy.py` owns backend-specific Core proxy normalization and backend environment
  injection.
- `services/run_archive.py` owns import/export archive behavior.
- `services/run_retention_policy.py` owns retention policy thresholds,
  all-run policy reporting, scheduled report persistence, and
  apply-if-over-limit enforcement.
- `services/onebin.py` and `scripts/build_onebin.py` own zipapp packaging.

## CLI Ownership

- `cli.py` owns command implementations and legacy CLI-level compatibility
  exports used by tests and callers.
- `cli_parser.py` owns argparse command registration, command alias
  normalization, and default-command selection.

## Web Ownership

`src/aha_cli/web/server.py` owns HTTP routing, WebSocket handoff, and static UI
serving. Keep request parsing, API payload assembly, and task lifecycle helpers
in focused modules so route handlers stay thin.

```text
web/http_utils.py       HTTP parsing and response helpers
web/run_api.py          workspace, bootstrap, run create/list/archive helpers
web/run_routes.py       run, archive, maintenance, bootstrap, and workspace HTTP routes
web/status.py           web status snapshots and backend-loss recovery
web/system_routes.py    status, access-control, backend, events, debug, and restart routes
web/task_actions.py     compatibility facade for task web helpers
web/task_command_actions.py compact reset, checkpoint, final, reopen, interrupt actions
web/task_command_format.py  /aha and /agent command text formatting
web/task_command_router.py  slash command routing and command response payloads
web/task_commands.py    compatibility facade for task command helpers
web/task_messaging.py   send payload, chat offsets, and task message locks
web/task_runtime.py     backend autostart and finalization runtime helpers
web/task_routes.py      task, agent, config, final, and send HTTP routes
web/conversation.py     conversation, event stream, and task log projections
web/session_debug.py    backend session jsonl discovery and analysis
web/server.py           route dispatch, WebSocket upgrade, static assets
```

Rules for web edits:

- Keep `server.py` route handlers thin; move reusable logic into the owning
  web module before adding new route-specific branches.
- Patch dependencies where they are used. After a helper moves out of
  `server.py`, tests should patch the helper module, not the server import.
- Avoid importing service runtime code into UI projection modules unless the
  projection directly needs process state.

## Test Layout Target

Keep tests grouped by behavior, not by historical entry point. The legacy
`tests/test_cli.py` bucket has been retired; new tests should land in the
focused module that owns the behavior.

```text
tests/test_store_state.py       persistence helpers, compatibility, snapshots
tests/test_cli_core.py          init, plan, status, commit policy, packaging
tests/test_backend_*.py         backend registry, runners, sessions, runtime
tests/test_chat_prompt.py       chat offsets, prompt context, sticky deltas
tests/test_supervision_flow.py  supervision routing and agent command flow
tests/test_finalization_flow.py final result, round journal, checkpoints
tests/test_web_run_api.py       bootstrap, workspace, run creation, archives
tests/test_web_task_api.py      task create/resume, settings, backend switch, proxy APIs
tests/test_web_events_api.py    UI core, conversation events, logs, event replay
tests/test_web_status.py        status snapshots, recovery, send/interrupt APIs
tests/test_websocket.py         WebSocket replay and realtime stream behavior
tests/test_frontend_static.py   static UI behavior that does not need a browser
tests/test_orchestrator.py      AHA action routing and sub-agent coordination
tests/test_run_archive.py       run archive import/export
tests/helpers.py                shared fixtures only
```

When splitting tests, move related tests without rewriting assertions first.
Only broaden or rewrite tests after the move is green.

Current follow-up candidates:

- `tests/test_supervision_flow.py` can split further if host supervision and
  agent-command behavior start changing independently.
- `tests/test_web_task_api.py` can split further if task creation/settings and
  proxy behavior start changing independently.

## Parallel Split Workflow

Use sub-agents for independent read/copy preparation, then let `main` do the
single conflicting edit:

1. Assign each sub-agent one target module and a disjoint behavior scope.
2. Sub-agents may create or update their owned target test file, but should not
   edit the shared source file that every split depends on.
3. `main` removes the duplicated tests from the shared file, resolves overlap,
   and runs the full verification set.
4. Commit only after `main` confirms no duplicated test names and the working
   tree contains only the intended split files.

This avoids parallel conflicts in large legacy files such as `tests/test_cli.py`.

## Verification

For refactors that touch shared behavior, run:

```bash
python3 -m compileall -q src tests
python3 -m unittest tests.test_cli_core tests.test_backend_runners tests.test_web_status
python3 -m unittest discover
git diff --check
```

For test-file splits, also run the focused modules that were moved:

```bash
python3 -m unittest tests.test_backend_runners tests.test_cli_core tests.test_web_run_api tests.test_web_task_api tests.test_web_events_api
python3 -m unittest discover
git diff --check
```

For documentation-only changes, `git diff --check` is enough unless the docs
describe a code path changed in the same commit.

## Commit Policy

Use `aha commit` with a Conventional Commit subject and the AHA generator trailer:

```bash
aha commit \
  --type refactor \
  --scope store \
  --summary "extract task snapshot views" \
  --add src/aha_cli/store
```

Task, agent, and scope tracking stays in the AHA journal instead of the Git commit body. Prefer one focused commit per ownership boundary.
