# AHA Subtask

Goal:
$goal

Task:
$task_title

Mode:
$mode

Rules:
- $mutability
- Do not revert user changes.
- Report facts with file paths when possible.
- Keep the result structured and concise.

Write scope:
$write_scope

Expected output sections:
## Summary
## Findings
## Files Read
## Files Changed
## Commands Run
## Risks
## Suggested Merge Notes

Realtime protocol:
- Read user/main-agent messages from $$AHA_INBOX_FILE when your runner supports it.
- Append JSON events to $$AHA_EVENTS_FILE when your runner supports it.
- Write final Markdown output to $$AHA_OUTPUT_FILE.
