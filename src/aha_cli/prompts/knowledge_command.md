AHA knowledge-base feedback request.

Generate knowledge-base candidates from the current sticky session context according to the user request below.

Rules:
- Use only your existing backend session context and the user request. AHA has not prepared extra context for this command.
- Do not run commands, inspect files, or start a new task only for knowledge feedback.
- Do not generate project navigation entries here. Task Final is responsible for project nav feedback.
- Use `solutions` for project-specific reusable decisions, fixes, procedures, or workflows.
- Use `wiki` only for general or personal explanatory knowledge that is not project navigation.
- Return concise visible Markdown for the user.
- If there is useful knowledge to save, append exactly one hidden sidecar:
`<aha_knowledge_candidates>[...]</aha_knowledge_candidates>`
- If there is no reusable knowledge, append:
`<aha_knowledge_candidates>[]</aha_knowledge_candidates>`

Candidate JSON fields:
- `kind`: `solutions` or `wiki`
- `scope`: `project`, `personal`, or `general`
- `title`: short title
- `body`: clean reusable Markdown
- `tags`: list of strings
- optional `related_files`: list of paths already known from this session
- optional `confidence`: number from 0 to 1

User request:
$instruction
