AHA coordination policy:
- Use AHA JSON actions only for sub-agent routing or durable task updates; otherwise reply in plain text.
- Do not use backend-native sub-agent tools, and do not invent sub-agent ids.
- Spawn only for independent parallel work with disjoint ownership; task-main remains responsible for integration and final verification.
- For a new sub-agent, omit `agent_id` or set it to `null`; route only to visible existing `sub-*` agents.
- If you use actions, return only one JSON object with `actions` and `response`.

Action formats:
- `{"type":"spawn_sub","agent_id":null,"scope_id":"optional","title":"assignment","backend":"codex","model":null,"reason":"why needed"}`
- `{"type":"route_to_agent","agent_id":"sub-001","message":"follow-up"}`
- `{"type":"record_task_update","summary":"...","changed_files":[],"verification":[],"risks":[]}`
