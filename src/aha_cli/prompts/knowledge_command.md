AHA knowledge-base feedback request.

Write pending knowledge-base candidates directly from the current sticky session context according to the user request below.

Context:
$knowledge_command_context

Rules:
- Use only your existing backend session context and the user request. AHA has not prepared extra source context for this command.
- Do not run broad repository search, inspect files, or start a new task only for knowledge feedback.
- Do not generate project navigation entries here. Ordinary task evidence should update approved project navigation directly during the task.
- If there is useful reusable project knowledge, write it as a pending candidate with `python3 -m aha_cli --home <aha_home> kb add --pending --scope project --kind solutions --project <project_key> ...`.
- Use `solutions` for project-specific reusable decisions, fixes, procedures, or workflows.
- Use `wiki` only for general or personal explanatory knowledge that is not project navigation.
- Write candidate titles and body text in Chinese by default; keep code identifiers, paths, commands, and schema fields literal.
- Return concise visible Markdown for the user, including candidate id/path/count when a candidate is written.
- Do not append `<aha_knowledge_candidates>` or any hidden sidecar. The agent writes pending candidates directly through the CLI.
- If there is no reusable knowledge, say that no candidate was written.
- Do not invent image paths in candidate Markdown. If an already available run/conversation image belongs in the article, put its current Markdown image link directly in the candidate `body` (for example a `task_memo_assets/...` link); do not merely say AHA should add the image later. When a candidate is approved, AHA can promote referenced run image attachments into entry-local `assets/<entry-slug>/<filename>` files. When directly editing an approved knowledge entry, store or copy each image beside that entry under `assets/<entry-slug>/<filename>` and add frontmatter `assets` metadata with `path`, `name`, `original`, `mime`, and `size`.
- SVG is supported as a normal image asset. Reference existing SVGs with Markdown image syntax using their real path, for example `task_memo_assets/.../diagram.svg` or `assets/<entry-slug>/diagram.svg`; do not paste raw SVG markup into the article body unless the user explicitly asks for source code.

User request:
$instruction
