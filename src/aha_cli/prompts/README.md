# AHA Prompt Templates

AHA 的所有 LLM 提示词集中在此目录，通过 `aha_cli.services.prompt_templates.render_prompt_template`
渲染（`string.Template` 语法，`$var` 占位符）。

## 命名规范

模板文件名必须使用 `snake_case`，并以下列功能域前缀开头：

| 前缀 | 功能域 | 示例 |
|---|---|---|
| `backend_` | 主 prompt 组装（full/delta 上下文、recovery、request policy） | `backend_chat_full.md` |
| `chat_` | 回合交互与重试 | `chat_action_schema_retry.md` |
| `knowledge_` | 知识库 capture/distill/nav | `knowledge_capture_prompt.md` |
| `navigation_` | 项目导航 | `navigation_command.md` |
| `service_assistant_` | 飞书私聊管家 | `service_assistant_identity.md` |
| `feishu_group_` | 飞书群聊数字人 | `feishu_group_digital_human_identity.md` |
| `supervision_` | 监督 host | `supervision_host_contract.md` |
| `finalization` | 任务收尾 | `finalization.md` |
| `subtask` | 子代理任务 | `subtask.md` |
| `runner_` | backend runner | `runner_claude.md` |
| `workflow_guidance_` | 工作流模式 | `workflow_guidance_bugfix.md` |
| `mode_instruction_` | 回合模式指令 | `mode_instruction_default.md` |
| `task_` | 回合摘要、任务分配、skills 上下文 | `task_round_summary.md` |
| `commit_policy` | 提交策略 | `commit_policy.md` |
| `compact_summary` | 压缩摘要 | `compact_summary.md` |
| `action_` | 动作 schema 校验 | `action_invalid_schema.md` |
| `hardware_debug_` | 硬件调试 | `hardware_debug_context.md` |
| `browser_control_` | 浏览器控制 | `browser_control_context.md` |
| `memo_` | Memo 完成报告 | `memo_completion_report.md` |

## 占位符命名约定

- 一律 `snake_case`。
- 语义明确的缩写：`task_id`、`run_id`、`agent_id`、`workspace`。
- `task_title` 指 Task 标题；`title` 仅用于无 Task 场景的通用标题。两者不要在同一模板混用。
- 布尔/枚举值用 `mode`、`status`、`reason`、`scope` 等，避免 `flag`、`enabled` 这类模糊命名。
- 新增占位符必须在 `tests/test_prompt_templates.py::SAMPLE_VALUES` 中补充示例值。

## 维护规则

- 只允许通过 `render_prompt_template` 读取模板；禁止在 `.py` 中硬编码 LLM 提示词。
- 新增模板按功能域放到对应前缀下；如无匹配前缀，先补充本表再落盘。
- 修改模板后运行 `python3 -m pytest tests/test_prompt_templates.py -q` 确认渲染与 denylist 通过。

## 功能域文件清单

按前缀分组列出当前全部模板，新增模板应放入对应分组：

### backend_（主 prompt 组装）
- backend_action_contract, backend_agent_command, backend_agent_context, backend_agent_metadata, backend_attachment_output_guidance
- backend_chat_delta, backend_chat_full, backend_claude_public_updates, backend_commit_policy_full
- backend_compact_summary_context, backend_compact_summary_missing, backend_compact_summary_truncated_suffix
- backend_context_delta, backend_context_pack, backend_coordination_policy_full, backend_input_image_guidance
- backend_knowledge_enabled_empty, backend_prompt_prefix
- backend_recent_conversation_chain, backend_recent_conversation_chains, backend_recent_conversation_empty, backend_recent_conversation_line, backend_recent_supervision_conversation
- backend_recovery_agent_context, backend_recovery_context, backend_recovery_sub_agent_notice, backend_request_policy
- backend_result_conversation_omitted, backend_sticky_context
- backend_task_context, backend_task_context_minimal, backend_task_context_missing, backend_task_context_none, backend_truncated_budget_suffix, backend_truncated_message_suffix

### chat_（回合交互与重试）
- chat_action_retry_schema, chat_action_schema_retry, chat_commit_policy_retry
- chat_feishu_group_action_retry_schema, chat_service_action_retry_schema, chat_task_update_required_retry

### knowledge_ / navigation_（知识库与导航）
- knowledge_capture_image_manifest, knowledge_capture_prompt, knowledge_command
- knowledge_distill_generate_rules, knowledge_distill_organize_rules
- knowledge_navigation_bootstrap, knowledge_navigation_header, knowledge_navigation_rule
- navigation_command

### service_assistant_ / feishu_group_（飞书管家与群聊数字人）
- service_assistant_action_contract, service_assistant_identity, service_assistant_runtime
- feishu_group_digital_human_action_contract, feishu_group_digital_human_coalesced
- feishu_group_digital_human_identity, feishu_group_digital_human_message, feishu_group_digital_human_permission

### supervision_（监督 host）
- supervision_exchange, supervision_host_context, supervision_host_contract, supervision_host_delta_context

### finalization / task_ / subtask / runner_（收尾与子代理）
- finalization, finalization_knowledge_feedback_disabled, finalization_knowledge_feedback_enabled, finalization_source_context
- finalization_task_journal, finalization_task_journal_empty, finalization_task_journal_field, finalization_task_journal_item
- task_assignment, task_round_summary, task_skills_context
- subtask, subtask_mutability_implementation, subtask_mutability_research
- runner_claude, runner_codex

### workflow_guidance_（工作流模式）
- workflow_guidance_auto, workflow_guidance_bugfix, workflow_guidance_embedded-driver
- workflow_guidance_fault-debug, workflow_guidance_feature, workflow_guidance_hil-regression
- workflow_guidance_release, workflow_guidance_review

### 其他
- mode_instruction_default, mode_instruction_final, mode_instruction_memo_report, commit_policy, compact_summary, action_invalid_schema
- hardware_debug_context, browser_control_context, memo_completion_report
