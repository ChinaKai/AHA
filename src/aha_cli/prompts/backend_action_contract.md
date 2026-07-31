AHA action output:
- Reply in plain text unless this turn needs AHA actions.
- If actions are needed, return only one JSON object with `actions` and `response`.
- Supported actions: `spawn_sub`, `route_to_agent`, `record_task_update`.
- For a brand-new sub-agent: `{"type": "spawn_sub", "agent_id": null, "scope_id": "optional", "title": "short handoff label", "assignment": "complete handoff assignment", "main_followup": "optional next main-owned work", "reason": "why needed"}`.
- Put the complete independent handoff in `assignment`: relevant files or commands already inspected, key facts, ownership boundaries, expected output, and validation target. Keep `title` short. Older payloads without `assignment` remain compatible by using `title` as the assignment.
- Include `main_followup` only when task-main should continue its own work after AHA starts or routes the sub-agent; omit it when task-main should wait for sub-agent results.
