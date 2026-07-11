# AHA 知识库沉淀系统 — 架构与实现计划

> 状态: **全部 6 个 Phase 完成**（Phase 1 `5d0866b` / Phase 2 `5ca3e31` / Phase 3 `3f26a66` / Phase 4 CLI `4400712` / Phase 4b `4249b4b` / Phase 5 `2bb1bae` 已提交；Phase 6 最后 checkpoint 待提交）。learn→do→distill + remote 同步 + 审核/浏览/通用知识(中英检索)/过期复核全闭环。启用与验证见第 11 节。运行期遗留：A/B 行为观察（需真实 task 样本）。
> 维护方式: 本文档是「活文档」。实现过程中每完成一个步骤，更新对应复选框与「进度日志」。

> **决策确认（2026-06-19，用户拍板按文档推荐值）**：D6 curation gate 默认 `manual`；
> D7 第一刀做项目解决方案库；布局/字段按第 2、3 节；当前产品策略收口为用户在 KB 设置页手动同步，git 自动项默认关闭。

> **实现决策（frontmatter 格式）**：AHA 声明零第三方依赖（`pyproject.toml` `dependencies=[]`），
> 不能依赖 PyYAML，故条目 frontmatter 采用 **JSON 块**而非 YAML（用 `---` 围栏，块内为 JSON）。
> 第 3 节示例的字段语义不变，仅序列化格式由 YAML 改为 JSON。

---

## 0. 一句话目标

让 agent 在做任务的过程中，**先从知识库学习、做完再把经验沉淀回知识库**；
知识库独立于 task/run 持久化，本身是一个由 AHA 管理的 git 仓库，可配置远端自动同步。

---

## 1. 设计原则与已确认决策

| # | 决策 | 状态 |
|---|---|---|
| D1 | 知识库**独立于 task/run**，挂在 AHA home 层 | ✅ 已确认（task/run 可删除，知识需迁移） |
| D2 | 知识库目录**本身是一个 git 仓库**，由 AHA 负责 init/commit/push/pull | ✅ 已确认 |
| D3 | settings 中提供 **git remote 配置**，支持自动同步 | ✅ 已确认 |
| D4 | 沉淀是**提炼**而非原样堆叠 final/report | ✅ 已确认 |
| D5 | Wiki 定位为**非项目相关的通用教程/技术文档**；项目知识主要进入 navigation，少量可复用排障进入 solutions | ✅ 已确认 |
| D6 | 入库需经过 **curation gate**（早期默认人工确认） | 🔲 待你拍板（建议默认开） |
| D7 | 第一刀优先做**项目解决方案库**（ROI 最高，贴近现有 final） | 🔲 待你拍板 |
| D8 | Project navigation 是随任务逐步完善的项目路线图：`index.md` 只做入口路由，模块/流程文档按需读、按需更 | ✅ 已确认 |

---

## 2. 存储布局

知识库根目录: `~/.aha/knowledge/`（可通过 config 覆盖路径）。**整个目录是一个 git 仓库。**

```
~/.aha/knowledge/                 # git 仓库根（AHA 管理）
├── .git/
├── aha-knowledge.json            # KB 元信息（schema 版本、project 索引、统计）
├── README.md                     # 人类可读入口（自动生成目录）
├── general/                      # 通用知识（跨项目）
│   ├── wiki/
│   │   └── <slug>.md
│   └── solutions/
│       └── <slug>.md
└── projects/
    └── <project-key>/            # 项目分区
        ├── project.json          # 项目元信息（名称、workspace 指纹、别名）
        ├── navigation/
        │   ├── index.md          # 小入口：项目介绍 + 模块/流程文档链接
        │   ├── modules/
        │   │   └── <module>.md   # 按任务命中阅读/更新的模块文档
        │   └── flows/
        │       └── <flow>.md     # 按任务命中阅读/更新的关键流程文档
        └── solutions/
            └── <slug>.md
```

- **不混入 task/run 目录**，独立生命周期。
- 删除某个 run 不影响知识库；知识库可整体拷贝/clone 到另一台机器。
- `navigation/index.md` 只承担项目定位和路由索引职责；普通任务不得把全项目结构全量写回 index。
- `navigation/modules/*.md` / `navigation/flows/*.md` 随真实任务逐步完善；一次任务只更新本次改动影响的少量文档。全量扫描/重建只用于初始化或用户显式维护。
- 每个 navigation 文档只负责一层入口：`index.md` 只列第一层模块/流程；模块/流程文档只列自己的直接子文档。新增子文档如果缺父入口，AHA 只补直接父入口，不把孙子节点展开到顶层。
- Root `navigation/index.md` 首次缺失时优先由 workspace scan 生成完整 bootstrap index；已有 index 后只做最小增量补链接。
- Web Entries 主列表只展示项目导航入口；模块/流程文档作为入口的可点击引用在弹窗中查看/编辑，不铺平成一堆平级知识卡片，也不撑乱主列表。

### 2.1 project-key 推导

需要一个**稳定**且**可迁移**的项目标识，不能用绝对路径（换机器就失效）。

策略（按优先级）：
1. 若 workspace 是 git 仓库 → 用 repo 名 + `remote origin` URL 规范化后的 hash，例如 `aha-git-<hash>`（跨机器一致，同时可读）。
2. 否则 → 用 run goal + workspace 目录名生成 slug。
3. `project.json` 记录所有曾见过的 workspace 路径作为「别名」，便于人工核对与合并。

> 边界：同一项目在不同机器/路径下应映射到同一 project-key。git remote 优先正是为此。
> 兼容：旧版本写入的 `git-<hash>` 目录作为 legacy alias 继续参与检索，不强制迁移。

---

## 3. 数据模型

每条知识是一个带 frontmatter 的 Markdown 文件。两类 schema：

### 3.1 Wiki 条目（通用教程/技术文档：「X 是什么 / 为什么」）

```markdown
---
id: kb_<ulid>
type: wiki
scope: general | personal
project_key: null
title: Git rebase 教程
tags: [hardware, serial, session]
confidence: 0.0~1.0
source_tasks: [run_id/task_id/round_id, ...]
related_files: [src/aha_cli/services/hardware_session.py]
created_at: <iso>
updated_at: <iso>
review_after: <iso|null>     # 过期复核提示
status: active | stale | deprecated
---
```

正文（结构化 Markdown）。Wiki 不承载项目结构知识；项目内模块职责、入口、约束应写入 `projects/<project-key>/navigation/`。Wiki 同主题应**持续更新同一篇**，而非新增。

建议正文结构：

```markdown
## 结论
## 适用范围
## 规则 / 约定
## 示例
## 相关位置
## 更新条件
```

### 3.2 解决方案条目（过程性：「遇到 Y 怎么办」，案例库/CBR）

```markdown
---
id: kb_<ulid>
type: solution
scope: project | general
project_key: <key|null>
title: zipapp 打包后启动报 ModuleNotFound 的排查
tags: [build, zipapp, packaging]
outcome: success | partial | failed   # 失败案例同样有价值
confidence: 0.0~1.0
source_tasks: [...]
related_files: [...]
created_at: <iso>
updated_at: <iso>
review_after: <iso|null>
status: active | stale | deprecated
---

## 问题 / 触发条件
## 尝试过的方案（含无效的）
## 有效解法
## 验证方式
## 失效条件 / 适用边界
```

> 设计要点：`outcome` 与「无效方案」是解决方案库相对 wiki 的核心价值——避免后续 agent 重走死路。

sidecar 产出的 `solutions` 正文建议使用更偏行动的固定段落：

```markdown
## 适用场景
## 问题 / 触发信号
## 推荐做法
## 关键位置
## 验证方式
## 失效条件 / 适用边界
```

---

## 4. 配置 schema（新增）

在 `default_config()`（`src/aha_cli/domain/models.py`）新增 `knowledge` 块，
由 `load_config` 做默认值合并（与现有 `proxy`/`retention_policy` 同模式）：

```python
"knowledge": {
    "enabled": False,            # 总开关
    "path": None,               # None -> ~/.aha/knowledge
    "git": {
        "enabled": False,
        "remote": None,         # e.g. git@github.com:user/aha-kb.git
        "branch": "main",
        "auto_commit": False,   # 默认不自动 commit，用户在 KB 设置页手动同步
        "auto_push": False,     # 自动 push 到 remote
        "auto_pull": False,     # 默认不自动 pull，用户在 KB 设置页手动同步
        "author_name": "AHA",
        "author_email": "aha@local",
    },
    "curation": {
        "gate": "manual",       # manual | auto | off
    },
    "retrieval": {
        "max_entries": 5,       # 注入 prompt 的条目上限
        "max_chars": 4000,
    },
}
```

---

## 5. 核心生命周期（learn → do → distill）

```
task 开始
  │  (a) git pull（若 auto_pull）
  │  (b) 按 project-key + tags 检索 KB → navigation/index 置顶，模块/流程文档只在任务命中时读取
  │  (c) 注入到 task prompt 上下文（"项目已知经验"段落）
  ▼
agent 执行任务  ……
  ▼
task 收尾 / round finalize 或 memo completion report 完成
  │  (d) final/report agent 同次输出用户可见报告 + knowledge candidates sidecar
  │  (e) AHA 剥离 sidecar 保存干净 final/report；无 sidecar 时用 heuristic 兜底提炼 0~N 条候选
  │  (f) curation gate：
  │        manual → 进入待确认队列，用户在 Web UI 审核
  │        auto   → 直接入库（带较低 confidence）
  │  (g) 写入 KB 文件 + 更新索引
  │  (h) git commit（若 auto_commit）→ 可选 git push
  ▼
完成
```

挂载点（复用现有 hook，不另起炉灶）：
- (a)(b)(c) 挂在 task 启动 / prompt 组装链路（`prompt_artifacts` / `chat_prompt_context`）。
- task final 的 (d)~(h) 挂在 `store/finals.py::write_task_result` 的 `finalize` 分支之后。
- memo report 的 (d)~(h) 挂在 `services/chat.py::write_memo_report_result` 成功写回之后，source 标记为 `memo_report`。
- final/report 属于同一 linked task 时共用同一 `source_group`，同标题候选更新同一条 `.pending`，避免执行顺序不同或重复执行产生重复知识。
- sidecar 使用软数量约束：默认产出 0~3 条高质量候选；只有确实存在更多独立可复用经验时才超过 3 条。
- sidecar 的 `body` 模板由 `kind` 决定：`solutions` 偏可复用行动指南，`wiki` 仅用于通用教程/技术文档，`navigation` 用于项目入口、模块文档和流程文档。普通一次性 bug fix 默认不入库；若暴露了模块职责/入口/约束，应只更新受影响的 navigation 文档，不能把普通任务变成全量 nav 重建。

---

## 6. 模块划分（拟新增）

| 模块 | 职责 |
|---|---|
| `store/knowledge.py` | KB 读写、索引、project-key、slug、文件 I/O |
| `services/knowledge_git.py` | git init/commit/push/pull 封装（幂等、错误隔离） |
| `services/knowledge_distill.py` | 处理 final/report sidecar 候选；缺失时用 heuristic 兜底提炼 |
| `services/knowledge_retrieval.py` | 检索 + 摘要 + prompt 注入 |
| `services/knowledge_curation.py` | 待确认队列、批准/拒绝/合并 |
| `websocket/server.py` 路由 + Web UI | KB 浏览、审核、配置 |
| `cli.py` / `cli_parser.py` | `aha kb ...` 子命令 |

> 与现有 `task` 隔离不冲突：task 执行时只做**只读检索**与**写入提案**，KB 的真正写入与 git 操作集中在 KB 服务层，串行化避免并发冲突。

---

## 7. 关键风险与对策

| 风险 | 对策 |
|---|---|
| 提炼幻觉/过度具体污染后续任务 | curation gate + final/report sidecar 结构约束 + confidence + source 可追溯 |
| 知识过期（项目演进） | `review_after` + `related_files` 关联，检索时标注「可能过时」，定期复核 |
| 检索注入过多/跑题 | `max_entries`/`max_chars` 上限，先朴素检索（key+tag+标题）不上向量库 |
| git 冲突 / push 失败 | 操作幂等、失败不阻断任务、冲突走 rebase 并在 UI 报警 |
| 并发写入 KB | KB 写入与 git 操作串行化（文件锁，复用现有 `locked_plan` 模式） |
| 远端泄露敏感信息 | 沉淀前过滤密钥/路径；remote 由用户显式配置且默认 auto_push=off |

---

## 8. 分阶段实现计划（带进度）

> 每阶段可独立交付与验证。状态: 🔲 未开始 / 🚧 进行中 / ✅ 完成。

### Phase 0 — 设计定稿（本文档）✅
- [x] 输出架构与实现计划文档
- [x] 用户确认 D6（curation gate 默认值）→ manual
- [x] 用户确认 D7（第一刀范围）→ 项目解决方案库
- [x] 用户确认存储布局与数据模型（第 2、3 节）

### Phase 1 — 存储与配置地基 ✅
- [x] 新增 `knowledge` 配置块（`default_knowledge_config`）+ 深合并（`_merge_knowledge_config`）+ 单测
- [x] `store/knowledge.py`：目录初始化、project-key 推导、frontmatter 编解码、条目读写、索引、status
- [x] `aha kb init` / `aha kb status` CLI（含 `--json`）
- [x] 单测：config 深合并、project-key 稳定性、slug、frontmatter 往返、读写/列举、status、CLI 端到端（共 12 例全绿）
- [x] 回归：`test_cli_core` + `test_store_state` 53 例全绿

### Phase 2 — Git 管理 ✅
- [x] `services/knowledge_git.py`：`ensure_repo`/`commit_all`/`pull`/`push`/`sync` 幂等封装
- [x] settings 中 git remote 配置项打通（`ensure_repo` 读取 `knowledge.git.remote` 自动 add/set-url；`aha kb sync` 端到端可用）
- [x] 沉淀后 auto_commit、task 前 auto_pull、可选 auto_push（config-gated 钩子 `auto_commit_after_change` / `auto_pull_before_task`）
- [x] 失败隔离（所有公开调用返回结果 dict，不抛异常打断任务）+ 冲突处理（pull rebase 冲突自动 abort 不留半成品）+ 单测（本地裸仓模拟 remote，14 例）
- [x] **同步顺序边界**：`sync` 改为 commit → pull(rebase) → push，本地 dirty tree 先安全提交再 rebase，避免脏树导致 rebase 失败；pull 失败（冲突/不可达）时不再 push，杜绝半同步/分叉历史
- [x] **远端状态区分**：用 `ls-remote` 把「remote 不可达」（返回失败，CLI 显示 FAILED）与「空远端尚无分支」（跳过，ok）分开，避免不可达时误显示 sync ok
- [x] 顺带修复 Phase 1 潜伏 bug：`kb` 漏加入 `cli_parser.COMMANDS`，导致 `aha kb sync` 被误判为无命令而追加 `ui`

> 说明：auto 钩子已就绪但尚未接入真实 task 生命周期（接入点在 Phase 3 的 `write_task_result` 后置与 Phase 5 的 task 启动前）。目前可用 `aha kb sync` 手动驱动。

### Phase 3 — 沉淀（distill，第一刀：项目解决方案库）✅
- [x] `services/knowledge_distill.py`：优先接收 final/report sidecar 生成项目解决方案候选；无 sidecar 时从 final + final_context(summary/changed_files/verification/risks) 用零依赖确定性 heuristic 兜底。
- [x] final/report agent 可同次输出 `<aha_knowledge_candidates>` sidecar；AHA 保存 final/report 前剥离 sidecar，候选进入同一套 `.pending` 审核流程。无 sidecar 或解析失败时才走 heuristic fallback。
- [x] 同一 linked task 的 final/report 使用同一 `source_group`；候选身份为 `scope + kind + project_key + normalized_title + source_group`，重复执行或顺序不同会更新同一 pending。
- [x] heuristic **实际利用 final/report 正文**：保留 Markdown 结构，优先抽取完成内容/稳定结果/关键结论/可复用经验等高价值章节作为「有效解法」；不再把受管正文硬截成 600 字前缀。
- [x] distill 时附带本项目命中的既有知识摘要；若新结论与旧知识冲突，候选提示审核时更新或废弃旧条目，避免过期知识静默误导。
- [x] 接入 `write_task_result` finalize 后置 hook（`_distill_knowledge_safe`，惰性导入避免 store→services 循环，**完全失败隔离，绝不打断 finalize**）
- [x] 接入 `write_memo_report_result` memo report 成功写回后置 hook（source=`memo_report`，带 memo_id/task_id，可追溯）。
- [x] **写入前确保 skeleton 已初始化**：`distill_and_enqueue` 在写候选/条目前调用 `init_knowledge_base`，即使首次由 finalize 触发也会生成 `aha-knowledge.json`/README/`.gitignore`，保证 `.pending/` 从一开始就被排除
- [x] curation 待确认队列落盘：`.pending/` 队列（`.gitignore` 排除，未审候选永不被 commit/push）；manual→入队、auto→直写并 auto_commit、off→跳过
- [x] `approve_candidate` 将候选提升为受管条目并出队（approve/reject 完整 CLI 在 Phase 4）
- [x] `aha kb pending` 列出待审候选；`aha kb status` 增加 pending 计数
- [x] 验证：真实 finalize 集成测试（init→plan→write_task_result finalize→产出 pending 候选）+「不提前 init 也会初始化并生成 pending」+ final 正文利用测试；单测共 12 例；全量 628 测试绿

### Phase 4 — 审核与浏览（CLI ✅ / Web UI 待做）
- [x] `aha kb list/show/approve/reject/search` CLI（list 支持 --scope/--project/--kind 过滤；show 按 id 或 slug；search over 标题/标签/正文）
- [x] approve 走 `approve_candidate`（提升候选→受管条目并出队）+ git enabled 时自动 commit；reject 丢弃候选
- [x] 合并/去重：approve 按**目标 scope+kind+project+slug** 精确判断 created/updated（跨 scope/project 同名不再误报 updated）；同一身份覆盖即合并；新增 `iter_all_entries`/`find_entry`/`search_entries` 存储查询
- [x] **受管条目稳定 `kb_` id**：`write_entry` 按身份(scope/kind/project/slug)生成确定性 id，重写同条目保留旧 id；`show` 支持按 id 或 slug 查
- [x] `kb reject --json` 输出 JSON（与其它子命令一致）
- [x] 单测：list/show(by slug & id)/search/approve/reject(+json)/跨 scope 同 slug created/dedup updated/id 稳定 共 7 例（test_knowledge_cli.py + test_knowledge.py）；全量 635 测试绿
- [ ] 更智能的同主题合并（跨 slug 的语义合并，非仅同 slug 覆盖）— 待 Phase 6 wiki 合并策略一并处理

### Phase 4b — Web UI ✅（待提交 review）
- [x] KB HTTP API（`web/knowledge_routes.py`，root-scoped，沿用 system_routes 约定，接到 server.py 分发）：
      `GET /api/kb/status|entries|entry|pending|config`、`POST /api/kb/approve|reject`、`PATCH /api/kb/config`
- [x] `PATCH /api/kb/config` 允许列表合并 enabled/path/git{enabled,remote,branch,auto_pull,auto_commit,auto_push}/curation.gate，写回 config.json 且保留其余键；非法 gate 返回 400
- [x] approve 经 API 复用同身份去重(created/updated) + git enabled 时 auto_commit
- [x] **自包含前端控制台 `web/static/knowledge.html`**：条目浏览(scope/kind/project 模糊过滤 + 标题/标签/正文搜索 + 查看正文)、正式条目 edit/deprecate/mark stale/restore/delete/copy id/path、待审 approve/reject、knowledge 设置表单；不侵入主 SPA(降低风险)，经 `/static/knowledge.html` 访问
- [x] PATCH config 对手写坏配置加固：`knowledge.git`/`knowledge.curation` 非 dict 时按空 dict 合并（不再 500），加测试
- [x] 单测：API 6 例 + **server 分发自动化测试**（`fetch_ui_response` 覆盖 `/api/kb/status`、`/static/knowledge.html`、`PATCH /api/kb/config`，保护 server.py 改动）+ 坏配置容错；全量 645 测试绿
- [x] 设计决策已确认：自包含 `/static/knowledge.html` console，不嵌主 SPA

### Phase 5 — 学习（retrieval 注入）✅
- [x] `services/knowledge_retrieval.py`：项目优先检索（按 term 重叠打分，无命中回退按 recency）+ 摘要格式化（条目数/字符预算双约束）；零依赖，不上向量库
- [x] 接入 task prompt 组装链路：`task_assignment.md` 加 `$knowledge_context` 占位；`task_assignment_prompt` 增参；`dispatch_task_to_main` 经 `knowledge_context_for_task` 注入
- [x] **闭合 remote 同步「开工前 pull 再学」边**：`knowledge_context_for_task` 检索前调用 Phase 2 的 `auto_pull_before_task`，失败隔离（pull 失败回退本地 KB，不阻塞）
- [x] **project_key 与 distill 侧一致**：检索复用同一 `project_key`(git remote 优先 / 同 goal 回退)，确保学到的正是本项目沉淀的
- [x] **format_injection 硬字符预算**：首条过长也按剩余预算截断 excerpt，绝不超 `max_chars`
- [x] **完全失败隔离**：注入任何异常(含 require_plan 的 SystemExit)→ 返回空串，绝不打断 prompt 组装；默认 knowledge.enabled=False 时零影响
- [x] 单测：terms/检索打分+回退/硬预算截断/auto_pull 调用+失败容错/disabled-或无 workspace 为空/匹配命中/prompt 嵌入/**真实 dispatch 注入 main inbox** 共 9 例；全量 654 测试绿
- [ ] A/B 行为观察（注入知识后 agent 是否少踩坑）→ 记录为**运行期验证项**，需跑真实 task 累积样本，不阻塞提交

### Phase 6 — 通用知识 + Wiki 扩展 ✅（待提交 review）
- [x] 通用 scope 沉淀与检索：`aha kb add --scope general` 手动沉淀通用知识；retrieval 已覆盖 general（按 term 命中注入），加通用知识被检索的测试
- [x] Wiki 条目类型 + 同主题更新合并策略：`aha kb add --kind wiki` 创建/更新（同 slug=同主题→更新同一篇）；`--append` 追加带时间戳的新段落（合并而非覆盖），保留稳定 id / created_at
- [x] 过期复核（review_after）提醒：`aha kb add --review-days N` 设置 review_after；`list_stale_entries`（ISO 字典序=时序比较）+ `aha kb stale` CLI + `aha kb status` / KB API status 增加 `stale` 计数
- [x] 单测：通用知识检索 / add 创建-更新-追加 / project 缺 key 报错 / stale 列举 / CLI review-days+stale+status 共 5 例；全量 659 测试绿

---

## 9. 进度日志

| 日期 | 阶段 | 变更 | 备注 |
|---|---|---|---|
| 2026-06-19 | Phase 0 | 创建本文档（架构 + 分阶段计划） | 等待 D6/D7 及布局确认 |
| 2026-06-19 | Phase 0 | 用户拍板四项默认值，设计定稿 | manual / 项目解决方案库 / 第2、3节 / pull+commit开,push关 |
| 2026-06-19 | Phase 1 | 配置块 + `store/knowledge.py` + `aha kb init/status` + 12 单测；frontmatter 改用 JSON（零依赖） | 改动：constants.py, domain/models.py, store/config.py, store/paths.py, store/knowledge.py(新), cli_parser.py, cli.py, tests/test_knowledge.py(新)。全测试绿。 |
| 2026-06-19 | Phase 1 | 收口修复：project_key 无 git fallback 去除绝对路径 hash（改为对 goal+目录名 basis 取 hash，可迁移）；第 10 节改为「已确认决策 + 后续待定」消除与顶部/第 8 节冲突；新增可迁移性单测 | 13 单测全绿，已提交 Phase 1 checkpoint `5d0866b` |
| 2026-06-19 | Follow-up | project_key git 格式改为 `<repo-name>-git-<hash>`，检索兼容旧 `git-<hash>`；知识库 entries 改为原卡片内 View/Close 展开，避免重复标题 | 用户体验/可读性修复 |
| 2026-06-19 | Follow-up | memo completion report 成功生成后接入知识沉淀；distill 时附带本项目命中的既有知识摘要，候选中提示审核时更新/废弃冲突旧条目 | 完善生产入口与消费后的更新复核线 |
| 2026-06-20 | Follow-up | Web entries 支持 project-key 模糊过滤与标题/标签/正文搜索；heuristic 改为保留 Markdown 结构、抽取高价值章节，不再硬截断存储正文 | 修复知识库可查性与候选内容质量 |
| 2026-06-20 | Follow-up | final/report 提示词支持 knowledge sidecar；AHA 写入 final/report 前剥离 sidecar 并以 sidecar 优先生成候选；pending 按 source_group + normalized title 幂等合并 | 明确 final/report/KB 三层心智模型，解决执行顺序与重复执行问题 |
| 2026-06-19 | Phase 2 | `services/knowledge_git.py`（ensure/commit/pull/push/sync + auto 钩子）+ `aha kb sync` CLI + 10 单测（本地裸仓）；修复 COMMANDS 漏 `kb` 的潜伏 bug | 改动：services/knowledge_git.py(新), cli.py, cli_parser.py, tests/test_knowledge_git.py(新)。全量 612 测试绿。 |
| 2026-06-19 | Phase 2 | 边界收口：sync 改为 commit→pull→push（脏树先提交再 rebase，pull 失败不 push）；ls-remote 区分 remote 不可达(失败)/空远端(跳过)；+4 单测 | 全量 616 测试绿，已提交 Phase 2 checkpoint `5ca3e31` |
| 2026-06-19 | Phase 3 | distill 服务（heuristic + 可插拔）+ finalize 后置 hook（失败隔离）+ `.pending` 待审队列（gitignore）+ approve_candidate + `aha kb pending`/status pending 计数 + 9 单测（含真实 finalize 集成） | 改动：services/knowledge_distill.py(新), store/knowledge.py, store/finals.py, cli.py, cli_parser.py, tests/test_knowledge_distill.py(新)。全量 625 测试绿。 |
| 2026-06-19 | Phase 3 | 收口：distill 写入前 ensure skeleton（首次 finalize 也生成 .gitignore 排除 .pending）；heuristic 实际利用 final 正文（summary 缺失用截断摘要、存在则单列 final 摘录段）；+3 单测 | 全量 628 测试绿，已提交 Phase 3 checkpoint `3f26a66` |
| 2026-06-19 | Phase 4 | 审核/浏览 CLI：list/show/search/approve/reject + iter_all_entries/find_entry/search_entries + approve 去重(同 slug 覆盖,报告 created/updated)+ git auto_commit；+3 单测 | 改动：store/knowledge.py, cli.py, cli_parser.py, tests/test_knowledge_cli.py(新)。全量 631 测试绿。Web UI 拆到 Phase 4b。 |
| 2026-06-19 | Phase 4 | 收口：受管条目稳定 kb_ id(重写保留)+ show 按 id 查;approve created/updated 改按 scope+kind+project+slug 精确判断;reject --json 输出 JSON;+4 单测 | 全量 635 测试绿，已提交 Phase 4 CLI checkpoint `4400712` |
| 2026-06-19 | Phase 4b | Web UI：knowledge_routes.py(KB HTTP API)+ 接入 server.py + 自包含 static/knowledge.html 控制台(浏览/审核/设置)+ 6 API 单测 + 起服冒烟 | 改动：web/knowledge_routes.py(新), web/server.py, web/static/knowledge.html(新), tests/test_knowledge_routes.py(新)。全量 641 测试绿。 |
| 2026-06-19 | Phase 4b | 收口：PATCH config 容忍非 dict git/curation(不再 500);起服冒烟固化为 fetch_ui_response 自动化测试(status/静态页/PATCH);+4 单测 | 全量 645 测试绿，已提交 Phase 4b checkpoint `4249b4b` |
| 2026-06-19 | Phase 5 | retrieval 注入：knowledge_retrieval.py(项目优先检索+摘要)+ task_assignment 模板/orchestrator 注入 + 失败隔离(含 SystemExit)+ 7 单测(含真实 dispatch 注入 inbox) | 改动：services/knowledge_retrieval.py(新), services/orchestrator.py, prompts/task_assignment.md, tests/test_knowledge_retrieval.py(新)。全量 652 测试绿。 |
| 2026-06-19 | Phase 5 | 收口：检索前串 auto_pull_before_task(失败回退本地 KB);format_injection 硬字符预算(首条也按剩余截断);+2 单测 | 全量 654 测试绿，已提交 Phase 5 checkpoint `2bb1bae`。A/B 观察记为运行期验证项。 |
| 2026-06-19 | Phase 6 | 通用知识/wiki: aha kb add(create/update/append)+ review_after/list_stale_entries/aha kb stale + status stale 计数;通用知识检索;+5 单测 | 改动：store/knowledge.py, cli.py, cli_parser.py, tests/test_knowledge_phase6.py(新)。全量 659 测试绿。 |
| 2026-06-19 | Phase 6 | 最后收口：_terms 加 CJK bigram(中文知识可检索);kb add 普通 update 也保留既有 tags/review_after 等元数据;kb stale --json 摘要带 review_after;文档加「启用与验证」段;+测试 | 全量测试绿，作为 Phase 6 最后 checkpoint 提交 |

---

## 10. 已确认决策与后续待定问题

### 10.1 已确认（2026-06-19 用户拍板）
1. **D6 curation gate** → 默认 `manual`（人工确认入库）。
2. **D7 第一刀范围** → 先做「项目解决方案库」。
3. **存储布局 / 数据模型** → 按第 2、3 节实施（frontmatter 实现为零依赖 JSON，见顶部实现决策）。
4. **git 默认行为** → 当前产品策略为手动同步，`auto_pull=off / auto_commit=off / auto_push=off`，设置页只暴露“立即同步”。

### 10.2 后续待定（进入对应阶段前再定）
- **Phase 2**：远端鉴权方式（SSH key / token）与 push 冲突时的处理策略（rebase 后人工介入 vs 自动放弃）。
- **Phase 3**：每轮最多沉淀几条候选；敏感信息（密钥/绝对路径）过滤规则；sidecar 缺失/解析失败时的 fallback 策略。
- **Phase 5**：检索是否需要升级到向量/语义检索，还是朴素 key+tag 足够。
- **Phase 6**：wiki 同主题更新的合并策略（自动 merge vs 生成新版本待审）。

---

## 11. 启用与验证

知识库默认**关闭**（`knowledge.enabled=False`），对现有流程零影响。启用步骤：

### 11.1 启用与配置
- **开关**：编辑 AHA home 的 `config.json`，把 `knowledge.enabled` 设为 `true`；
  或用 Web 控制台「设置」页（见下）；或 `PATCH /api/kb/config`。
- **git 远端同步（可选）**：设 `knowledge.git.enabled=true` 与 `knowledge.git.remote=<url>`；
  默认 `auto_pull=off / auto_commit=off / auto_push=off`。用户在 KB 设置页点击“立即同步”时显式执行 commit/pull/push。
- **curation gate**：默认 `manual`（候选先入 `.pending` 待人工批准）；可设 `auto`（直写）/`off`（不沉淀）。
- 初始化骨架：`aha kb init`（首次由 finalize 触发时也会自动建骨架与 `.gitignore`）。

### 11.2 Web 控制台
- 启动 UI：`PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788`。
- 入口：主面板顶部 **Integrations 区的「知识库 / Knowledge base」**按钮 → 新标签打开整页控制台 `/static/knowledge.html`（自包含,样式继承全站 token,与微信操作台视觉一致；不接入 SPA 控制器图谱）。
- 功能：条目浏览、`.pending` 审核（批准/拒绝）、knowledge 设置表单（enabled/path/git remote+branch/curation gate）、同步状态提示（未提交/领先/落后/远端错误）和手动“立即同步”按钮。

### 11.3 常用命令
- `aha kb status`：路径 / 条目数 / pending / stale / git 状态。
- `aha kb add --kind wiki --title "标题" --body "…" [--scope general|personal] [--tag t] [--append] [--review-days N]`：手动沉淀/更新通用/个人文档（同标题=同主题更新；`--append` 追加段落）。
- `aha kb map build [--workspace PATH] [--project KEY]`：扫描生成项目 `navigation/index.md` 和 `navigation/modules/*.md` 候选。
- `aha kb pending` / `aha kb approve <cand_id>` / `aha kb reject <cand_id>`：审核 distill 候选。
- `aha kb list [--scope --kind --project]` / `aha kb show <id|slug>` / `aha kb search <query>`：浏览检索。
- `aha kb stale`：列出 `review_after` 已过期、需复核的条目。
- `aha kb sync [--push] [--no-pull] [-m msg]`：手动与 git 远端同步（ensure→commit→pull→push）。

### 11.4 闭环怎么跑
- **沉淀（do→distill）**：开启后，task finalize 和 memo completion report 优先使用 sidecar 提炼高价值候选；普通 bug fix 默认空候选，项目结构/模块认知进入 navigation，少量可复用排障进入 solutions → `aha kb pending` 审核 → `approve` 入库。
- **学习（learn）**：同项目下一个 task 派发前，若显式启用 `auto_pull`，`dispatch_task_to_main` 会先拉取再检索；默认手动同步模式下使用本地 KB。
- **项目导航（nav）**：注入时 `navigation/index` 始终置顶作为路由；`modules/*` / `flows/*` 只在任务标题/描述命中时逐层进入 prompt，无命中时不按最近更新时间兜底注入，避免大项目里读取无关模块文档。新增子文档时若直接父入口缺链接，沉淀链路会补一个最小父入口候选。
- **更新复核**：distill 会附带检索到的既有相关知识；若新结论冲突，候选会提示审核时更新/废弃旧条目，避免过期知识静默误导后续任务。

### 11.5 A/B 行为观察（运行期验证项）
纯单测无法覆盖「注入知识后 agent 是否少踩坑」。建议人工 A/B：
1. 选一个有重复性踩坑的项目，先在 `knowledge.enabled=false` 下跑若干同类 task，记录踩坑/返工次数。
2. 沉淀几条高质量解决方案条目（`aha kb add` 或批准 distill 候选），开启 `knowledge.enabled=true`。
3. 跑同类 task，对比 task-main prompt 是否含「项目已知经验」段、以及踩坑/返工是否下降。
4. 注入内容可在 run 目录的 prompt artifact / main inbox 中核对。
