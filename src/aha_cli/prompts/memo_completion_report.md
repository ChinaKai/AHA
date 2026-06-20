AHA MEMO completion report request.

MEMO:
- id: $memo_id
- title: $memo_title
- status: $memo_status
- completed_at: $memo_completed_at

Linked task:
- id: $task_id
- title: $task_title

Request:
- requested_at: $requested_at
- memo attachment directory: $attachment_dir

MEMO description:
$memo_description

$task_journal

Generate the MEMO completion report now.

Requirements:
- Return concise Markdown only.
- Do not modify files.
- Do not continue the task.
- Do not generate or update the task Final.
- Use the linked task context and Task journal as the primary source when available.
- If information was not recorded, write `未记录`.
- Include these sections when relevant: `## 背景`, `## 目标`, `## 完成内容`, `## 关键结论`, `## 产出物`, `## 验证情况`, `## 遗留问题`, `## 可复用经验`.
- Keep reusable knowledge as candidates only; do not claim it was written to a knowledge base.

Knowledge candidate sidecar:
- After the visible MEMO report, append exactly one machine-readable block:
  `<aha_knowledge_candidates>[...]</aha_knowledge_candidates>`.
- The visible report must remain clean; do not explain the sidecar in the report.
- Each candidate must be reusable knowledge, not a copy of the report. Ordinary one-off bug fixes should usually produce no candidate; project structure discoveries should update navigation/module docs instead of becoming solution entries. Use this JSON shape:
  `{"kind":"solutions","title":"...","body":"...","tags":[],"related_files":[],"invalid_when":"...","confidence":0.7}`.
- For `kind="solutions"`, `body` must use these Markdown sections: `## 适用场景`, `## 问题 / 触发信号`, `## 推荐做法`, `## 关键位置`, `## 验证方式`, `## 失效条件 / 适用边界`.
- For `kind="wiki"`, use only for non-project general tutorials/reference docs; set `"scope":"general"`. `body` must use these Markdown sections: `## 结论`, `## 适用范围`, `## 规则 / 约定`, `## 示例`, `## 相关位置`, `## 更新条件`.
- For `kind="navigation"`, emit ONLY when this work changed a module's responsibility, entry point, architecture, key source locations, or known blind spot. Navigation updates are incremental: update only the affected `"index"`, `"modules/<module-slug>"`, `"modules/<module>/<child-slug>"`, `"flows/<flow-slug>"`, or `"flows/<flow>/<child-slug>"` document, never regenerate the whole project navigation from ordinary work. Each navigation document only owns one layer of links: index links first-level modules/flows, module/flow docs link only their direct children. If root index is missing, AHA will bootstrap it from the workspace; if a non-root child doc has no direct parent entry, AHA will add a minimal parent-link candidate. Do not expand grandchildren into index. Carry forward still-correct content and only adjust what changed.
- Prefer 0-3 high-quality candidates. Only exceed 3 when there are more truly independent reusable lessons.
- If there is no reusable knowledge, use an empty list: `<aha_knowledge_candidates>[]</aha_knowledge_candidates>`.
