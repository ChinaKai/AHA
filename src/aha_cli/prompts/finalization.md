AHA finalize request.

Task:
- id: $task_id
- title: $title

$task_journal

Generate or update the task Final now.

Requirements:
- Return concise Markdown only.
- Use the Task journal as the primary source when it has entries.
- Preserve meaningful task rounds under `## 任务轮次` as a chronological ordered list (`1.`, `2.`, ...).
- For each round, include result plus verification, files, notes, or risks when available.
- Summarize the stable outcome of this task, not the whole noisy chat transcript.
- Include changed files or concrete decisions when relevant.
- Include verification performed when relevant.
- Include remaining risks or next steps only if they are actionable.
- Do not include internal AHA command chatter unless it directly affects the outcome.
