AHA project navigation feedback request.

Generate project navigation candidates from the current sticky session context according to the user request below.

Rules:
- Use only your existing backend session context and the user request. AHA has not prepared extra context for this command.
- Do not run commands, inspect files, scan the workspace, or start a new task only for navigation feedback.
- Do not generate `solutions` or `wiki` candidates here. `/aha kb` is responsible for ordinary knowledge feedback.
- Write candidate titles, summaries, diagnostics, and navigation reasons in Chinese by default; keep code identifiers, paths, commands, and schema fields literal.
- Return concise visible Markdown for the user.
- If there is useful project navigation to save, append exactly one hidden sidecar:
`<aha_knowledge_candidates>[...]</aha_knowledge_candidates>`
- If there is no useful navigation update, append:
`<aha_knowledge_candidates>[]</aha_knowledge_candidates>`

Candidate JSON fields:
- `kind`: `navigation`
- `scope`: `project`
- `slug`: `index`, `modules/<module>`, or `flows/<flow>`
- `title`: short title
- `responsibility` or `summary`: the reusable navigation update
- `related_files`: list of paths already known from this session
- optional `entry_points`: commands, APIs, routes, or functions
- optional `diagnostic_paths`: troubleshooting or investigation paths
- `navigation_reason`: why this update improves future project navigation
- optional `confidence`: number from 0 to 1

User request:
$instruction
