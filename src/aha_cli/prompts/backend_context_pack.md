AHA Knowledge/Nav Pull Contract:
- purpose: token-saving KB/navigation usage and maintenance rules
- mode: agent-pull; AHA provides entrypoints and rules, not keyword-selected KB content, task history, or evidence recap

Use this contract before broad repository search. Decide semantic relevance yourself from the user's request. Read exact source files before analysis or edits.

Current-task evidence protocol:
- AHA may record which KB/nav/source paths you actually read, changed, or verified.
- If KB/nav is missing, stale, wrong, or irrelevant, say so briefly when material.
- Self-growth/self-repair is current-task incremental CRUD only.
- If navigation_index is not found yet, create a minimal project navigation/index.md after the first verified source pass; keep it small and evidence-based, then grow it incrementally.
- If nav fails to locate the relevant code, or points to stale/wrong paths, first find and verify the real source path, then update or create the project navigation entry with the verified files, entrypoints, flow, and validation command.
- For project-scoped navigation/solutions/worklog, directly edit the approved KB Markdown files when current task evidence proves a fix or durable task progress; agent owns knowledge maintenance.
- Write project-scoped KB Markdown (navigation/solutions/worklog) in Chinese by default; keep code identifiers, paths, commands, and schema fields literal.
- Maintain task_worklog during the whole task lifecycle when plans, progress, decisions, requirement changes, verification, or KB/nav updates become useful; do not wait for only task start or task end.
- Manual `/aha kb` feedback is only for candidate-review flows; ordinary project task evidence should be written directly to approved project navigation/solutions/worklog Markdown.
- For ordinary project work, persist stable routes, flows, and diagnostics in project navigation, and task-specific execution history in task_worklog.
- Keep project navigation as a reachable parent-child hierarchy: `index.md` links top-level parent docs, non-index nav docs need a direct parent entry, and parent docs link only their direct child docs. Do not create orphan nav docs or keep adding every child link to `index.md`.
- New approved KB Markdown entries must use one JSON object frontmatter between `---` fences. Do not use YAML frontmatter; Web/API listing depends on JSON frontmatter parsing.
- Do not rebuild, rescan, or delete the full knowledge base as part of a task. For stale project entries, prefer a narrow deprecate/repair edit over physical deletion.
- When returning a `record_task_update` action after using KB/nav, include optional `kb_feedback` with concise `helped`, `stale`, `missed`, `updated`, or `pending` lists.
- If you directly edited approved project `navigation/solutions/worklog` Markdown, include those paths in `kb_feedback.updated`; AHA may auto-commit only those approved project KB roots. Pending candidates remain review-gated and are not auto-committed.

$knowledge_reference

Agent workflow:
- For broad project orientation, read navigation/index first when it exists, then choose the smallest relevant modules/* or flows/* docs yourself.
- Skip irrelevant KB/wiki/solutions entries. Do not use knowledge just because it exists.
- Trust order: current user request > current source and command output > current task evidence > project navigation > project solutions > general wiki.
