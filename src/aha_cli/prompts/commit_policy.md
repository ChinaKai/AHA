Commit message policy:
- Use a Conventional Commit subject: `<type>(<scope>): <summary>`.
- Include exactly one generator trailer in the commit body:
  `Generated-by: AHA Codex GPT-5.5`
- Keep task, agent, and scope tracking in the AHA journal; do not write `AHA-Task`, `AHA-Agent`, or `AHA-Scope` trailers into Git commits.
- Prefer `aha commit --type <type> --scope <scope> --summary <summary>` over raw `git commit`.
- Validate hand-written commit messages with `aha commit-check <message-file>` before committing.
