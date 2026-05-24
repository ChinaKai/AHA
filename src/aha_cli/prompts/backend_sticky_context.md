Current task delta:
- task_id: $task_id
- title: $title
- status: $status
- agent_id: $agent_id
- role selected by user: $role
- backend: $backend
- workspace: $workspace
- collaboration_mode: $collaboration_mode
- max_sub_agents: $max_sub_agents
- sandbox: $sandbox
- approval: $approval
- session_policy: $session_policy
- backend_session_id: $backend_session_id

Sticky session note:
- This backend session already contains prior AHA runtime contract, previous AHA prompts, tool outputs, and replies.
- Treat this prompt as a delta update; do not assume omitted history or policies were deleted.
- Respect existing AHA sub-agent ownership and commit rules from the resumed session.
- Use the current user message below as the new request.

Intent priority policy:
- Current user message > task journal / active intent > compact summary / recent messages > original task description.
- Treat task.description as the original request / historical background. It does not automatically remain the current todo after later rounds.
- If requirements were completed, superseded, explicitly excluded, or changed by later user messages, follow the latest active intent and next action instead of replaying the original request.
