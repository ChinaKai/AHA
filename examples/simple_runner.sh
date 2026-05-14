#!/usr/bin/env bash
set -eu

{
  echo "## Summary"
  echo "Simple runner handled ${AHA_TASK_ID}."
  echo
  echo "## Findings"
  echo "- Prompt file: ${AHA_PROMPT_FILE}"
  echo "- Inbox file: ${AHA_INBOX_FILE}"
  echo "- Events file: ${AHA_EVENTS_FILE}"
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"run_id\":\"${AHA_RUN_ID}\",\"type\":\"runner_note\",\"data\":{\"task_id\":\"${AHA_TASK_ID}\",\"message\":\"simple runner started\"}}" >> "${AHA_EVENTS_FILE}"
  echo
  echo "## Files Read"
  echo "- ${AHA_PROMPT_FILE}"
  echo
  echo "## Files Changed"
  echo "- none"
  echo
  echo "## Commands Run"
  echo "- examples/simple_runner.sh"
  echo
  echo "## Risks"
  echo "- This is a demo runner, not a real agent."
  echo
  echo "## Suggested Merge Notes"
  echo "- Replace this runner with a real agent backend."
} > "${AHA_OUTPUT_FILE}"
