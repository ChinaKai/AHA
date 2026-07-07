AHA Knowledge/Nav Pull Contract:
- purpose: token-saving guidance for the latest user request
- mode: agent-pull; AHA provides entrypoints and rules, not keyword-selected KB content

Use this contract before broad repository search. Decide semantic relevance yourself from the user's request. Read exact source files before analysis or edits.

Latest request:
$request

Current-task evidence protocol:
- AHA may record which KB/nav/source paths you actually read, changed, or verified.
- If KB/nav is missing, stale, wrong, or irrelevant, say so briefly when material.
- Self-growth/self-repair is current-task incremental CRUD only.
- If nav fails to locate the relevant code, or points to stale/wrong paths, first find and verify the real source path, then update or create the project navigation entry with the verified files, entrypoints, flow, and validation command.
- For project-scoped navigation/solutions, directly edit the approved KB Markdown files when current task evidence proves a fix; agent owns knowledge maintenance.
- Manual `/aha kb` and `/aha nav` feedback commands are the candidate-review path; ordinary task evidence should not wait for pending candidates.
- For ordinary project work, persist stable routes, flows, and diagnostics in project navigation.
- Do not rebuild, rescan, or delete the full knowledge base as part of a task. For stale project entries, prefer a narrow deprecate/repair edit over physical deletion.
- When returning a `record_task_update` action after using KB/nav, include optional `kb_feedback` with concise `helped`, `stale`, `missed`, `updated`, or `pending` lists.

$knowledge_reference

$evidence_reference

Agent workflow:
- For broad project orientation, read navigation/index first when it exists, then choose the smallest relevant modules/* or flows/* docs yourself.
- Skip irrelevant KB/wiki/solutions entries. Do not use knowledge just because it exists.
- Trust order: current user request > current source and command output > current task evidence > project navigation > project solutions > general wiki.
