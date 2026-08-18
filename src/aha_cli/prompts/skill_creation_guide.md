You are creating an AHA skill. A skill is a self-contained directory of guidance
(SKILL.md plus optional bundled files and an agents/openai.yaml interface) that
an agent loads when a task binds to it. Skills are stored under the knowledge
base's `skills/` directory and are classified as `system` (bundled, read-only)
or `personal` (user/agent-created, editable).

Your task: `$instruction`

Use the existing skill layout so skills stay compatible:
- `<skill_id>/SKILL.md` — frontmatter + markdown body
- `<skill_id>/agents/openai.yaml` — optional interface metadata
- `<skill_id>/scripts|references/...` — optional bundled files

Produce your reply as a single JSON object with these fields:
{"skill_id":"<lowercase-hyphenated id>","skill_md":"<full SKILL.md content>","openai_yaml":"<optional agents/openai.yaml content>"}

SKILL.md frontmatter requirements:
- `name`: the skill id (lowercase, digits, hyphens only).
- `description`: one line describing the capability and when to use it.
- `source`: `personal` (default for created skills) or `system` (only for bundled AHA skills).

SKILL.md body guidance (write in Chinese unless the content is code/identifiers):
- A short `# Title`.
- `## Core Rules`: durable, task-independent rules — what to check first, what
  never to do, how to keep output safe.
- `## Workflow`: concrete ordered steps an agent follows when the skill is bound.
- Keep it focused and actionable. Do not invent facts about AHA commands; only
  reference commands or paths you have verified.

agents/openai.yaml interface (optional but recommended):
interface:
  display_name: "<human label>"
  short_description: "<one line>"
  default_prompt: "<the prompt an agent uses to activate this skill>"

After composing the skill, use the `aha skill create` CLI (or the Web Skills tab)
to persist it, then confirm it appears in `aha skill list`.
