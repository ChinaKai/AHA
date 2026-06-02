# AHA Optimization Plan

This document is the running plan for reducing AHA maintenance cost while preserving the local-first, onebin-friendly workflow. Keep it updated before and after each optimization slice so future agents can resume without reconstructing context from chat history.

## Goals

- Keep AHA fast to change without breaking existing runs, task archives, or local UI behavior.
- Reduce cross-file churn for protocol fields, prompts, and task metadata.
- Make real UI/API/runtime smoke checks repeatable instead of manual.
- Split oversized orchestration and frontend modules only when the new boundary is clear.

## Non-Goals

- Do not rewrite the storage format wholesale.
- Do not replace the file-backed store with a database.
- Do not remove legacy `collaboration_mode`, `delegation_policy`, or `max_sub_agents` compatibility.
- Do not make large frontend framework migrations in this optimization track.

## Progress

| Phase | Status | Notes |
| --- | --- | --- |
| 0. Planning anchor | Done | Created this plan before code changes. |
| 1. Repeatable smoke | Done | Added `scripts/smoke_workflow_ui_api.py` for workflow UI/API smoke checks. |
| 2. Task schema consolidation | Done | Added task metadata projection helper, Web execution parser, status/event consistency checks, and compatibility tests. |
| 3. Orchestrator split | Done | `action_payloads.py`, `subagent_state.py`, `task_updates.py`, and `routing.py` slices complete. |
| 4. Frontend modularization | Paused | Pure-helper slices through `conversation_metadata.js` are complete; pause before DOM-heavy/state orchestration refactors. |
| 5. Workflow template registry | Done | Backend registry slice complete; frontend still keeps static fallback. |
| 5b. Run lifecycle projection | Done | Projection and read-only UI display are complete; no list filtering, lifecycle buttons, endpoints, schema writes, or cleanup changes. |
| 5c. Run lifecycle soft actions | Done | Added guarded CLI/API write entries for `active` / `hidden` / `archived` on inactive runs only; no UI controls, hard delete, list filtering, or cleanup changes. |
| 5d. Run lifecycle UI actions | Done | Added soft Hide/Archive/Restore controls for protected-aware lifecycle API calls; no hard delete, list filtering, or cleanup changes. |
| 5e. Run lifecycle frontend filter | Done | Added frontend-only Active/Hidden/Archived/All filters so hidden and archived runs can be found and restored without changing `/api/runs`. |
| 6. Run cleanup tooling | Done | Script and `aha runs cleanup` CLI/onebin cleanup entries are complete. |
| 6b. Service/run diagnostics | Done | Added a read-only `aha runs diagnose` CLI/onebin entry for current run, visible runs, heartbeat runs, service/listener/process probes, and cleanup protection reasons. |
| 6c. Stale runtime diagnostics | Done | Extended `aha runs diagnose` with read-only stale running-agent detection; no recovery action or status mutation. |
| 6d. Explicit run delete | Done | Added guarded physical run deletion for CLI/API and force support for heartbeat-active non-current runs. |
| 6e. Source service persistence | Done | Added a source user-service installer that pins the source UI to repo `.aha` on port 8766. |
| 7. Prompt/protocol alignment | Done | Aligned action contract docs and tests with current task prompts; no runtime semantics change. |
| 8. Onebin smoke hardening | Done | Added repeatable isolated onebin CLI smoke coverage for init, diagnose, and cleanup. |
| 8b. Onebin/run management docs | Done | Documented user-facing cleanup, diagnose, lifecycle, and onebin smoke commands. |
| 8c. Dual home smoke | Done | Added a source-vs-onebin UI smoke that verifies separate AHA homes and non-leaking run lists. |
| 9. Run retention and compaction | Done | Added dry-run retention reporting, policy thresholds, archive inspect/restore, and guarded apply/archive/force compaction for safe artifact groups. |
| 10. Stale runtime recovery | Done | Added dry-run-first `runs recover` with exact-target apply and optional backend restart for stopped backend agents still marked running. |
| 11. Browser heartbeat orphan hardening | Done | Realtime debug persistence now requires a plan-backed run, preventing stale tabs from recreating deleted run dirs. |
| 12. Frontend DOM/state split | In Progress | Added pure realtime/run lifecycle helpers and a guarded maintenance panel; `app.js` still owns broad DOM/fetch/realtime coordination. |
| 13. Install/update workflow hardening | Done | Added dry-run service unit generation, default token-file auth wiring, health checks, and standalone no-write smoke coverage for onebin/source installers. |

## Complete Roadmap

The optimization track is ordered by maintenance risk and elapsed-time payoff. Each item should land as a small commit with this document updated before and after the slice.

| Priority | Area | Target | Safe first slices |
| --- | --- | --- | --- |
| 1 | Frontend modularization | Reduce `app.js` from a mixed UI/runtime file into plain static helper files plus thin DOM orchestration. | Pure metadata/helper slices are mostly complete and paused before DOM-heavy work; resume only with a clearly isolated view-model/render boundary. |
| 2 | Run lifecycle | Add explicit run lifecycle semantics without surprising deletion behavior. | Projection and read-only UI labels are complete; later add soft hide/archive APIs before any destructive UI control. |
| 3 | Service/run management | Make local services, active pages, heartbeat runs, and cleanup decisions explainable. | Add read-only `aha runs diagnose` first; do not stop services, delete runs, or write systemd state. |
| 4 | Task/run schema boundary | Keep file-backed dictionaries but reduce duplicated field projection and compatibility logic. | Add small schema/projection helpers for run metadata and task runtime settings; avoid dataclass migrations until callers converge. |
| 5 | Prompt/protocol maintainability | Make agent contracts easier to audit and update. | Split long prompt contexts by concern, keep examples close to protocol docs, and add tests for required runtime contract phrases. |
| 6 | Runtime resilience | Improve backend/session recovery without changing normal happy paths. | Add targeted recovery tests, clearer session reset reasons, and safer active-backend detection. |
| 7 | Product/UI polish | Make the local dashboard easier to scan under repeated use. | Improve dense controls, empty states, status wording, and responsive behavior after logic is modularized. |
| 8 | Onebin/release/docs hardening | Keep the single-file artifact and docs reliable as features move. | Add onebin smoke coverage for new CLI entries, static assets, import/export, cleanup, diagnose, and bootstrap; refresh architecture/protocol/session docs after completed structural phases. |

Near-term rule: prefer pure helper extraction and projection consistency over feature work. Do not introduce new framework, database, or run schema migration as part of this track.

## Current Remaining Gaps

The main onebin/source-home, retention, stale-runtime, orphan-heartbeat, and
service-token risks are now covered by guarded commands, Web/API entries, and
smoke tests. The remaining gaps are lower-level productization and test
environment limits:

- `src/aha_cli/web/static/app.js` still owns broad DOM rendering, mutable UI
  state, fetch orchestration, and WebSocket coordination. More helper slices are
  possible, but the next splits should be bounded to one panel at a time.
- Web maintenance actions are confirmation-gated and server-guarded, but the UI
  still uses compact browser prompts instead of richer modal review flows that
  show the exact file/agent impact before apply.
- Token auth is local-tool hardening, not multi-user authorization. There are no
  roles, CSRF tokens, TLS termination, audit login events, or remote identity
  provider integrations.
- Service installers verify dry-run units and real post-restart health checks
  when run manually, but automated tests intentionally avoid starting real user
  systemd services.
- Onebin smoke builds and exercises a zipapp, but does not replace the user's
  real `~/.local/bin/aha` or restart the live user service.
- Browser regression coverage now has an optional Playwright smoke, but default
  automated tests still skip it when Playwright/Chromium is unavailable.

Recommended residual order:

1. Continue Phase 12 only with small frontend view-model/render slices.
2. Run `scripts/smoke_playwright_ui.py --require-playwright` on machines that
   provide browser dependencies.
3. Add an opt-in manual/systemd E2E checklist for release machines,
   kept out of default automated tests.

## Phase 1: Repeatable Smoke

Create a script that:

- Boots an isolated temporary AHA home.
- Creates a run using the current working tree.
- Exercises the UI HTML served by `handle_ui_client`.
- Verifies the task creation UI emphasizes `Execution=auto` and `Workflow` templates.
- Verifies `solo` / `pair` / `team` are still API-compatible but not shown in the main UI select.
- Prints a concise JSON summary and exits non-zero on failure.

Acceptance:

- `PYTHONPATH=src python3 scripts/smoke_workflow_ui_api.py` passes.
- Existing unit tests continue to pass.

## Phase 2: Task Schema Consolidation

Problem: adding `workflow_template` touched domain, store, API routes, prompt context, UI, tests, and snapshots. That is a signal that task fields need a stronger schema boundary.

Target shape:

- Introduce a small task metadata schema helper for defaults, normalization, and projection fields.
- Keep dictionaries as the persistence format for compatibility.
- Replace ad hoc field lists in snapshots/events/API routes with shared helpers where practical.

Acceptance:

- New task fields require fewer call-site edits.
- Archive and old plan compatibility tests still pass.

## Phase 3: Orchestrator Split

Problem: `src/aha_cli/services/orchestrator.py` is over 1000 lines and mixes prompt construction, action parsing, spawn/reuse, routing, and journal updates.

Target slices:

- `action_payloads.py`: parse and validate AHA action JSON.
- `subagent_dispatch.py`: create/reuse sub-agents and normalize runtime settings.
- `routing.py`: route messages to existing agents.
- `task_updates.py`: durable journal updates.

Acceptance:

- Public behavior remains unchanged.
- Existing orchestrator tests pass with smaller, focused tests for extracted helpers.

## Phase 4: Frontend Modularization

Problem: `src/aha_cli/web/static/app.js` owns too many UI concerns.

Target slices:

- Task creation form.
- Run/session metadata helpers.
- Task list and selected task header.
- Agents/runtime panel.
- Conversation/log/context panels.
- Settings and run management.

Backlog from the 2026-05-31 audit:

- `prompt_metrics.js`: metric number/byte formatting, context pressure summaries, cache-token helpers, component metric rows, and prompt artifact metadata. Completed as the sixth frontend helper slice.
- `conversation_metadata.js`: event payload parsing, AHA action envelope parsing, task/agent event ownership checks, timeline category metadata, conversation counts, and sender/target display helpers. Completed as the seventh frontend helper slice; split a dedicated `event_metadata.js` later only if this file grows beyond pure metadata helpers.
- Do not move `renderAgents()`, timeline DOM rendering, WebSocket/fetch, optimistic mutation, selected-state switching, or helpers that depend on `Date.now()`/global event state until their boundaries are clearer.

Acceptance:

- No framework migration.
- Static frontend tests still pass.
- UI smoke script still passes.

## Phase 5: Workflow Template Registry

Problem: workflow templates are useful, but inline constants will become hard to maintain as domain-specific templates grow.

Target shape:

- Keep built-in templates in code initially.
- Provide one registry function that returns template metadata and prompt guidance.
- Later allow config-defined templates if needed.

Acceptance:

- API, CLI, UI, and prompt code read from the same registry.

## Phase 5b: Run Lifecycle Projection

Problem: run cleanup exists, but run lifecycle semantics are not projected consistently yet. Adding hide/archive controls later should not require every API caller to rediscover old `plan.json` compatibility rules.

Target shape:

- Add a small read-only run lifecycle metadata helper.
- Treat legacy plans without lifecycle fields as `active`.
- Recognize future `hidden` / `hidden_at` / `archived` / `archived_at` fields and equivalent lifecycle status fields.
- Expose the projection through run summaries, `/api/runs`, and bootstrap payloads.
- Do not filter run lists, add Web UI operations, add hide/archive/delete endpoints, or change cleanup behavior in this slice.

Acceptance:

- Legacy runs report lifecycle `active`.
- Hand-written hidden/archived metadata appears in store summaries, `/api/runs`, and bootstrap.
- Existing create, rename, import, and export behavior remains unchanged.
- The run list and current-run area can display `active`, `hidden`, or `archived` as read-only lifecycle labels without adding lifecycle actions.

## Phase 5c: Run Lifecycle Soft Actions

Problem: lifecycle projection and labels exist, but there is no guarded way to write `hidden` / `archived` / restored `active` state for old inactive runs.

Target shape:

- Add minimal CLI/API write entries for setting run lifecycle status to `active`, `hidden`, or `archived`.
- Reuse `run_lifecycle.py` normalization/projection rules.
- Refuse missing runs, the current run, and runs with active heartbeat by default.
- Do not add hard delete, Web UI buttons, list filtering, cleanup behavior changes, or system service changes.

Acceptance:

- Store/API/CLI tests cover hide, archive, restore, and legacy default-active behavior.
- Current-run and active-heartbeat protection are covered.
- Existing cleanup and diagnose behavior remains unchanged.

## Phase 5d: Run Lifecycle UI Actions

Problem: lifecycle write APIs exist, but users still need a safe dashboard entry for soft hide/archive/restore.

Target shape:

- Add lightweight Hide / Archive / Restore controls in the run management area.
- Call the existing `/api/runs/<run-id>/lifecycle` endpoint.
- Disable or suppress current-run actions and show protection reasons returned by the API.
- Keep active heartbeat protection server-side; if a protected run is attempted, show the returned reason.
- Do not filter run lists, physically delete run directories, alter cleanup behavior, or add hard delete controls.

Acceptance:

- Frontend static tests confirm the UI calls the lifecycle API and exposes only soft actions.
- Web/API tests continue to cover lifecycle protection.
- Workflow UI/API smoke continues to pass.

## Phase 5e: Run Lifecycle Frontend Filter

Problem: soft hide/archive/restore controls exist, but the run management list needs a safe way to focus active runs while still making hidden/archived runs recoverable.

Target shape:

- Add frontend-only `Active` / `Hidden` / `Archived` / `All` filters to the run lifecycle management list.
- Default to active runs for scanning.
- Keep hidden and archived runs discoverable through the filters and restorable through the existing soft lifecycle API.
- Do not change `/api/runs`, backend list semantics, cleanup behavior, or physical run directories.

Acceptance:

- Static/frontend tests confirm filters are present and lifecycle actions remain soft only.
- API/Web tests still cover hide and restore.
- Workflow UI/API smoke continues to pass.

## Phase 6: Run Cleanup Tooling

Problem: tests, smoke scripts, and interrupted manual checks can leave run directories behind, but deleting runs from the Web UI would imply user-facing lifecycle semantics that are not designed yet.

Target shape:

- Provide a dry-run-first cleanup script for stale temporary/test residuals.
- Protect the current run, active heartbeat runs, and normal user runs by default.
- Clean only explicit temporary/orphan run directories and temporary `/tmp` `.aha` homes.
- Avoid run schema migrations or Web UI delete semantics.

Acceptance:

- Dry-run/list mode reports candidates without deleting files.
- Apply mode deletes stale temporary residuals only.
- Unit tests cover current-run protection, active-heartbeat protection, stale temporary deletion, and dry-run safety.

## Phase 6b: Service/Run Diagnostics

Problem: cleanup can correctly protect active runs, but users still need a safe way to understand which run, page, service, port, or process is keeping state alive.

Target shape:

- Provide a read-only `aha runs diagnose` CLI/onebin entry.
- Report the current run, visible plan-backed runs, active heartbeat runs, and cleanup classification reasons.
- Report best-effort AHA-related listeners, processes, and service units without stopping or changing anything.
- Support `--json` for tooling and text output for humans.
- Keep probes injectable so tests do not depend on local `systemd`, `ss`, or host services.

Acceptance:

- Diagnosis never deletes runs or stops services.
- Current run, active heartbeat run, and non-temporary run protection reasons match cleanup behavior.
- CLI tests cover JSON/text output with fake probe data.
- Onebin smoke can run `runs diagnose --json`.

## Phase 6c: Stale Runtime Diagnostics

Problem: a task/agent can remain recorded as `running` even after its backend process has stopped. Recovery already exists elsewhere, but users need a safe diagnostic view before taking action.

Target shape:

- Extend `aha runs diagnose` with a read-only `stale_running_agents` section.
- Flag agents whose task and agent status are still `running` while `backend_status()` reports `stopped`.
- Include run id, task id, agent id, backend status, backend name, last pid, and a stable reason.
- Keep backend status probing injectable for tests.

Non-goals:

- Do not call `/api/agents/recover-stale`.
- Do not write task, agent, event, or backend state.
- Do not change cleanup, lifecycle, or service management semantics.

Acceptance:

- Unit tests cover stale candidate detection with fake backend status data.
- Unit tests verify diagnose does not emit `agent_status_recovered` or mutate task/agent status.
- Text and JSON diagnose output include stale runtime candidates.

## Phase 7: Prompt/Protocol Contract Alignment

Problem: the runtime prompt contracts and protocol documentation can drift as AHA action shapes evolve. The 2026-05-31 audit found that `task_assignment.md` and `backend_task_context.md` already describe `spawn_sub.agent_id` / `scope_id`, while `docs/protocol.md` examples still under-emphasize those optional fields.

Target shape:

- Keep prompt templates, protocol docs, and action payload tests aligned on the canonical action envelope.
- Document `spawn_sub.agent_id` as the explicit reassignment/reuse target and `scope_id` as the same-scope continuation key.
- Add focused tests that catch future drift between prompt-required fields, protocol examples, and the accepted action payload shape.

Non-goals:

- Do not change action execution behavior.
- Do not split or rewrite prompt contracts in this slice.
- Do not change backend/session routing semantics.

Acceptance:

- Protocol examples include `agent_id` and `scope_id` where reassign/reuse semantics matter.
- Tests assert the prompt and protocol docs expose the same canonical action fields.
- Existing action payload/orchestrator behavior remains unchanged.

## Phase 8: Onebin Smoke Hardening

Problem: onebin support is central to AHA, but recent CLI entries are currently verified with ad hoc commands during each slice.

Target shape:

- Add a repeatable onebin CLI smoke script that can build or reuse an artifact.
- Run with temporary `HOME`, temporary `AHA_HOME`, and temporary `/tmp` scan roots.
- Cover at least `--help`, `init --portable`, `runs diagnose --json`, and `runs cleanup --dry-run --json`.
- Validate JSON shape for diagnose and cleanup without depending on a real local UI port, user run, or user home.

Non-goals:

- Do not change release packaging semantics.
- Do not write to the real user home.
- Do not require 8788, systemd, or existing user runs to be present.

Acceptance:

- `PYTHONPATH=src python3 scripts/smoke_onebin_cli.py --json` passes.
- Full tests, `py_compile`, and `git diff --check` pass.

## Phase 9: Run Retention And Compaction

Problem: long-lived runs such as `AHA-ONEBIN` accumulate chat turn files, backend logs, realtime logs, runtime locks, prompt artifacts, and old result snapshots. The current cleanup tooling protects normal user runs, so it does not reduce active-run growth.

Target shape:

- Add a dry-run-first retention report for one run: file counts, byte totals, largest directories, and age buckets.
- Define explicit keep/delete/archive rules for safe artifact classes.
- Add optional policy thresholds for total/candidate size or candidate count so operators can gate apply behavior with a dry-run report first.
- Support archive list/inspect/restore for retention archives without allowing arbitrary archive paths through Web routes.
- Prefer archiving or trimming append-only logs over deleting plan, events, final results, or current task/session state.
- Keep the current run protected from destructive defaults; require explicit run id and apply flag for mutations.
- Require `--force` before deleting archived originals, and refuse active heartbeat runs even with force.

Non-goals:

- Do not change archive import/export format in the first slice.
- Do not delete active task state, current session records, events, plan files, or final artifacts.
- Do not make background cleanup automatic.

Acceptance:

- Dry-run output explains exactly what would be compacted.
- Apply mode is opt-in and covered by tests using temporary runs.
- Onebin smoke or a focused retention smoke verifies the command can run from the zipapp without touching real `~/.aha`.

Progress:

- First slice complete: `aha runs retention <run-id>` reports file counts, byte totals, top-level groups, age buckets, largest files, and retention notes without changing files.
- Apply slice complete: `--apply` creates a compressed retention archive for explicit candidates while preserving originals; `--apply --force` deletes only the archived candidates after archive creation succeeds.
- Safety boundaries: default candidates are limited to `logs/` and `prompts/`; `chat/` requires `--include-chat`; `runtime/`, `results/`, `tasks/`, `sessions/`, `inbox/`, and top-level metadata remain preserved. Apply mode refuses current runs and heartbeat-active runs.
- Policy/archive slice complete: retention dry-runs include threshold alerts, `--apply-if-over-limit` only archives when thresholds trip, and archive list/inspect/restore paths validate source run, archive basename, manifest members, symlink escapes, current run, active heartbeat, and overwrite intent.
- All-run automation slice complete: `aha runs retention-policy` aggregates threshold alerts across every run, stays read-only by default, and only auto-applies over-limit compaction to non-current, heartbeat-inactive runs.

## Phase 10: Stale Runtime Recovery

Problem: diagnostics can report stale running agents, but the user still has to decide manually whether to mark them stopped, restart them, or leave them alone.

Target shape:

- Add a guarded recovery command that consumes the same stale-running-agent evidence as `runs diagnose`.
- Provide dry-run JSON first, then explicit apply action for marking stopped-backend running agents as interrupted.
- Refuse current active backends by only acting on stopped backend evidence, recheck before mutation, and require exact run/task/agent identity for targeted recovery.
- Record recovery events so audits can distinguish automatic repair from normal task transitions.
- Allow backend restart only when explicitly requested after exact-target recovery.

Non-goals:

- Do not silently recover every stale candidate.
- Do not infer task completion from backend exit alone.
- Do not change normal backend startup paths.
- Do not restart backends without explicit `--restart-backend` / Web confirmation.

Acceptance:

- Tests cover stale detection reuse, dry-run output, guarded apply, and event emission.
- Recovery never acts on agents outside the selected run/task/agent.
- Existing diagnose output remains read-only.

Progress:

- Complete: `aha runs recover <run-id>` defaults to dry-run and reports stale runtime candidates without probing service state beyond backend status.
- Complete: `--apply` requires exact `--task-id` and `--agent-id`, rechecks the backend, marks the agent `interrupted`, records recovery context/events, and returns the task to `awaiting_user` when no other agent remains running.
- Complete: `--apply --restart-backend` consumes recovery context, enqueues a recovery resume prompt, marks the agent pending, and starts the matching process-backed backend. Onebin smoke covers recover dry-run with temporary homes only.

## Phase 11: Browser Heartbeat Orphan Hardening

Problem: a stale browser tab can keep sending websocket/client telemetry for a deleted run id. The server currently writes realtime logs under that run id, which can recreate a plan-less run directory after deletion.

Target shape:

- Treat plan-less or deleted run ids as invalid for realtime log persistence.
- Keep enough server-side telemetry to debug invalid clients without recreating run directories.
- Make the browser recover cleanly to the default/current run when selected run data disappears.
- Preserve useful heartbeat behavior for valid runs.

Non-goals:

- Do not remove realtime diagnostics for valid runs.
- Do not break direct deep links to existing plan-backed runs.
- Do not add destructive UI controls in this slice.

Acceptance:

- Tests show websocket/client log paths are not created for missing run ids.
- Deleting a run followed by stale client telemetry does not recreate `runs/<id>/`.
- Valid current-run realtime logs still work.

Progress:

- Complete: WebSocket, task messaging, and browser debug telemetry now share one realtime debug writer.
- Complete: realtime log persistence only writes under `runs/<id>/logs/` when `runs/<id>/plan.json` exists; missing, plan-less, or deleted run ids still print process logs but do not recreate run directories.
- Complete: tests cover WebSocket heartbeat logging for missing runs and client debug telemetry after run deletion.

## Phase 12: Frontend DOM/State Split

Problem: pure helper extraction reduced some `app.js` churn, but the remaining hard part is shared mutable UI state, DOM rendering, fetch/WebSocket coordination, and optimistic updates.

Target shape:

- Choose one bounded view, such as run management or agent runtime panel.
- Extract a pure view-model builder and keep DOM mutation in one render function.
- Add static tests for the view-model contract before moving event handlers.
- Avoid global state reshaping until one view is stable.

Non-goals:

- Do not introduce a frontend framework.
- Do not split every panel at once.
- Do not move realtime/WebSocket ownership until render boundaries are smaller.

Acceptance:

- The selected view has a testable view-model helper.
- Existing frontend static tests and workflow UI/API smoke pass.
- No visual or behavior regression in the selected view.

Progress:

- First slice complete: extracted realtime transport state decisions into `realtime_state.js` while keeping socket ownership, DOM updates, fetch, and event handlers in `app.js`.
- Added a bounded run lifecycle management view-model in `run_metadata.js`; `app.js` now renders filter/action rows from helper output instead of recomputing row state inline.
- Static coverage verifies helper inclusion order, the realtime helper contract, and the run lifecycle view-model contract used by `app.js`.
- Run maintenance UI now exposes confirmation-gated archive, restore, and stale recovery actions plus access-control status, while server routes keep the destructive safety checks.

## Phase 13: Install/Update Workflow Hardening

Problem: source and onebin service installers now exist, but upgrade paths are still manually verified on the developer machine. Release confidence should not depend on inspecting local user systemd state by hand.

Target shape:

- Add dry-run or print-only modes to service install scripts so tests can verify generated unit content without writing user service files.
- Add smoke coverage for source service unit generation and onebin service unit generation with temporary homes.
- Wire service installs to token-file auth by default and make unauthenticated network-visible binds explicit.
- Document the safe sequence for replacing `~/.local/bin/aha`, daemon-reload, restart, and home verification.
- Keep real systemd start/restart out of default automated tests.

Non-goals:

- Do not require systemd in CI-like smoke environments.
- Do not change default ports or homes.
- Do not restart real user services during automated tests.

Acceptance:

- Shell syntax tests and unit-content smoke pass without writing `~/.config/systemd/user`.
- Docs include source and onebin update commands with verification steps.
- Manual service status checks remain optional, not the only validation path.

Progress:

- Added `--dry-run` to onebin and source user-service installers. Dry-run prints the install plan and generated unit without building onebin, writing user systemd files, or calling `systemctl`/`loginctl`.
- Added unittest coverage for shell syntax and dry-run unit content using temporary homes/config dirs.
- Added `scripts/smoke_service_installers.py --json` as a standalone release/preflight smoke for installer dry-runs and no-write behavior.
- Added `GET /api/health` as a run-independent readiness endpoint and wired onebin/source installers to poll it after real service restarts.
- Added installer entrypoint version validation: onebin verifies the freshly built `aha --version`, source verifies `python -m aha_cli --version`; dry-run/smoke coverage reports the planned health URL and validation mode.
- Added `aha ui --auth-token` / `--auth-token-file`, token-protected UI/API/WebSocket access, public `/api/health` readiness with `auth_required`, `/api/access-control` risk reporting, and service installer token-file wiring. Dry-run/smoke coverage verifies token file paths are planned but not written.

## Working Log

- 2026-05-30: Started optimization plan after workflow-template implementation was committed in `649139c`.
- 2026-05-30: Added repeatable workflow UI/API smoke script. Verified `PYTHONPATH=src python3 scripts/smoke_workflow_ui_api.py --json` passes.
- 2026-05-30: Completed Phase 2 audit. `sub-001` mapped task field duplication across domain/store/API/status/UI/prompt; `sub-002` identified compatibility, projection, event, and smoke coverage gaps.
- 2026-05-30: Started Phase 2 implementation with scope limited to schema helper, field projection consistency, creation-path parsing cleanup, and focused tests.
- 2026-05-30: Completed Phase 2 implementation. Verified `py_compile`, schema/Web API targeted tests, workflow UI/API smoke with `/api/status`, full `unittest discover`, and `git diff --check`.
- 2026-05-30: Completed Phase 3 audit. Recommended first slice is `action_payloads.py` because it is pure JSON/schema logic with minimal runtime coupling.
- 2026-05-30: Started Phase 3 first implementation slice. Scope is limited to action payload parsing/schema helpers; routing, sub-agent state, and dispatch remain in `orchestrator.py`.
- 2026-05-30: Completed Phase 3 first slice by adding `services/action_payloads.py`, preserving `orchestrator.py` compatibility imports, and moving chat/supervision parser imports to the new helper. Verified `py_compile`, targeted action/orchestrator tests, full `unittest discover`, and `git diff --check`.
- 2026-05-30: Started Phase 3 second implementation slice. Scope is limited to pure sub-agent state helpers; routing, dispatch, task updates, and spawn/reuse side effects stay in `orchestrator.py`.
- 2026-05-30: Completed Phase 3 second slice by adding `services/subagent_state.py`, preserving `orchestrator.py` compatibility imports, and moving chat/chat-offset/supervision waiting checks to the new helper. Verified `py_compile`, targeted sub-agent state/orchestrator tests, full `unittest discover`, and `git diff --check`.
- 2026-05-30: Started Phase 3 third implementation slice. Scope is limited to `record_task_update` parsing, validation, journal update, and return payload helpers.
- 2026-05-30: Completed Phase 3 third slice by adding `services/task_updates.py` for `record_task_update` payload parsing, skip handling, journal update, and return payload. Verified `py_compile`, targeted task-update/orchestrator tests, full `unittest discover`, and `git diff --check`.
- 2026-05-30: Started Phase 3 fourth implementation slice. Scope is limited to pure `route_to_agent` validation, target resolution, skip-event payload, routed-event payload, and executed-result payload helpers.
- 2026-05-30: Completed Phase 3 fourth slice by adding `services/routing.py` for `route_to_agent` validation, target resolution, and event/result payload helpers while keeping state changes and backend dispatch in `orchestrator.py`. Verified `py_compile`, targeted routing/orchestrator/supervision tests, full `unittest discover`, and `git diff --check`.
- 2026-05-30: Started Phase 4 first implementation slice. Scope is limited to static frontend task metadata constants and pure helpers in `task_metadata.js`; DOM rendering, fetch, WebSocket, and UI state machines stay in `app.js`.
- 2026-05-30: Completed Phase 4 first slice by adding `web/static/task_metadata.js` as a plain global script and moving workflow/collaboration/supervision/context constants and pure helpers out of `app.js`. Verified frontend static tests, workflow UI/API smoke, related Web tests, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-30: Completed Phase 4 follow-up audit. `sub-001` recommended `task_form.js` as the next minimal frontend slice; `sub-002` recommended a later workflow template registry slice and deferring run lifecycle cleanup.
- 2026-05-30: Started Phase 4 second implementation slice. Scope is limited to task creation payload normalization, confirm dialog rows, and fallback confirm text in `task_form.js`; submit handlers, fetch, WebSocket, and dialog state stay in `app.js`.
- 2026-05-30: Completed Phase 4 second slice by adding `web/static/task_form.js` as a plain global script for task create payload normalization, confirm dialog rows, and fallback confirm text while keeping submit handlers and runtime state in `app.js`. Verified frontend static tests, workflow UI/API smoke, related Web tests, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-30: Started Phase 5 first implementation slice. Scope is limited to a backend workflow template registry, backend validation/prompt/bootstrap consumers, and frontend bootstrap-driven metadata when available; static frontend fallback remains.
- 2026-05-30: Completed Phase 5 first slice by adding `domain/workflow_templates.py` for ordered workflow template metadata and guidance, keeping legacy model aliases, wiring Web validation/CLI choices/prompt/bootstrap to the registry, and letting the frontend prefer bootstrap metadata while retaining static fallback. Verified registry/API/prompt/frontend targeted tests, workflow UI/API smoke, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-30: Started Phase 6 first implementation slice. Scope is limited to conservative cleanup tooling for stale temporary/orphan runs and temporary `/tmp` `.aha` homes; no Web UI delete semantics or run schema migration.
- 2026-05-30: Completed Phase 6 first slice by adding `services/run_cleanup.py` and `scripts/cleanup_temp_runs.py`. The script defaults to dry-run/list, requires `--apply` for deletion, protects current runs, active heartbeat runs, and non-temporary user runs, and handles unreadable `/tmp` entries conservatively. Verified targeted cleanup tests, live dry-run smoke, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-30: Started Phase 6 second implementation slice. Scope is limited to exposing the existing run cleanup helper through `aha runs cleanup` for CLI/onebin use while keeping default dry-run behavior and the standalone script as a thin wrapper.
- 2026-05-30: Completed Phase 6 second slice by adding `aha runs cleanup` with dry-run/apply/json options, sharing cleanup summary formatting between CLI and script, and verifying the onebin artifact can run the cleanup dry-run entry. Verified CLI/run cleanup targeted tests, live CLI dry-run smoke, full `unittest discover`, onebin build and onebin cleanup smoke, `py_compile`, and `git diff --check`.
- 2026-05-31: Expanded this plan with a complete optimization roadmap ordered by maintenance risk and payoff. Started Phase 4 third frontend slice; scope is limited to pure run/session metadata helpers with DOM, fetch, WebSocket, and state transitions staying in `app.js`.
- 2026-05-31: Completed Phase 4 third slice by adding `web/static/run_metadata.js` as a plain global script for run id/title/session label helpers while keeping DOM, fetch, WebSocket, and state transitions in `app.js`. Verified frontend static tests, workflow UI/API smoke, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-31: Started Phase 4 fourth frontend slice. Scope is limited to task/run list status, agent/session display, metadata text, title text, and pure filter/sort helpers in `task_list.js`; DOM rendering, fetch, WebSocket, event binding, and state transitions stay in `app.js`.
- 2026-05-31: Completed Phase 4 fourth slice by adding `web/static/task_list.js` as a plain global script for task/run list status, agent/session display, metadata/title text, and pure visibility/activity helpers while keeping list DOM rendering, event binding, fetch, WebSocket, and state transitions in `app.js`. Verified frontend static tests, workflow UI/API smoke, related Web tests, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-31: Started Phase 4 fifth frontend slice. Scope is limited to agent role/status/backend/model/session display, grouping, runtime defaults, and card view-model helpers in `agent_metadata.js`; agent DOM rendering, event binding, fetch, WebSocket, and state transitions stay in `app.js`.
- 2026-05-31: Completed Phase 4 fifth slice by adding `web/static/agent_metadata.js` as a plain global script for agent role/status/backend/model/session display, grouping, runtime defaults, and card view-model helpers while keeping agent DOM rendering, event binding, fetch, WebSocket, and state transitions in `app.js`. Verified frontend static tests, workflow UI/API smoke, related Web tests, full `unittest discover`, `py_compile`, `git diff --check`, and `node --check`.
- 2026-05-31: Completed Phase 4 follow-up audit. `sub-001` recommended `prompt_metrics.js` as the smallest next pure-helper slice; `sub-002` recommended `conversation_metadata.js` / `event_metadata.js` as a later pure classification/formatting slice and advised against returning to run lifecycle/schema before finishing these low-risk frontend helpers.
- 2026-05-31: Started Phase 4 sixth frontend slice. Scope is limited to metric/context pressure/prompt artifact pure formatting helpers in `prompt_metrics.js`; rendering, DOM, fetch, WebSocket, state caches, and `Date.now()` dependent helpers stay in `app.js`.
- 2026-05-31: Completed Phase 4 sixth slice by adding `web/static/prompt_metrics.js` as a plain global script for metric formatting, context pressure summaries, usage-cache token helpers, component metric rows, and prompt artifact metadata while keeping rendering, DOM, fetch, WebSocket, state caches, and `Date.now()` dependent helpers in `app.js`. Verified frontend static tests, workflow UI/API smoke, related Web tests, full `unittest discover`, `py_compile`, `git diff --check`, and `node --check`.
- 2026-05-31: Started Phase 4 seventh frontend slice. Scope is limited to conversation/event metadata helpers in `conversation_metadata.js`: event data extraction, AHA action envelope parsing, event/task/agent ownership, timeline type/category metadata, conversation filter counts, sender/target display text, and agent update text. Timeline rendering, DOM, fetch/WebSocket, optimistic mutation, state switching, and time-cache behavior stay in `app.js`.
- 2026-05-31: Completed Phase 4 seventh slice by adding `web/static/conversation_metadata.js` as a plain global script for event data extraction, AHA action envelope parsing, event/task/agent ownership, timeline category/filter count helpers, sender/target display text, and agent update text while keeping timeline rendering, DOM, fetch/WebSocket, optimistic mutation, state switching, and time-cache behavior in `app.js`. Verified frontend static tests, workflow UI/API smoke, related Web tests, full `unittest discover`, `py_compile`, `git diff --check`, and `node --check`.
- 2026-05-31: Completed Phase 4 closeout audit. Pure frontend helper extraction is paused before DOM-heavy/state orchestration work; the next low-risk slice is run lifecycle read-only projection.
- 2026-05-31: Started Phase 5b run lifecycle projection slice. Scope is limited to read-only lifecycle metadata/projection in run summaries, `/api/runs`, and bootstrap; run lists are not filtered, no UI operations or lifecycle endpoints are added, and cleanup behavior stays unchanged.
- 2026-05-31: Completed Phase 5b run lifecycle projection slice by adding `domain/run_lifecycle.py` and exposing legacy-active, hidden, and archived lifecycle metadata through run summaries, `/api/runs`, and bootstrap without filtering lists or changing cleanup behavior. Verified related run/store/Web tests, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-31: Started Phase 5b read-only UI display slice. Scope is limited to showing lifecycle labels from existing `/api/runs` / bootstrap projection in the run list and current-run area; no filtering, hide/archive/delete buttons, schema writes, or cleanup changes.
- 2026-05-31: Completed Phase 5b read-only UI display slice by extending `run_metadata.js` lifecycle helpers, showing lifecycle in the run select label and current-run badge, and keeping lifecycle as display-only. Verified frontend static tests, workflow UI/API smoke, related run/Web tests, full `unittest discover`, `py_compile`, `git diff --check`, and `node --check`.
- 2026-05-31: Started Phase 6b service/run diagnostics slice. Scope is limited to a read-only `aha runs diagnose` CLI/onebin entry with injectable probes for run visibility, heartbeat activity, cleanup protection reasons, listeners, processes, and service units; no services are stopped, no runs are deleted, no cleanup semantics change, and no systemd writes are introduced.
- 2026-05-31: Completed Phase 6b service/run diagnostics slice by adding `services/run_diagnostics.py` and `aha runs diagnose` with JSON/text output, cleanup-aligned run explanations, and best-effort read-only listener/process/service probes. Verified targeted CLI/run diagnostics tests, full `unittest discover`, `py_compile`, `git diff --check`, onebin build, and onebin `runs diagnose --json` smoke.
- 2026-05-31: Started Phase 5c run lifecycle soft-actions slice. Scope is limited to guarded CLI/API entries for setting inactive runs to `active`, `hidden`, or `archived`; current runs, active heartbeat runs, and missing runs must be refused, and no UI controls, hard delete, list filtering, cleanup changes, or system service changes are included.
- 2026-05-31: Completed Phase 5c run lifecycle soft-actions slice by adding shared lifecycle normalization/write helpers, guarded `aha runs lifecycle`, and `/api/runs/<run-id>/lifecycle` for inactive runs only. Verified store/API/CLI protection tests, cleanup/diagnose unchanged behavior, full `unittest discover`, `py_compile`, `git diff --check`, onebin build, and onebin lifecycle/diagnose smoke.
- 2026-05-31: Started Phase 5d run lifecycle UI-actions slice. Scope is limited to soft Hide/Archive/Restore controls that call the existing lifecycle API and surface protection errors; no hard delete, list filtering, cleanup changes, directory deletion, or system service changes are included.
- 2026-05-31: Completed Phase 5d run lifecycle UI-actions slice by adding protected-aware soft lifecycle controls to the Run operation menu, reusing `run_metadata.js` helpers, and surfacing API protection errors without adding hard delete, filtering, or cleanup changes. Verified frontend static tests, workflow UI/API smoke, related run/Web tests, full `unittest discover`, `py_compile`, `git diff --check`, and `node --check`.
- 2026-05-31: Started Phase 5e run lifecycle frontend-filter slice. Scope is limited to frontend-only Active/Hidden/Archived/All filtering for the lifecycle management list; `/api/runs`, backend list semantics, cleanup, physical deletion, and lifecycle API behavior remain unchanged.
- 2026-05-31: Completed Phase 5e run lifecycle frontend-filter slice by adding frontend-only Active/Hidden/Archived/All filters to the lifecycle management list, defaulting to active runs while keeping hidden/archived runs discoverable and restorable through the existing soft lifecycle API. Verified frontend static tests, workflow UI/API smoke, related run/Web tests, full `unittest discover`, `py_compile`, `git diff --check`, and `node --check`.
- 2026-05-31: Completed follow-up audit after run lifecycle closeout. `sub-002` recommended Phase 6c read-only stale runtime diagnostics as the next minimal slice; `sub-001` recommended prompt/protocol contract alignment as a later documentation/testing slice. Started Phase 6c with scope limited to `aha runs diagnose` output and fakeable backend-status probes; no recovery, status mutation, cleanup change, or service action is included.
- 2026-05-31: Completed Phase 6c stale runtime diagnostics by adding `stale_running_agents` to `aha runs diagnose` JSON/text output for running task agents whose backend status is stopped. The diagnostic path is read-only and uses injectable backend-status probes in tests. Verified targeted run/CLI tests, full `unittest discover`, `py_compile`, `git diff --check`, onebin build, and onebin `runs diagnose --json` smoke.
- 2026-05-31: Started Phase 7 prompt/protocol contract alignment based on the completed `sub-001` audit. Scope is limited to protocol documentation examples and focused contract tests for the canonical AHA action envelope; no action runtime, prompt execution, backend/session, or routing semantics change is included.
- 2026-05-31: Completed Phase 7 prompt/protocol contract alignment by updating `docs/protocol.md` spawn/reassign examples to include `agent_id` and `scope_id`, and adding `tests/test_protocol_contract.py` to assert protocol examples, prompt templates, and supported action types stay aligned. Verified targeted protocol/action/prompt tests, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-31: Started Phase 8 onebin smoke hardening. Scope is limited to a repeatable isolated CLI smoke script for onebin help/init/diagnose/cleanup behavior; no release semantics, real user home writes, service management, or runtime behavior changes are included.
- 2026-05-31: Completed Phase 8 onebin smoke hardening by adding `scripts/smoke_onebin_cli.py`. The script builds or reuses the onebin artifact, runs with temporary `HOME`, temporary portable `.aha`, and temporary cleanup scan roots, then verifies help, `init --portable`, `runs diagnose --json`, and `runs cleanup --dry-run --json`. Verified the script itself, full `unittest discover`, `py_compile`, and `git diff --check`.
- 2026-05-31: Started Phase 8b onebin/run management docs pass. Scope is limited to README and docs updates for `aha runs cleanup`, `aha runs diagnose`, `aha runs lifecycle`, and `scripts/smoke_onebin_cli.py`; no feature logic or release behavior changes are included.
- 2026-05-31: Completed Phase 8b onebin/run management docs pass by updating the Chinese and English READMEs plus `docs/architecture.md` with user-facing run management commands, safety defaults, and onebin smoke usage. Verified `git diff --check`; full tests were not rerun because this slice changes documentation only.
- 2026-05-31: Completed Phase 6d/8c follow-up by adding guarded `aha runs delete` and `DELETE /api/runs/<run-id>` for physical deletion of non-current runs, with `--force` / `force=1` for heartbeat-active non-current runs. Extended onebin CLI smoke to cover run delete and added `scripts/smoke_dual_ui_homes.py` to verify source and onebin UIs use separate AHA homes and isolated run lists.
- 2026-05-31: Completed Phase 6e source service persistence by adding `scripts/install_source_user_service.sh`. The script installs `aha-src.service` with `PYTHONPATH=repo/src`, working directory at the repo root, `--home repo/.aha`, and default port 8766 so source and onebin services do not accidentally share `~/.aha`.
- 2026-05-31: Completed a post-service-hardening planning pass. Added the current remaining gaps and planned Phases 9-13 for run retention/compaction, stale runtime recovery, browser heartbeat orphan hardening, frontend DOM/state splitting, and install/update workflow hardening. This slice is documentation-only and intentionally makes no runtime behavior changes.
- 2026-05-31: Started Phase 9 run retention/compaction with a read-only first slice. Added `aha runs retention <run-id>` to report run file counts, byte totals, top-level directory groups, age buckets, largest files, and retention notes. Extended onebin CLI smoke to cover the new read-only command; no archive/trim apply behavior is included yet.
- 2026-05-31: Completed Phase 9 apply mode by adding guarded retention archives and optional force compaction. `--apply` writes a `.tar.gz` archive for default-safe `logs/` and `prompts/` candidates while keeping originals; `--apply --force` deletes only archived candidates. Current runs and active heartbeat runs are refused, `chat/` requires `--include-chat`, and destructive coverage uses only temporary fixtures and isolated onebin smoke homes.
- 2026-05-31: Completed Phase 10 stale runtime recovery by adding `aha runs recover`. The command defaults to dry-run candidate reporting, requires exact `--task-id` and `--agent-id` with `--apply`, rechecks backend status before mutation, marks stale running agents interrupted, records recovery context/events, and returns single-agent tasks to `awaiting_user`. Onebin smoke covers recover dry-run only in temporary homes.
- 2026-05-31: Completed Phase 11 browser heartbeat orphan hardening by centralizing realtime debug persistence and requiring a plan-backed run before writing `logs/realtime-debug.log`. Stale WebSocket heartbeats or client debug telemetry for deleted run ids now remain process-only diagnostics and do not recreate `runs/<id>/` directories.
- 2026-05-31: Started Phase 12 frontend DOM/state split with low-risk pure helpers. Extracted realtime transport label, reconnect delay, and stale fallback decisions into `realtime_state.js`, then moved run lifecycle filter/action row state into `run_metadata.js`; `app.js` still owns WebSocket instances, DOM rendering, fetch, and event handlers.
- 2026-05-31: Started Phase 13 install/update workflow hardening by adding dry-run service unit generation to both onebin and source installers. The dry-runs are covered by no-write unittest smoke checks for generated unit content, service paths, and shell syntax.
- 2026-05-31: Completed Phase 13 by adding `scripts/smoke_service_installers.py --json`, which runs onebin/source installer dry-runs under temporary homes, validates generated unit content, and asserts no executable or service files are written.
- 2026-05-31: Completed the final maintenance/security integration pass. Web maintenance actions are confirmation-gated, run retention has policy thresholds plus archive inspect/restore, stale recovery can explicitly restart a backend, services default to token-file auth with bind-risk reporting, and the existing onebin/service/UI/dual-home smokes cover the supported non-destructive paths. Playwright browser regression remains an environment-limited follow-up.
- 2026-05-31: Hardened Web/service bind defaults by moving `aha ui`, onebin service, and source service defaults to `127.0.0.1`; `/api/access-control` and `/api/health` now report configured bind metadata so a `0.0.0.0` service still appears risky even when opened through localhost.
- 2026-05-31: Added service/test hygiene closeout scripts: optional `scripts/smoke_playwright_ui.py`, no-write `scripts/preflight_service_upgrade.py`, installer dry-run AHA-home no-write assertions, and explicit successful-smoke run cleanup for onebin/dual-home smokes.
- 2026-05-31: Added retention scheduler/cleanup guardrail closeout: `runs retention-policy --write-report` now persists aggregate policy reports under `reports/retention-policy/`, Web UI startup runs a configured read-only scheduled reporter, and cleanup now refuses non-temp scan roots unless explicitly allowed while protecting symlink run homes and configured portable `.aha` homes without temporary runs.
