AHA action output:
- Reply in plain text unless this turn needs AHA actions.
- If actions are needed, return only one JSON object with `actions` and `response`.
- Supported actions: `spawn_sub`, `route_to_agent`, `record_task_update`.
- For a brand-new sub-agent: `{"type": "spawn_sub", "agent_id": null, "scope_id": "optional", "title": "complete handoff assignment", "main_followup": "optional next main-owned work", "reason": "why needed"}`.
- When delegating, include enough handoff detail for the sub-agent to work independently: relevant files or commands already inspected, key facts, ownership boundaries, expected output, and validation target.
- Include `main_followup` only when task-main should continue its own work after AHA starts or routes the sub-agent; omit it when task-main should wait for sub-agent results.
