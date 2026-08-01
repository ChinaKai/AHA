# AHA Current-Code Optimization Plan (2026-08, pinned)

This is the **active, current-code-grounded** optimization plan. It supersedes
nothing — `docs/optimization-plan.md` (maintenance track, Phases 0–13) and
`docs/web-ui-optimization.md` (UI track, P0–P2) remain the historical record and
are referenced here for format and prior decisions. Update this file before and
after each slice so the next agent can resume from evidence, not chat history.

## Baseline (verified 2026-08-01, HEAD `2184c44`)

- Python source: 51,659 LOC / 148 files. JS: 29,232 LOC / 74 files. CSS: 9,539.
- Tests: 41,735 LOC / 83 files (≈ 0.8:1 test/source ratio).
- Source `TODO/FIXME/XXX/HACK` count: **0** in both Python and JS.
- `app.js` modularization (optimization-plan Phase 4/12) is effectively complete:
  `app.js` is now a 1-line shim; concerns live in ~70 focused modules.
- **File-size limit: 2000 lines** (history: 1000 → 1600 → 2000; see `AGENTS.md`
  and `docs/repository-management.md`).

### Files over the 2000-line limit (current code)

| File | Lines | Notes |
| --- | --- | --- |
| `src/aha_cli/web/static/i18n.js` | 2141 | mostly translation data tables — low split value; treat as data, not logic. Do not split mechanically. |

**No Python file and no logic-bearing JS file exceeds 2000 lines.** The only
over-limit file is translation data, which is accepted as-is.

### Near-limit files (1500–2000) — preventive split candidates

These are under the limit today but close enough that ordinary feature work
could push them over. They are the proactive split targets, using the same
extraction strategy that worked for `orchestrator.py` (Phase 3).

| File | Lines | Split direction |
| --- | --- | --- |
| `src/aha_cli/cli.py` | 1934 | argparse wiring vs command-handler bodies; `cli_parser.py` already holds parser setup. |
| `src/aha_cli/web/knowledge_routes.py` | 1856 | route handlers by concern: project identity, retrieval, navigation, markdown images. |
| `src/aha_cli/web/static/task_memo_controller.js` | 1819 | pure view-model/format helpers vs DOM/render + event wiring. |
| `src/aha_cli/services/chat.py` | 1591 | chat-loop phases vs finalization handling. |

Files comfortably compliant under 2000 (informational): `chat_prompt_context.py`
(1463), `filesystem.py` (1448), `task_routes.py` (1396), `knowledge.py` (1314),
`knowledge_distill.py` (1298), `orchestrator.py` (1223), `models.py` (1143),
`observe_proxy.py` (1095), `browser_bridge.py` (1035).

## Goals

- Keep the near-limit files from breaching 2000 lines through behavior-preserving
  preventive splits, so size pressure does not block later feature work.
- Make the 2000-line limit **enforced**, not just documented.
- Lock the already-shipped UI optimization gains behind default automated
  regression coverage instead of an environment-optional smoke.
- Keep AHA local-first, onebin-friendly, framework-free on the frontend, and
  free of new runtime dependencies.

## Non-Goals

- Do not introduce a frontend framework, a database, or new runtime deps.
- Do not rewrite the storage format or migrate task/run schema.
- Do not split `i18n.js` mechanically — it is translation data; revisit only if
  it grows past ~3000 lines or logic creeps in.
- Do not change backend protocol, action execution, or session semantics here.
- Do not split files that are comfortably under the limit just to move a number.

## Prioritized Slices

| Priority | Area | Target | Safe first slice |
| --- | --- | --- | --- |
| P0a | `cli.py` headroom | Keep below 2000 as features land. | Extract one command group (e.g. `runs` or `kb`) into a focused module; keep `cli.py`/`cli_parser.py` as thin wiring. |
| P0b | `knowledge_routes.py` headroom | Keep below 2000 as features land. | Extract one concern (project identity, retrieval, navigation, or markdown images) into a sub-module re-exported from the facade. |
| P0c | `task_memo_controller.js` headroom | Keep below 2000 as features land. | Mirror Phase 4: extract pure helpers first, leave render + event wiring in the controller. |
| P1a | Size-limit guard | Make 2000 enforceable. | Add a tiny test or `scripts/` check that fails when any `src` Python/JS file exceeds 2000 lines. |
| P1b | UI regression in default CI | Lock experience gains. | Run `scripts/smoke_playwright_ui.py` in the default pipeline when Playwright/Chromium are available; keep `status=skipped` exit only when truly absent. |
| P1c | Accessibility light pass | Operations console reachability. | Focus rings, color-contrast, keyboard reachability for primary task/agent/conversation controls; add to the Playwright screenshot set. |
| P2 | Frontend sustainability | Continue Phase 12 view-model/render splits. | One bounded panel at a time (run management or agent runtime), testable view-model helper before moving handlers. |

### Slice acceptance pattern (reuse from prior phases)

- Public behavior unchanged; existing tests pass.
- Each slice lands as one small Conventional Commit and updates this file's
  Working Log before and after.
- `py_compile`, `node --check` for touched JS, focused tests, full
  `python3 -m pytest`, and `git diff --check` all pass.

## Verification Commands

```bash
PYTHONPATH=src python3 -m pytest                                  # full suite
PYTHONPATH=src python3 -m pytest tests/test_web_task_api.py       # focused
PYTHONPATH=src python3 scripts/smoke_onebin_cli.py --json         # onebin CLI smoke
PYTHONPATH=src python3 scripts/smoke_dual_ui_homes.py             # separate homes
PYTHONPATH=src python3 scripts/smoke_playwright_ui.py --require-playwright  # UI regression
# Size guard (manual until P1a lands):
find src -name '*.py' -exec wc -l {} + | awk '$1>2000 && $2!="total"'
find src/aha_cli/web/static -name '*.js' -exec wc -l {} + | awk '$1>2000 && $2!="total"'
```

## Working Log

| Date | Status | Notes |
| --- | --- | --- |
| 2026-08-01 | Done | Raised file-size limit 1000 → 1600 in `AGENTS.md` and `docs/repository-management.md`; pinned this plan from `docs/optimization-plan.md`. Baseline verified at `2184c44`. |
| 2026-08-01 | Done | Raised limit 1600 → 2000. Under 2000 only `i18n.js` (2141, translation data, exempt) is over; `cli.py`/`knowledge_routes.py`/`task_memo_controller.js` are now under-limit near-limit preventive targets. Refreshed baseline, goals, slices, and verification commands. |
| 2026-08-01 | Pending | P0a/P0b/P0c preventive splits; P1a size guard. |
