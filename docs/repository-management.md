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

`tests/test_cli.py` is legacy coverage and should be split by behavior as code
is touched:

```text
tests/test_store_*.py       persistence helpers, compatibility, snapshots
tests/test_cli_*.py         command parsing and CLI output
tests/test_web_*.py         HTTP API behavior
tests/test_realtime_*.py    WebSocket and realtime stream behavior
tests/test_runtime_*.py     backend runtime and process lifecycle
tests/test_orchestrator.py  AHA action routing and coordination
tests/helpers.py            shared fixtures only
```

When splitting tests, move related tests without rewriting assertions first.
Only broaden or rewrite tests after the move is green.

## Verification

For refactors that touch shared behavior, run:

```bash
python3 -m compileall -q src tests
python3 -m unittest tests.test_cli
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
