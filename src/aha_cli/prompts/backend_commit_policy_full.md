Commit ownership policy:
- Commit, revert, and repository-change finalization requests are ownership-sensitive.
- When you are `task-main`, route commit work to the sub-agent that owns the changed scope when one exists.
- When you are a sub-agent, commit only files covered by your `assignment` / `created_reason`; if the requested commit is outside your scope, report back to `task-main`.
- Before any commit, inspect `git status`, avoid unrelated or user changes, and follow the AHA commit message policy below.

$commit_message_policy
