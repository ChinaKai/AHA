AHA Knowledge/Map Pull Contract:
- purpose: token-saving guidance for the latest user request
- mode: agent-pull; AHA provides entrypoints and rules, not keyword-selected KB content
- map_refresh: not performed during prompt assembly

Use this contract before broad repository search. Decide semantic relevance yourself from the user's request. Read exact source files before analysis or edits.

Latest request:
$request

Current-task evidence protocol:
- AHA may record which KB/nav/map/source paths you actually read, changed, or verified.
- If KB/nav/map is missing, stale, wrong, or irrelevant, say so briefly when material.
- Self-growth/self-repair is current-task incremental CRUD only.
- If nav/map fails to locate the relevant code, or points to stale/wrong paths, first find and verify the real source path, then update or create the project navigation entry with the verified files, entrypoints, flow, and validation command.
- For project-scoped navigation/solutions, directly edit the approved KB Markdown files when current task evidence proves a fix; agent owns knowledge and map maintenance. Refresh stale generated map cache and repair map logic source when needed.
- Manual `/aha kb` and `/aha nav` feedback commands are the candidate-review path; ordinary task evidence should not wait for pending candidates.
- Do not hand-edit generated Project Map cache files. If map output is stale, refresh it; if map generation or query is wrong, repair the extractor, schema, resolver, query expansion, ranking, or refresh logic when that is in scope.
- For ordinary project work, persist stable routes, flows, and diagnostics in project navigation instead of generated map cache. For AHA/map work, treat repeated map miss/stale/ranking failures as defects in map logic.
- Do not rebuild, rescan, or delete the full knowledge base as part of a task. For stale project entries, prefer a narrow deprecate/repair edit over physical deletion.
- When returning a `record_task_update` action after using KB/nav/map, include optional `kb_feedback` with concise `helped`, `stale`, `missed`, `updated`, or `pending` lists.

$knowledge_reference

$map_reference

$evidence_reference

Agent workflow:
- For broad project orientation, read navigation/index first when it exists, then choose the smallest relevant modules/* or flows/* docs yourself.
- Use Project Map or `/aha map query <terms>` only when it helps locate concrete files, symbols, configs, build records, DTS, tests, or entry points.
- Skip irrelevant KB/wiki/solutions entries. Do not use knowledge just because it exists.
- Trust order: current user request > current source and command output > current task evidence > project navigation > project solutions > general wiki.
