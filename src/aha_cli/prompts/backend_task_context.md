Task context:
- task_id: $task_id
- title: $title
- status: $status
- role selected by user: $role
- workspace: $workspace
$project_map_context
- collaboration_mode: $collaboration_mode
- max_sub_agents: $max_sub_agents
$agent_context
$task_skills_context
$hardware_debug_context
$final_context
$task_journal
$compact_summary

Intent priority: latest user message first; task description and older summaries are background unless the latest message keeps them active.

$action_contract
$coordination_policy

Repository guard: do not commit, revert, merge, delete, or finalize repository changes unless the current request asks for it.
$commit_policy
