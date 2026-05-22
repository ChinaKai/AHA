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
- `services/chat.py` owns backend chat turns and finalization handling.
- `services/orchestrator.py` owns AHA action execution and sub-agent
  coordination.
- `services/proxy.py` owns task proxy normalization and backend environment
  injection.
- `services/run_archive.py` owns import/export archive behavior.
- `services/onebin.py` and `scripts/build_onebin.py` own zipapp packaging.

## Test Layout Target

Keep tests grouped by behavior, not by historical entry point. The legacy
`tests/test_cli.py` bucket has been retired; new tests should land in the
focused module that owns the behavior.

```text
tests/test_store_state.py       persistence helpers, compatibility, snapshots
tests/test_cli_core.py          init, plan, status, commit policy, packaging
tests/test_backend_*.py         backend registry, runners, sessions, runtime
tests/test_chat_*.py            chat turns, prompt context, finalization flow
tests/test_web_run_api.py       bootstrap, workspace, run creation, archives
tests/test_web_task_api.py      task create/resume, settings, proxy APIs
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

- `tests/test_chat_flow.py` is large and can split into prompt context,
  supervision, and finalization modules if those areas change again.
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
python3 -m unittest tests.test_cli
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

Use `aha commit` with Conventional Commit metadata and AHA trailers:

```bash
aha commit \
  --type refactor \
  --scope store \
  --summary "extract task snapshot views" \
  --task-id task-043 \
  --agent main \
  --aha-scope store-snapshots \
  --add src/aha_cli/store
```

Prefer one focused commit per ownership boundary.
