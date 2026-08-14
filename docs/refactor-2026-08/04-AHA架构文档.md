# AHA 架构文档

> 版本：v0.1.103（2026-08-14）
> 定位：AHA 的权威架构文档 —— 描述「它是什么、现在长什么样、哪里在痛、要长成什么样、怎么演进」。
> 它取代零散的边想边写的认知，作为后续所有架构决策的输入。
> 关联文档：`01-调研基线.md`（事实）、`02-架构重构方案.md`（缺陷与分层方案）、`03-实施计划.md`（执行清单）。

---

## 0. 本质：AHA 是什么

> 官方定位：**本地优先的 AI agent 工作台**，用 `Run → Task → Agent` 组织工作，
> 让 Codex、Claude 等 agent 在相互隔离的任务中执行和协作，并通过 Web UI 统一管理
> 对话、上下文、共享浏览器、本机终端与硬件调试。不提供模型，调用本机已登录的 agent CLI。

把它放到更大的坐标里，AHA 同时是四种东西：

| 面向谁 | 它是什么 | 说明 |
|---|---|---|
| 用户 | **多 agent 任务管理器** | Run/Task/Agent 三级组织，并行、监督、收报告 |
| agent | **后台运行时（runtime）** | 每 task 一个 watcher 进程，监听消息、组装 prompt、调 CLI、推进 offset |
| 世界 | **事件驱动的消息总线** | inbox/events.jsonl 是神经；所有通道写同一套流，按 task/target 消费 |
| 自己 | **递归的自我维护系统** | 用 AHA 开发 AHA：KB/worklog/navigation 给自己建记忆与路由 |

**一句话**：AHA 是「为 AI agent 而生的自托管运行时」—— 一半任务管理器，一半消息总线，
一半进程监督器，还有一半是它自己的开发工具。它当前最大的未完成项，是把**「单任务的脚本」
长成「多任务、可恢复、跨平台的 OS 骨架」**。

---

## 1. 运行形态（进程模型）

```
┌──────────────────────────────────────────────────────────────────┐
│                       AHA Web 服务（Windows pythonw）               │
│  HTTP API · Web UI · 消息路由 · slash 命令 · 配置 · KB 路由         │
└───────────────┬──────────────────────────────────────────────────┘
                │ spawn / 管理
        ┌───────┴───────────────┐
        │ 后端 agent（每 task 一个）│
        │ claude-chat / codex-chat │
        │ <run_id> <target> --task-id│
        └───────┬───────────────┘
                │ 调外部 agent CLI（黑盒）
        ┌───────┴───────────────┐
        │  Codex CLI（Node.js）   │   ← /home/.../bin/codex
        │  Claude Code（Rust ELF）│   ← ~/.local/bin/claude
        └────────────────────────┘
```

**关键特征**：
- 每个 task 一个独立后端进程；所有 task **共享 run 级 inbox 和 events.jsonl**。
- **WSL 模式**（workspace 是 UNC/native WSL 路径时）：`wsl.exe` 是 Windows 宿主进程，
  distro 内 python + 真实 CLI 是**工作进程**。宿主与工作进程不在同一进程树 → 进程管理复杂。
- **AHA 本体 = Python**（onebin zipapp，`/usr/bin/env python3` shebang）。Codex CLI 是 Node.js，
  Claude Code 是 Rust 原生二进制 —— 都是 AHA 的黑盒子，通过 `--codex-bin`/`--claude-bin` 调用。

---

## 2. 数据模型（存储布局）

### 2.1 AHA home 目录

```
<AHA home>/                         # 默认 ~/.aha，Windows 侧单副本
├── config.json                     # 全局配置（backend/workspace_roots/providers/configured_models/knowledge/codex/claude）
├── runs/<run_id>/
│   ├── plan.json                   # run 计划 = task 权威副本
│   ├── events.jsonl                # 全 run 事件流（27+ 模块写入）
│   ├── inbox/{target}.jsonl        # 消息队列，按 target 隔离（main/browser/aha/sub-N）
│   ├── chat/{agent}-{hash}.md      # 每次回复的 markdown
│   ├── sessions/{agent}.json       # run 级 session
│   ├── tasks/{task_id}/
│   │   ├── task.json               # task 副本（与 plan.json 同构，冗余）
│   │   ├── messages.jsonl          # task 专属消息记录
│   │   ├── prompts/                # 每次 prompt 快照
│   │   ├── sessions/{agent}.json   # task 级 session
│   │   ├── rounds/  artifacts/
│   └── runtime/
│       ├── backend-{task}-{agent}.json    # 后端进程状态（pid/command/status/wsl_*）
│       ├── chat-offset-{task}-{agent}.json # 消息消费游标（字节偏移）
│       └── chat-consumer-{task}-{agent}.lock
├── knowledge/                      # KB（git 仓库），projects/<key>/{navigation,worklog,solutions}
├── runtime/  wsl-backends.json     # AHA home 级进程状态 / WSL 探测缓存
├── browser/ feishu/ hardware/ weixin/    # 各通道状态
```

### 2.2 核心 schema（简表）

| 对象 | 位置 | 关键字段 |
|---|---|---|
| config | `config.json` | providers[]/configured_models[]/codex+claude.env[]/knowledge.path/workspace_roots |
| plan | `runs/<id>/plan.json` | goal/tasks[]；task: id/workspace_path/preferred_*/agents[]/status |
| task | `tasks/<id>/task.json` | 与 plan 中 task 同构（冗余副本） |
| backend state | `runtime/backend-*.json` | status/pid/command/model/started_at/stopped_at/wsl_distro/wsl_native_home |
| session | `tasks/<id>/sessions/<agent>.json` | backend_session_id/history_backend_sessions/compact_summary/workspace_path/model |
| chat-offset | `runtime/chat-offset-*.json` | offset（字节游标指向 inbox）/updated_at |
| 消息 | `inbox/{target}.jsonl` + `tasks/<id>/messages.jsonl` | ts/run_id/target/task_id/role/sender/message |
| 事件 | `runs/<id>/events.jsonl` | ts/run_id/type/data（agent_started/message/backend_*/agent_interrupted/...） |

### 2.3 平台双视角（路径模型）

AHA 路径存在**两种视角**，是几乎所有 WSL bug 的土壤：

```
存储视角（AHA 平台视角，通常是 Windows）：
    ~/.aha/knowledge
    D:\tmp\APK
    \\wsl.localhost\Ubuntu-24.04\home\kaikai\kk-workspace\my_project\AHA

消费视角（运行平台原生视角）：
    Windows 宿主 → C:\Users\toope\.aha\knowledge / D:\tmp\APK / \\wsl.localhost\...
    WSL 后端    → /mnt/c/Users/toope/.aha/knowledge / /mnt/d/tmp/APK / /home/kaikai/kk-workspace/my_project/AHA
```

`ws_target.host_native_path(path, aha_home)` 负责在消费点把存储视角转成本机原生视角：
`~` 展开到 AHA home 父目录、Windows 盘符 → `/mnt/<drive>/...`、UNC → `/...`、native 原样透传。

---

## 3. 数据流（消息 → prompt → 结果）

```
用户消息 → handle_send_payload → append_message → inbox/{target}.jsonl（task_id 在 payload）
                                 （双写 tasks/<id>/messages.jsonl）
    ↓
后端 watcher（每 task 一个进程）:
    load_chat_offset → iter_jsonl_records_from(inbox, offset)
    → worker_task_id 过滤 → next_task_message_batch（逐组合并）
    → 组装 prompt（注入 KB/nav/skills/recovery_context/context_pressure）
    → 调 codex/claude CLI（黑盒）→ 写 events + chat/*.md + prompts/*
    → save_chat_offset(推进)   ← 中断时推进到 inbox 末尾（interrupt 语义）
    ↓
interrupt: stop_backend + save_chat_offset(→inbox末尾) + set_agent_status(interrupted)
           + update_agent_runtime(recovery_context=...)
recover:   recover_stale_running_agent（backend 已停时）→ 同样推进 offset + 标 interrupted
```

---

## 4. 核心抽象与设计原则

| 抽象 | 载体 | 责任 |
|---|---|---|
| Run / Task / Agent | plan.json + task.json | 工作组织三级 |
| inbox | `inbox/{target}.jsonl` | 消息投递队列（按 target 隔离） |
| events | `events.jsonl` | 全 run 事件流（source of truth 候选） |
| chat-offset | `runtime/chat-offset-*.json` | 字节级消费游标 |
| backend state | `runtime/backend-*.json` | 进程状态（含 wsl_* 上下文） |
| 路径抽象 | `store/ws_target.py` `host_native_path` | 平台视角 ↔ 原生视角转换 |
| 存储 IO | `store/io.py` | append_jsonl（sidecar 锁）/iter_jsonl/read/write |
| 通道 | browser/feishu/weixin/hardware/observe_proxy | 外部世界接入 |

**设计原则（沿用并强化）**：
1. **单副本**：AHA home 数据只在 Windows 侧一份；WSL 只跑进程不改数据位置。
2. **渐进重构**：每层独立提交、独立验证、可回滚，不做一次性大爆炸。
3. **agent CLI 是黑盒**：AHA 只管进程与上下文，不内嵌模型能力。
4. **事件驱动**：一切状态变化落到 events.jsonl，可回放、可诊断。

---

## 5. 结构缺陷（as-is 的痛，按根因归四类）

> 详见 `01-调研基线.md` §5 与 `02-架构重构方案.md` §1。这里只保留结论。

| # | 缺陷 | 根因 | 表象 |
|---|---|---|---|
| 1 | **inbox 不按 task 隔离** | `inbox_path` 只有 target 维度 | 并发 main task 共享 inbox，offset 互相跨越 → 串台 |
| 2 | **task 数据冗余无一致边界** | plan.json / task.json / messages.jsonl 多份 | 同步散落，可能不一致 |
| 3 | **路径无统一抽象** | 配置存 Windows 视角，WSL 消费靠零散补丁 | `~`/盘符/UNC 在 WSL 解析错 → KB 分裂、workspace 失效 |
| 4 | **进程生命周期管理分散** | interrupt/recover/offset 散在 3 个文件；宿主/工作进程未区分 | 孤儿进程、offset 不推进、recover 误判、interrupt 崩溃 |

> 这四类都是**操作系统层**的缺陷，不是业务 bug —— 它们证明 AHA 正从
> 「单用户、单任务、单平台」的假设向「多任务、多平台、可恢复」演进，
> 而这个演进需要的就是 OS 该有的地基。

---

## 6. 目标架构（to-be）：AI agent 的运行时

### 6.1 目标形态一句话

**AHA 最终应是一个「自托管的 agent 运行时」**：像 OS 管理进程那样管理 agent，
像消息总线那样路由消息，像事件溯源那样保证可恢复。

### 6.2 目标分层

```
┌─────────────────────────────────────────────────────────┐
│  业务层（快速迭代，允许临时手段）                          │
│  agent 编排 · provider/模型探测 · KB/检索 · 通道 bridge   │
├─────────────────────────────────────────────────────────┤
│  平台层（收敛成少数稳定模块，接口固定，最硬）               │
│  进程生命周期 · 任务隔离 · 事件溯源 · 路径抽象 · 并发原语   │
├─────────────────────────────────────────────────────────┤
│  运行时（Web 服务 + 每 task watcher + bridges）           │
└─────────────────────────────────────────────────────────┘
```

**目标**：改业务不牵动地基；改地基不怕碰坏业务。

### 6.3 五级演进路线图（按优先级）

| 级 | 目标 | 现状 | 验收标准 |
|---|---|---|---|
| **L0 进程生命周期抽象** | interrupt/recover/stop/start/offset 原子化 | 阶段 3 未做 | 无孤儿进程；任何时刻 interrupt 干净收尾；崩溃后 recover 到一致状态 |
| **L1 task 级隔离** | inbox/offset/事件流按 run/task/agent 三级命名 | 已决策用共享 inbox+过滤 | 两 task 并发 main 不串台 |
| **L2 事件溯源** | events.jsonl 成为唯一权威，plan/task/offset 由其投影 | 多份冗余+同步 | 重放事件恢复任意一致状态，无需"修 plan.json" |
| **L3 自愈与监督** | 心跳/健康检查/监督树/看门狗 | 靠用户发现异常 | 卡死 backend 自动识别重启，子进程按策略重启 |
| **L4 分层固化** | 平台层/业务层边界固定 | 混在一层 | 平台层接口稳定，业务层自由迭代 |

---

## 7. 关键技术决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| 语言底座（现状） | Python（onebin zipapp） | 业务迭代快、LLM 生态；代价是平台工程暗礁（锁/进程/分发） |
| 若重写（评估） | Go 或 BEAM 做平台层；业务层留 Python/JS | 进程监督/并发/单文件是 Go/BEAM 强项；生态红利不能丢 |
| 换语言 vs 重构 | **优先重构抽象层** | 缺陷是架构假设错，不是语言不够强 |
| inbox 隔离（附录 B） | 保留共享 inbox + offset 修复 | 串台根因是 offset 语义，不是物理隔离；隔离迁移风险高 |
| 路径抽象（阶段 1） | `host_native_path` 统一转换 | 根治 `~`/盘符/UNC 在 WSL 的解析错 |
| 部署 | 单文件 onebin → `AppData/Local/AHA/aha` | 静态产物，BUILD_VERSION 内嵌，不受 git 历史重写影响 |

---

## 8. 验证与演进纪律

1. 每层独立提交、单独回滚；涉及运行环境改动先用临时 task 验证。
2. 每层完成跑聚焦测试 + 关键回归点（WSL 启动、并发写锁、context window、interrupt、串台）。
3. 文档随进度更新；架构文档是本系列的唯一权威，01/02/03 是其支撑。

---

## 附录 A：代码地图（关键模块）

| 概念 | 文件 | 说明 |
|---|---|---|
| 路径 | `store/paths.py` | aha_home/inbox/event/session 路径 |
| 路径转换 | `store/ws_target.py` | host_native_path/UNC/native |
| 存储 IO | `store/io.py` | append_jsonl（sidecar 锁）/iter_jsonl/read/write |
| 事件 | `store/events.py` | append_event/event_stream |
| 消息 | `store/filesystem.py` | append_message（双写 inbox+messages）|
| 消费 | `services/chat.py` | agent_chat 主循环 |
| 合并 | `services/chat_coalescing.py` | next_task_message_batch |
| offset | `services/chat_offsets.py` | chat_offset_path/load/save |
| 进程 | `services/backend_runtime.py` | start/stop/status backend |
| interrupt | `web/task_command_actions.py` | interrupt_selected_agent |
| recover | `web/status.py` | recover_stale_running_agent |
| WSL 探测 | `services/wsl_backend.py` | distro/python3/claude/codex 探测 |

## 附录 B：本次演进已落地（v0.1.95 → v0.1.103）

| 提交（合并后） | 内容 |
|---|---|
| `0fce8c4 feat(backend): run backends inside WSL` | WSL backend 运行、加固、路径、进程清理、UnboundLocalError 修复 |
| `5ddf85e fix(wsl): resolve config paths native view` | host_native_path 统一（WSL 消费方向） |
| `6d1eb19 fix(settings): preselect provider` | Configured Model 编辑回显 |
| `44a7d62 refactor(backend): path abstraction + offset` | 重构文档 + 路径收敛 + recover 推进 offset + interrupt recovery context |
| 远端 `6782c67` + `88a5cc1`（v0.1.104） | plan.json 加固 + Windows 宿主保持 Windows 视角 |

**升级兼容性结论**（v0.1.95 → v0.1.103，尤其 WSL 路径任务）：
- 数据格式向后兼容：新增字段均可选，旧数据 `None` 安全。
- 路径解析是**修复**不是破坏：UNC→native、盘符→/mnt，升级后才读对。
- 进程判定看 workspace 不看 state：WSL 任务下次启动自动进 distro。
- 跨平台 session 有 `_compact_unresumable_session` 兜底：旧 Windows session 归档重建摘要。
- 唯一注意：升级重启会中断在跑任务；源码方式运行（非 onebin）退回 Windows backend。
