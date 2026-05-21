Current task delta:
- task_id: $task_id
- title: $title
- status: $status
- agent_id: $agent_id
- role selected by user: $role
- backend: $backend
- workspace: $workspace
- sandbox: $sandbox
- approval: $approval
- session_policy: $session_policy
- backend_session_id: $backend_session_id

Sticky session note:
- This backend session already contains prior AHA runtime contract, previous AHA prompts, tool outputs, and replies.
- Treat this prompt as a delta update; do not assume omitted history or policies were deleted.
- Respect existing AHA sub-agent ownership and commit rules from the resumed session.
- Use the current user message below as the new request.
