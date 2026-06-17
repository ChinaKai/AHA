Commit message policy:
- Use a Conventional Commit subject: `<type>(<scope>): <summary>`.
- Include a concise commit body describing the concrete changes before the generator trailer.
- Include exactly one generator trailer as the last non-empty line, using this task agent's backend/model:
  `Generated-by: $generated_by`
- Keep task, agent, and scope tracking in the AHA journal; do not write `AHA-Task`, `AHA-Agent`, or `AHA-Scope` trailers into Git commits.
- Prefer `aha commit --type <type> --scope <scope> --summary <summary> --body <change-summary>` over raw `git commit`.
- Validate hand-written commit messages with `aha commit-check --generated-by "$generated_by" <message-file>` before committing outside an AHA task environment.
