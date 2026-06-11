# AHA Token Savings Plan

This is the running plan for task-029. Keep it updated before and after each
implementation slice so later turns do not have to reconstruct state from chat
history.

## Goal

Reduce token growth in AHA backend conversations without weakening task
ownership, commit safety, sub-agent routing, or recovery after backend session
reset.

The target model is:

```text
effective backend input = backend sticky history
                        + current AHA injection
                        + current user message
```

AHA can save tokens by reducing current injection, rotating oversized sticky
history, and replacing large repeated context with durable references.

## Current Progress

| Phase | Status | Commit | Notes |
| --- | --- | --- | --- |
| P0. Quantify overhead | Done | `3bfb34c` | Added `aha_prompt_tokens`, `backend_input_tokens`, `estimated_backend_history_tokens`, and `aha_overhead_ratio`; surfaced them in backend usage breakdown. |
| P1. Compact reset continuation | Done | `3bfb34c` | Normal full prompts can inject a bounded compact summary after `backend_session_id` is cleared. |
| P2. Turn-end auto compact | Done | `3bfb34c` | Added turn-end auto compact/reset that archives the old native session without stopping the idle worker. |
| P3. Protocol/rules on-demand injection | Done | `dc1afb5` | Full chat prompts now keep short action/commit reminders by default and inject long coordination/commit policies only on matching intent. |
| P4. Tool output references | Done | `d479fbd` | Large Codex command output and Claude tool results now keep bounded `output_tail` plus `output_ref` artifact metadata. |
| P5. Phase fresh session | Done | this slice | Added explicit `/aha phase <phase> [summary]` checkpointing that clears oversized native sessions without stopping the worker. |

## P3: Protocol And Rules On-Demand Injection

Problem: stable AHA instructions, commit policy, routing rules, and collaboration
protocol can be injected repeatedly even when a sticky backend session already
contains them.

Implementation slices:

1. Add prompt helpers that identify when full policy text is necessary.
2. Keep first-turn/full prompts conservative.
3. For sticky delta prompts, include short versioned reminders by default.
4. Expand full commit policy only for commit/revert/finalization intent.
5. Expand full sub-agent routing policy only for task-main coordination or
   explicit delegation actions.

Acceptance:

- Sticky prompts keep the AHA action JSON contract visible.
- Commit policy remains present when commit/revert/finalization is requested.
- Sub-agent ownership rules remain present when main is expected to route or
  spawn.
- Existing prompt and action parsing tests continue to pass.

## P4: Tool Output References

Problem: AHA already stores many event payloads compactly, but backend-native
sessions can still accumulate large command/tool outputs. AHA-owned outputs
should be referenced instead of repeated when practical.

Implementation slices:

1. Add a small artifact/reference helper for AHA-owned large text.
2. Store oversized previews under the run/task artifact area.
3. Inject only a short preview plus path/reference metadata.
4. Expose enough metadata for session debug and future UI inspection.

Acceptance:

- Large AHA-owned text has a durable path/reference.
- Prompt/event previews stay bounded.
- Existing archive/export behavior remains safe.
- Tests cover the reference format and fallback for small text.

## P5: Phase Fresh Session

Problem: research, planning, implementation, verification, and finalization often
need different context. Reusing one backend-native session for every phase keeps
old context alive longer than necessary.

Implementation slices:

1. Add a durable phase field/checkpoint helper for task agent sessions.
2. Allow selected phase transitions to compact/reset the native session.
3. Keep AHA journal, task updates, and compact summaries as the source of truth.
4. Default to safe explicit transitions first; avoid surprising automatic phase
   switches.

Acceptance:

- A phase transition can create a compact checkpoint and clear
  `backend_session_id`.
- The next turn receives enough summary to continue.
- No active backend process is killed unexpectedly.
- Tests cover the reset payload and follow-up prompt continuity.

## Global Acceptance

- `python3 -m pytest` focused tests for touched behavior pass after every slice.
- `python3 -m compileall -q src/aha_cli` passes before each commit.
- `git diff --check` passes before each commit.
- Commit messages follow AHA policy and include exactly one
  `Generated-by: AHA Codex GPT-5.5` trailer.
- The task remains recoverable from docs plus AHA journal without relying on a
  long backend sticky thread.

## Next Step

Run the focused P5 verification, then do a final cross-slice smoke over prompt
policy injection, output references, phase transitions, compact continuation,
and frontend static command coverage.
