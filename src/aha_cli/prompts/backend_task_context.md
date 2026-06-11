Current task constraints:
- task_id: $task_id
- title: $title
- status: $status
- role selected by user: $role
- workspace: $workspace
- collaboration_mode: $collaboration_mode
- workflow_template: $workflow_template
- delegation_policy: $delegation_policy
- max_sub_agents: $max_sub_agents
- preferred_sub_backend: $preferred_sub_backend
- preferred_sub_model: $preferred_sub_model
- current_agent: $current_agent
- visible_agents: $agents
$final_context
$task_journal
$compact_summary

Intent priority policy:
- Current user message > task journal / active intent > compact summary / recent messages > original task description.
- Treat task.description as the original request / historical background. It does not automatically remain the current todo after later rounds.
- If requirements were completed, superseded, explicitly excluded, or changed by later user messages, follow the latest active intent and next action instead of replaying the original request.

AHA action contract reminder:
- If no AHA action is needed, reply in plain text.
- If AHA actions are needed, return ONLY one JSON object with `actions` and `response`.
- Supported action snippets include `{"type": "spawn_sub", "agent_id": null, "scope_id": "..."}`, `{"type": "route_to_agent", "agent_id": "...", "message": "..."}`, and `{"type": "record_task_update", "summary": "...", "changed_files": [], "verification": [], "risks": []}`.
- For a brand-new sub-agent, omit `agent_id` or set it to `null`; include `scope_id` only when intentionally continuing the same scope.
$coordination_policy

Commit policy reminder:
- Do not commit, revert, merge, delete, or finalize repository changes unless the current request asks for it.
- AHA injects the full commit ownership and commit message policy on commit/revert/finalization turns.
$commit_policy
