# Commit Convention

Use a concise Conventional Commit subject plus AHA trailers:

```text
feat(web): add task creation API

AHA-Task: task-001
AHA-Agent: main
AHA-Scope: task-creation-api
```

Keep each commit focused on one behavior or refactor step.

Prefer `aha commit` over raw `git commit` so the trailers are generated consistently:

```bash
aha commit --type feat --scope web --summary "add task creation API" --task-id task-001 --agent main --aha-scope task-creation-api
```

Validate hand-written commit messages with:

```bash
aha commit-check .git/COMMIT_EDITMSG
```
