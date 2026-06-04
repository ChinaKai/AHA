# AHA Web UI Optimization Design

This document tracks the product and interaction design work for improving the
AHA Web UI. It is separate from `docs/optimization-plan.md`, which is focused on
maintenance and architecture cost.

## Goals

- Make the default workspace easier to scan during repeated local agent work.
- Keep the primary loop obvious: choose a task, choose an agent, chat, inspect
  result, and finalize.
- Support English and Chinese UI copy with English as the default language.
- Reduce visual noise without removing advanced local debugging controls.
- Land changes incrementally without committing until the optimization pass is
  ready for one coordinated commit.

## Non-Goals

- Do not introduce a frontend framework.
- Do not redesign backend APIs unless a UI workflow cannot be improved without
  it.
- Do not remove existing diagnostics, archive, recovery, or context inspection
  capabilities.
- Do not commit partial optimization work.

## Current Assessment

Strengths:

- The three-column desktop layout maps well to AHA's work model: tasks,
  conversation, and agents.
- The UI already exposes useful runtime details for local debugging, including
  backend status, logs, context pressure, prompt metrics, run lifecycle, and
  maintenance actions.
- Responsive support exists for sidebar collapse, mobile sheets, composer
  behavior, and mobile keyboard handling.
- Static frontend code has already been split into smaller helper modules, which
  makes focused UI slices practical.

Problems:

- The first screen mixes primary work controls with administrative tools,
  diagnostics, archive actions, integrations, and experiments.
- Task cards and agent cards expose too many low-priority details at the same
  visual level, which slows scanning.
- Normal, destructive, and diagnostic actions often have similar weight.
- The visible copy is inconsistent across English and Chinese labels.
- CSS has useful patterns, but lacks a small design-token layer for surface,
  border, text, status, spacing, and button variants.
- Mobile hides critical context such as selected agent and backend state behind
  a compact action panel.

## Design Direction

The UI should feel like a dense local operations console, not a marketing page.
The preferred shape is compact, stable, and fast to scan:

- Use restrained full-width layout bands and small cards only for repeated
  items.
- Keep one primary action visible in each workflow area.
- Use overflow menus for secondary and dangerous actions.
- Keep advanced diagnostics available, but make them opt-in.
- Prefer consistent text labels over abbreviations unless the label is a known
  technical term.

## Language Strategy

Default language: `en-US`.

Supported languages:

- `en-US`: default, used for all first-load labels and persisted only if the
  user chooses a different language.
- `zh-CN`: secondary language for users who prefer Chinese UI copy.

Implementation guidance:

- Add a small static i18n module under `src/aha_cli/web/static/`.
- Store language preference in `localStorage` and fall back to `en-US`.
- Add a language switcher in the Run menu or Settings area.
- Use stable translation keys, for example `task.new`, `run.menu`,
  `agent.runtime`, and `conversation.final`.
- Start with visible static shell copy, task list actions, tabs, composer, run
  menu, dialogs, and common empty/error states.
- Keep backend protocol strings, command names, status enum values, and file
  paths untranslated unless they are presented as user-facing labels.

Acceptance:

- First load shows English UI text.
- Switching to Chinese updates the visible shell without a page reload.
- Refresh preserves the selected language.
- Existing command syntax such as `/aha final` remains unchanged.

## Roadmap

### P0: Information Architecture And Copy

- Move low-frequency tools such as diagnostics, Weixin, play console, archive
  import/export, and restart behind a clearer "More tools" or admin grouping.
- Simplify task cards to title, status, agent count, latest activity, and move
  selected-task actions/configuration into one flat details panel.
- Make Conversation the strongest visual center.
- Show selected agent and backend status near the composer.
- Add English/Chinese switching with English default.

### P1: Visual System

- Add CSS tokens for colors, surfaces, borders, text, status colors, spacing,
  radius, shadows, and focus rings.
- Normalize button variants: primary, secondary, ghost, danger, icon.
- Normalize badge usage so each task or agent row shows only the most useful
  statuses by default.
- Align empty states, confirmation dialogs, and error messages.

### P2: Workflow Polish

- Rework mobile navigation into a clearer bottom action model.
- Collapse prompt/context metrics by default and surface only warnings in the
  main conversation view.
- Add better task and agent scan states for running, waiting, blocked, and
  completed work.
- Add visual regression coverage through Playwright screenshots at desktop,
  tablet, and mobile sizes.

## Suggested Implementation Order

1. Add i18n infrastructure and language switcher with English default.
2. Convert static shell copy, tabs, composer, task actions, and dialogs.
3. Simplify task card rendering and move selected-task actions/configuration
   into one flat details panel.
4. Group run/admin tools into clearer sections.
5. Introduce CSS tokens and button/status variants.
6. Refine mobile navigation and visible context.
7. Add Playwright screenshot checks for the optimized views.

## Verification Plan

- `python3 -m pytest tests/test_frontend_static.py`
- `python3 scripts/smoke_playwright_ui.py --require-playwright`
- Manual check at 1280x800, 980px width, and 390px mobile width.
- Confirm language switch persistence across refresh.
- Confirm no existing command syntax or backend status enum is translated.

## Progress

| Time | Status | Notes |
| --- | --- | --- |
| 2026-06-02 | Done | Completed read-only UI assessment. |
| 2026-06-02 | Done | Captured default-English bilingual copy requirement. |
| 2026-06-02 | Done | Created this design document as the coordination artifact for the UI optimization pass. |
| 2026-06-02 | Done | Added static i18n infrastructure, English default shell copy, a Run-menu language switcher, locale-aware time formatting, and targeted static/Playwright smoke coverage. |
| 2026-06-02 | Done | Localized high-priority dynamic copy for login, access/run action status, maintenance diagnostics, pending messages, Weixin console, and Play console. |
| 2026-06-02 | Done | Localized remaining dynamic copy in task metadata, task creation guard text, run metadata, status switching/error text, and agent runtime config confirmation. |
| 2026-06-02 | Done | Added Playwright screenshot regression support for desktop, tablet, and mobile en-US/zh-CN views and verified the first screenshot set. |
| 2026-06-02 | Done | Completed P0 second-stage hierarchy pass: grouped low-frequency Run tools, folded task secondary actions into details, added initial CSS tokens/button/status levels, and made composer/agent status context visible. |
| 2026-06-02 | Done | Tightened visual consistency after review: prevented utility button wrapping, reduced repeated task/agent metadata, and normalized danger/action button treatment. |
| 2026-06-02 | Done | Reviewed desktop/tablet/mobile screenshots and tightened again: simplified timeline/composer/backend status repetition, moved compact button/card rhythm onto shared tokens, and kept task/agent cards less diagnostic-heavy. |
| 2026-06-02 | Done | Final small visual pass: normalized mobile conversation filter chips, localized their labels, and folded dense agent runtime controls behind a low-weight summary row. |
| 2026-06-02 | Done | Minimal layout pass: folded Run utilities, task configuration, task filters, conversation filters, and agent config into More/details so the default screen focuses on tasks, conversation, and the current agent summary. |
| 2026-06-02 | Done | Merged Task options and per-card Actions into one selected-task Task details panel with flat sections for actions, task list filtering, proxy, supervision, and context. |
| 2026-06-02 | Done | Hid the conversation filter entry when there are no conversation events, reducing adjacent empty fold controls in the default workspace. |
| 2026-06-02 | Done | Repositioned task controls after review: task visibility filters now sit directly above the task list, and selected task settings moved into the task sidebar so Conversation remains the main workspace. |
| 2026-06-02 | Done | Replaced the default Task details fold with a lightweight per-task settings icon; Task settings now opens on demand, with proxy/supervision/context in the body and task actions at the bottom. |
| 2026-06-02 | Done | Converted Task settings from an inline sidebar panel to a gear-anchored popover, with mobile using a bottom sheet and close behavior for same-trigger toggle, task row clicks, outside clicks, and Escape. |
| 2026-06-02 | Done | Fixed Task settings interaction bugs: gear now opens settings without switching the active Conversation task, settings forms operate on the gear task, and mobile Tasks/Close/outside/Escape flows are covered by smoke tests. |
| 2026-06-02 | Done | Tightened the simplified default workspace: strengthened selected task state, restored always-visible conversation filter chips, removed the redundant composer target status row, and replaced Agent More/details nesting with a gear-driven settings popover/mobile sheet. |
| 2026-06-02 | Done | Refined Agent settings so runtime config controls use a single-column rhythm. |
| 2026-06-02 | Done | Stabilized Conversation filter chips as a non-shrinking header layer so the chat panel and composer cannot cover them. |
| 2026-06-02 | Done | Moved task view switching into the selected task card, kept desktop Conversation chrome out of the center workspace, and changed Conversation filters into a lightweight popover with a mobile menu entry. |
| 2026-06-02 | Done | Refined the Conversation filter entry into a selected-task icon rail above the task settings gear, kept non-selected task cards lighter, and preserved the filter popover open state across filter changes. |
| 2026-06-02 | Done | Made Conversation filter behavior match task settings: the same icon toggles open/closed, outside clicks close it, mobile uses the same selected-task icon entry, and the icon is now a clearer funnel. |
| 2026-06-02 | Done | Reorganized the Run console into Current run, Runs, Data, Run diagnostics, System, and Integrations sections so navigation, data import/export, diagnostics, and utilities no longer compete in one mixed group. |

## Open Decisions

- Whether the language switch belongs in the compact Run menu or the Settings
  dialog.
- Whether Weixin and play console should stay as low-weight utilities in the Run
  menu or move under a separate integrations/tools surface.
- Whether the mobile Task settings sheet needs a dimmed backdrop after more
  hands-on testing.
