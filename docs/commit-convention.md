# Commit Convention

Use a concise Conventional Commit subject plus one generator trailer:

```text
feat(web): add task creation API

Generated-by: AHA Codex GPT-5.5
```

Keep each commit focused on one behavior or refactor step.

Prefer `aha commit` over raw `git commit` so the message is generated consistently. Task, agent, and scope tracking stays in the AHA journal instead of the Git commit body:

```bash
aha commit \
  --type feat \
  --scope web \
  --summary "add task creation API" \
  --add README.md src/aha_cli/web
```

Use `--dry-run` to print the generated commit message without committing.

Validate hand-written commit messages with:

```bash
aha commit-check .git/COMMIT_EDITMSG
```
