AHA action output:
- Reply in plain text unless this turn needs AHA actions.
- If actions are needed, return only one JSON object with `actions` and `response`.
- Supported actions: `spawn_sub`, `route_to_agent`, `record_task_update`.
- For a brand-new sub-agent: `{"type": "spawn_sub", "agent_id": null, "scope_id": "optional", "title": "assignment", "reason": "why needed"}`.
