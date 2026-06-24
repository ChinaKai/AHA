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

Generate the MEMO completion report now.

Requirements:
- Return concise Markdown only.
- Do not modify files.
- Do not continue the task.
- Do not generate or update the task Final.
- Use your resumed backend session context. AHA is not replaying the full task history in this request.
- If information was not recorded, write `未记录`.
- Include these sections when relevant: `## 背景`, `## 目标`, `## 完成内容`, `## 关键结论`, `## 产出物`, `## 验证情况`, `## 遗留问题`, `## 可复用经验`.
