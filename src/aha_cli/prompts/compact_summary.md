# Backend Session Compact Summary

This summary was generated from AHA durable state for a backend session compact/reset.

## Trigger
- reason: `$reason`
- created_at: `$created_at`

## Task
- run_id: `$run_id`
- task_id: `$task_id`
- title: $title
- status: `$status`
- current_round_id: `$current_round_id`
- round_sequence: `$round_sequence`
- last_final_round_id: `$last_final_round_id`
- workspace: `$workspace`

## Agent
- agent_id: `$agent_id`
- role: `$role`
- backend: `$backend`
- model: `$model`
- sandbox: `$sandbox`
- approval: `$approval`

## Archived Backend Session
- backend_session_id: `$backend_session_id`
- jsonl_path: `$jsonl_path`
- jsonl_exists: `$jsonl_exists`
- size_bytes: `$size_bytes`
- latest_usage: `$latest_usage`
- latest_prompt_mode: `$latest_prompt_mode`

## Task Journal
$task_journal

## Recent Messages
$recent_messages

## Recent AHA Events
$recent_events

## Resume Guidance
- Continue from this summary and current AHA task state.
- Do not assume the archived backend transcript will be automatically resumed.
- Preserve AHA ownership, routing, and commit rules from the current task context.
