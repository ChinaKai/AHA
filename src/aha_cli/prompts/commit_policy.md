Commit message policy:
- Use a Conventional Commit subject: `<type>(<scope>): <summary>`.
- Include AHA trailers in the commit body:
  `AHA-Task: $task_id`
  `AHA-Agent: $agent_id`
  `AHA-Scope: <short-scope>`
- Prefer `aha commit --type <type> --scope <scope> --summary <summary> --task-id $task_id --agent $agent_id --aha-scope <short-scope>` over raw `git commit`.
- Validate hand-written commit messages with `aha commit-check <message-file>` before committing.
