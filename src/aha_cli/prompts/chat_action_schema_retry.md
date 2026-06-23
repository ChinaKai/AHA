$action_retry_schema

AHA runtime rejected your previous reply before executing actions.
Reason: $reason.
Return exactly one JSON object with an `actions` array and `response` string.
Allowed action types are `route_to_agent`, `spawn_sub`, and `record_task_update`.
Do not use top-level `type` or `action`; do not wrap the JSON in Markdown.
Continue from the latest active user request.
