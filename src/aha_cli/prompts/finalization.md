AHA finalize request.

Task:
- id: $task_id
- title: $title

$final_context

$task_journal

Generate or update the task Final now.

Requirements:
- Return concise Markdown only.
- Use the Task journal as the primary source when it has entries.
- Summarize only the Final source range above.
- Preserve meaningful task rounds under `## 任务轮次` as a chronological ordered list (`1.`, `2.`, ...).
- For each round, include result plus verification, files, notes, or risks when available.
- Summarize the stable outcome of this task, not the whole noisy chat transcript.
- Include changed files or concrete decisions when relevant.
- Include verification performed when relevant.
- Include remaining risks or next steps only if they are actionable.
- Do not include internal AHA command chatter unless it directly affects the outcome.

Knowledge candidate sidecar:
- After the visible Final, append exactly one machine-readable block:
  `<aha_knowledge_candidates>[...]</aha_knowledge_candidates>`.
- The visible Final must remain clean; do not explain the sidecar in the Final.
- Each candidate must be reusable knowledge, not a copy of the Final. Ordinary one-off bug fixes should usually produce no candidate; project structure discoveries should update navigation/module docs instead of becoming solution entries. Use this JSON shape:
  `{"kind":"solutions","title":"...","body":"...","tags":[],"related_files":[],"invalid_when":"...","confidence":0.7}`.
- For `kind="solutions"`, `body` must use these Markdown sections: `## 适用场景`, `## 问题 / 触发信号`, `## 推荐做法`, `## 关键位置`, `## 验证方式`, `## 失效条件 / 适用边界`.
- For `kind="wiki"`, use only for non-project general tutorials/reference docs; set `"scope":"general"`. `body` must use these Markdown sections: `## 结论`, `## 适用范围`, `## 规则 / 约定`, `## 示例`, `## 相关位置`, `## 更新条件`.
- For `kind="navigation"`, emit ONLY when this task changed a module's responsibility, entry point, architecture, key source locations, or known blind spot. Navigation updates are incremental: update only the affected `"index"`, `"modules/<module-slug>"`, `"modules/<module>/<child-slug>"`, `"flows/<flow-slug>"`, or `"flows/<flow>/<child-slug>"` document, never regenerate the whole project navigation from an ordinary task. Each navigation document only owns one layer of links: index links first-level modules/flows, module/flow docs link only their direct children. If root index is missing, AHA will bootstrap it from the workspace; if a non-root child doc has no direct parent entry, AHA will add a minimal parent-link candidate. Do not expand grandchildren into index. Carry forward still-correct content and only adjust what changed.
- Prefer 0-3 high-quality candidates. Only exceed 3 when there are more truly independent reusable lessons.
- If there is no reusable knowledge, use an empty list: `<aha_knowledge_candidates>[]</aha_knowledge_candidates>`.
