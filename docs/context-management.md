# Context Management Notes

This document captures the current understanding of AHA backend context
management. It is a design note, not yet a complete implementation contract.

## Context Sources

A backend turn is built from two main sources:

```text
turn context ~= backend resume(previous accumulated thread)
              + AHA current injection
              + current user message
```

During the turn, the backend response, tool calls, command output, file reads,
and diff output may become part of the backend thread. On the next turn, that
material can come back through `backend resume(...)`.

## AHA Persistent State

AHA persists task and run state locally, for example:

```text
runs/<run-id>/events.jsonl
runs/<run-id>/tasks/<task-id>/messages.jsonl
runs/<run-id>/tasks/<task-id>/task.json
runs/<run-id>/tasks/<task-id>/sessions/<agent-id>.json
runs/<run-id>/tasks/<task-id>/rounds/<round-id>/final.md
runs/<run-id>/results/task-<id>.md
```

These files are the durable record of the run, but they are not identical to
the model context. Repository files, AHA logs, task journals, and result files do
not enter the backend context automatically. They only count when AHA injects
them into the prompt, or when the backend reads them through a tool or command.

## Backend Sticky Sessions

For sticky sessions, AHA stores a backend-native resumable session id:

```text
backend_session_id
```

For Codex, this is the Codex thread id. For Claude, this is the Claude session
id. AHA uses that id to resume the backend session on later turns. The old
thread content is restored by the backend from its own session storage/cache,
not by AHA replaying every line from `messages.jsonl` or `events.jsonl`.

The effective context seen by the model is therefore:

```text
effective context = backend-restored thread + AHA current injected package
```

## AHA Current Injection

On each turn, AHA assembles a current input package. Today this can include:

```text
fixed AHA prompt and routing rules
current run/task/agent status
task journal
recent events
latest user message
ownership, sandbox, approval, proxy, and commit policies
finalization or reopen instructions when relevant
```

This injection is useful for recovery and coordination, but it is also one of
the main context costs.

## Accumulation Behavior

Context grows through two paths:

```text
1. AHA injects a large current package on each turn.
2. Backend sticky resume brings back previous thread content, including earlier
   AHA injections and backend-generated output.
```

This creates a compounding effect:

```text
turn N = previous thread
       + new AHA injection
       + new user message
       + new backend/tool output
```

The next turn may include the whole accumulated thread again through backend
resume, plus another AHA injection.

## Practical Consequences

Large context pressure usually comes from a combination of:

```text
large AHA per-turn injections
long sticky backend threads
verbose tool or command output
repeated full run/task snapshots
unbounded recent event history
```

The `task-019` context overflow matched this pattern: AHA injected large status
blocks, the sticky Codex thread retained earlier turns, and backend output kept
adding to the resumed thread.

## Management Direction

Context management should address both sides:

```text
1. Reduce AHA per-turn injection.
2. Reset or compress backend sticky sessions when they become too large.
```

Potential policies:

```text
inject summaries instead of full run snapshots by default
cap recent events by count and size
store large logs and command output by path, not by prompt copy
write durable round/task summaries before resetting a backend session
start a fresh backend session after final/reopen when appropriate
start a fresh backend session after context-window errors
use `/aha phase <phase> [summary]` to checkpoint and freshen the selected agent at explicit phase boundaries
keep backend sessions as execution continuity, not as the source of truth
```

New tasks keep agent context auto compact off by default, with a 75%
context-pressure threshold prefilled for users who choose to enable it. The task
creation dialog exposes this policy so users can turn it on or adjust the
threshold before the first backend session starts. Existing tasks without stored
context-management metadata keep legacy compatibility defaults until they are
explicitly edited.

The durable source of truth should be AHA state, task journals, round summaries,
finals, decisions, changed files, verification, and risks. Backend sticky
threads should be treated as useful but disposable execution state.

## Open Questions

```text
What exact token or character budget should AHA enforce per injection?
Which fields belong in the default prompt versus a debug-only expansion?
When should AHA rotate backend_session_id automatically?
How should AHA summarize a session before reset?
How should users inspect omitted context when needed?
```
