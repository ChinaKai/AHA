# AHA Token Saving Context Planner 设计

> 2026-07-07 当前状态：Project Map / Project Context Index 已从活跃产品面切除。token saving 现在走 `provider=nav`，旧 `provider=map` 只做兼容迁移到 `nav`。下文保留了部分历史设计讨论；实现和调试以项目导航 `navigation/flows/token-saving.md` 和当前源码为准。

> 状态：Context Planner / EVD 主链路已接入；EVD 历史证据读侧降噪、KB growth hard loop、EVD 首屏降噪和无横向滚动已接入；Project Map 已删除，下一步做 nav-only 真实任务回归验证。
> 最后更新：2026-07-07。
> 维护规则：讨论过程中只要形成明确结论，就更新本文档对应章节，并在「进展日志」记录。没有结论的问题放到「开放问题」，不要只留在聊天记录里。

## 1. 核心判断

Codex/Claude 这类 agent 后端没有真正的远端连接态记忆。每一轮模型调用都是一次新的请求。所谓连续对话，主要是本地运行时把固定指令、task 状态、历史消息、工具结果、代码片段、摘要和最新用户输入重新组装后发给远端模型。

因此 AHA token saving 的核心不是让 agent 少思考，而是做更好的上下文路由：

```text
自然语言需求
  -> 注入知识库/项目 map 入口和使用契约
  -> agent 按自然语言主动 pull 需要的 nav/map/entry
  -> agent 精准读代码
  -> 修改和验证
  -> agent 基于当前任务证据维护项目 KB 原文
  -> AHA 记录 evidence 和命中/失配信号
  -> 需求结束后 reset/compact
  -> 下一个自然语言需求重新从入口契约启动
```

目标是让每一轮进入 agent 的请求更小、更干净、更相关。

## 2. 目标工作流

```text
自然语言需求
  -> Context Planner
  -> 知识库/Project Map Pull Contract
  -> 后端 agent 执行
  -> 验证
  -> 直接维护 project navigation / project solutions
  -> AHA 记录 evidence
  -> reset 或 compact
  -> 下一个自然语言需求
```

节省 token 的位置：

1. **准确读代码**：减少无关文件、无关日志、无效搜索结果进入模型上下文。
2. **复用压缩后的历史经验**：用结构化知识替代完整历史聊天。
3. **需求结束后 reset/compact**：避免新需求携带旧需求历史。
4. **大内容引用化**：大日志、大响应体、大命令输出只存 artifact，prompt 中只放必要摘要和引用。

关键产品原则：

- 用户只负责用自然语言表达需求。
- 用户不负责选择知识库条目、检索关键词、模块路径或历史经验。
- AHA 不用关键词替 agent 判断语义相关性；关键词检索最多作为调试/显式候选能力，不能把命中内容直接塞进 prompt。
- AHA 负责注入稳定入口、使用方法、信任优先级、预算边界和当前任务级 evidence 回写协议。
- agent 根据当前任务自然语言主动读取必要的 `navigation/index`、`modules/*`、`flows/*`、`solutions/wiki` 和 `/aha map query` 结果，并自行决定采用或跳过。

## 3. 知识库与项目 Map 职责划分

知识库和项目 map 必须职责清晰，不能把整个知识库或整个 map 塞进每轮 prompt。

当前 AHA 已经有通用项目 map 基线：

- `Project Context Index`：生成式本地项目上下文索引，缓存到 AHA runtime，不是人工审核的长期知识条目。
- `/aha map status|refresh|query <terms>`：查看、刷新、查询项目 map。
- Web Knowledge Map tab：查看生成的 project map、文件树和搜索结果。
- task token saving/project map 开关：在 prompt 中注入 KB/Map Pull Contract；旧 compact map capability block 只作为兼容/兜底，不自动注入全部 map 查询结果。

因此本文档里的 `map` 默认指 **现有 Project Context Index / `/aha map` 查询能力**，不是要新增一个独立 KB 类型。需要人工维护的“流程关系图”优先落到 project navigation 的 `flows/*.md`。

| 类型 | 职责 | 使用时机 |
| --- | --- | --- |
| `nav` | 人工/agent 审核后的项目导航：从哪里开始看。包括模块、流程、入口函数、API、诊断路径。 | 最高优先级，先于代码搜索使用。 |
| `map` | 当前已实现的生成式项目索引：根据关键词定位具体文件、符号、配置、构建记录、DTS、测试和入口点。cache 是生成物，不能手改；extractor/schema/resolver/query expansion/ranking/refresh 逻辑可以自修复。 | nav 缩小范围后，用于找“现在该打开哪些具体文件/记录”。 |
| `entry` | 经验条目：修复方案、操作流程、坑点、验证命令、失败尝试。 | 当前需求命中类似问题时使用。 |

推荐检索顺序：

```text
nav -> map -> entry
```

`nav` 回答“从哪里开始”，`map` 回答“哪些具体代码/配置/构建记录最相关”，`entry` 回答“以前怎么解决过”。

如果问题需要表达“模块/API/事件之间怎么连起来”，第一版不新增独立 map KB 类型，而是写入 `navigation/flows/*.md`。也就是说：

```text
人类可读流程关系 -> navigation flows
机器生成代码定位 -> Project Context Index map
```

## 4. 当前 AHA 知识库实现基线

继续讨论 Context Planner 前，必须先承认当前 AHA 已经有一套知识库和项目 map 的基线实现。后续设计不是从零开始，而是在这套 producer/consumer 管道上补“自然语言自动定位”和“反馈闭环”。

### 4.1 存储模型

当前长期知识库由 `src/aha_cli/store/knowledge.py` 管理，知识库目录独立于 run/task，默认在 AHA home 下，也可通过 `knowledge.path` 覆盖。

主要结构：

```text
general/
  wiki/
  solutions/
personal/
  wiki/
  solutions/
projects/<project-key>/
  navigation/
    index.md
    modules/*.md
    flows/*.md
  solutions/
.pending/
capture/
runtime/project_context/<project-key>/<workspace-id>/
```

长期 KB 条目是 Markdown 文件，frontmatter 使用 JSON，不依赖 YAML。支持的条目类型是：

- `wiki`：通用或个人教程/解释类知识。
- `solutions`：可复用解决方案、排障流程、操作经验。
- `navigation`：项目导航，包含 `index`、`modules/*`、`flows/*`。

`personal` scope 可存储和检索，但不会自动注入 task prompt。项目相关知识主要应该进入 `projects/<project-key>/navigation`，少量真正可复用的排障经验进入 `projects/<project-key>/solutions`。

`Project Context Index` 不属于长期 KB 条目。它是 runtime 生成缓存，路径在 `runtime/project_context/<project-key>/<workspace-id>/`，包含 `index.json`、`summary.md` 和分片记录，用于定位文件、符号、配置、构建、DTS、测试、入口点。

### 4.2 生产者

当前代码里有多条候选入口，但新设计必须区分“核心生产者”和“辅助生产者”。

核心结论：**自动自增长、自修复是本方案的灵魂；手动反馈只作为辅助。**

长期目标里的主生产者不是 task final，而是 AHA 在每轮执行过程中自动观测到的事实：

- Context Pack 命中了哪些 nav/map/entry。
- agent 实际读取、修改、验证了哪些文件和命令。
- map 结果是否被采用。
- nav/entry 是否被证明准确、缺失或过时。
- 验证是否成功，以及最终关键路径是什么。

这些观测应自动生成 `context_hit_ok`、`nav_stale`、`map_miss`、`entry_wrong`、`missing_nav`、`missing_entry` 等信号，并生成 agent-owned `maintenance_actions` / `maintenance_plan`。普通任务链路不再把这些信号转成 pending candidate 或用户建议；agent 在有当前任务证据时直接维护项目级 approved KB Markdown、刷新 stale generated map cache，或修复 map 生成/查询源码逻辑，并把结果记录到 EVD。

当前仍存在的 `task final / memo report sidecar` 只能视为历史兼容或补充入口，不再作为新设计的主生产链路。后续设计和验收不能依赖 agent 在 final 里主动总结知识，因为这会把自增长能力退化成人工/模型自觉行为。

辅助生产者包括：

1. **`/aha nav <message>`**

   这是显式项目导航反馈入口。AHA 生成 `navigation_command.md` 提示词，让当前 sticky agent 只基于已有 session context 产出 navigation sidecar。AHA 再把候选送入同一套 distill/curation 管道。

2. **`/aha kb <message>`**

   这是普通知识反馈入口。AHA 生成 `knowledge_command.md` 提示词，让当前 sticky agent 产出 `solutions` 或 `wiki` sidecar，并明确不生成 navigation。它适合沉淀可复用经验、通用解释、操作流程。

3. **Capture note distill**

   用户可以把原始 note、截图、日志、想法放入 `capture/`。`knowledge_capture_distill.py` 会启动一次独立 codex/claude agent，把 raw note 整理或生成成 sidecar candidate，再进入 `.pending`。这条链路是：

   ```text
   raw capture note -> distill agent -> pending candidate -> approve -> KB entry
   ```

4. **Project navigation bootstrap / project map refresh**

   Project navigation bootstrap 会在项目没有 `navigation/index.md` 时生成第一批 navigation candidate，仍然走 curation。

   Project map refresh 由 `project_context_index.py` 生成 runtime map，不写长期 KB。它是代码定位缓存生产者，不是经验知识生产者。刷新动作来自 `/aha map refresh` 或 Web Knowledge Map UI，正常 task prompt 不会自动 build map。

### 4.3 消费者

当前知识消费者分为四类。

1. **task prompt 的 KB 使用契约**

   新的 token saving 主链路不再把 `knowledge_context_for_task()` 的关键词检索结果注入 task prompt。首轮和 sticky delta 只注入一个短的 Pull Contract，内容包括：

   - KB root、project key、`navigation/index.md` 入口。
   - Project Map cache 状态和 `/aha map query <terms>` 使用方法。
   - agent 自主判断相关性：需要时读取，发现不相关时跳过。
   - KB/map 只是路标，任何分析和修改前都必须读真实源码。
   - 当前任务级自增长/自修复协议：只基于本任务读写、命令、验证和回复摘录做窄范围 KB 原文维护，不做全库重建，不自动删除长期 KB。

   `knowledge_context_for_task()` 仍可作为兼容/显式检索工具存在，但不再作为 token saving prompt 的主注入路径。

2. **sticky session context delta**

   `chat_prompt_context.py` 会用 fingerprint 记录已经交付过的知识/能力块。sticky session 里，如果 KB context 或 Project Map capability 没变，不会每轮重复注入同一段固定上下文。

3. **Context Pack 与 Project Map capability block**

   当 task token saving 启用且 provider 为 `map` 时，普通用户消息前会先尝试生成 `backend_context_pack.md` 这类 KB/Map Pull Contract。它只注入入口、使用边界和 evidence 协议，不自动注入关键词 KB 命中或 map query 结果。

   当 Context Pack 不可用、但当前 workspace 已有 map cache 时，`backend_project_map_context.md` 这类旧能力说明仍可作为兼容/兜底块：

   - map index 路径。
   - project key / workspace id。
   - generated_at / counts / flavors / profiles。
   - 要求用聚焦 terms 执行 `/aha map query <terms>` 或检查 map cache。

   两条路径都不会自动注入 map query 结果。也就是说当前实现是“告诉 agent 有 KB/map 入口可用”，不是“每轮替 agent 自动查 map 并塞结果”。

4. **`/aha map` 和 Web Knowledge UI**

   `/aha map status|refresh|query <terms>` 由 AHA 本地处理，不路由给后端 agent。`query_project_context_index_cache()` 会读取已有 runtime map，返回 files/packages/symbols/configs/build/device_tree/entry_points 等命中，并用 `format_project_context_reference()` 生成紧凑引用。

   `project_context_resolver.py` 已经实现了 deterministic nav -> map query 扩展：先读 `navigation/index.md` 和第一跳 module/flow docs，按自然语言 query 打分，命中后提取 `related_files`、code spans、路径片段等作为 expanded terms/path hints，再交给 Project Map 排序。不存在的 nav path hint 会从正向 hint 中剔除并作为 `stale_path_hints` 暴露；`project_context_index.py` 会对这些 stale hint 降权，避免旧导航路径继续推高结果。Slash query 和 Web map search 会显示 nav route、expanded terms、stale path hint 这类诊断信息。

   map 的边界要分两层：runtime cache 原文不能手改；但生成和查询逻辑必须能自增长、自修复。当前任务证据如果证明 map stale、extractor 漏抓、schema 不够、query expansion 选错 nav hint、ranking 把关键文件排丢，AHA 应记录结构化 gap，agent 在 AHA/map 相关任务里可以直接修 `project_context_index.py`、`project_context_resolver.py` 或相关测试。

### 4.4 审核与写入

长期 KB 现在分两条写入路径：

1. **普通任务链路**

   agent 根据 Pull Contract 主动读取 KB/map/source。若当前任务已经验证出新的项目导航、项目解决方案、过期路径或错误说明，agent 直接修改对应 approved Markdown 原文，范围必须窄，且要在条目里保留可核验的 related files、命令或来源说明。这个路径不进 `.pending`，因为它本质上是当前任务的一部分，和改源码一样由 agent 负责维护。

   适用范围只限 project-scoped `navigation` 和确实可复用的 project `solutions`。generated Project Map cache 不能手改；缺失或过期时使用 `/aha map query`、refresh 或把稳定入口沉淀到 project navigation。若当前任务就是 AHA/map 能力修复，则应修 map extractor/schema/resolver/query expansion/ranking/refresh 逻辑，而不是改 cache 原文。general/personal/wiki 类知识默认不走普通任务自动直写，除非用户明确要求。

2. **显式反馈 / 导入链路**

   手动 `/aha nav`、`/aha kb`、capture distill、project navigation bootstrap 仍然走 curation gate：

```text
sidecar/capture/bootstrap
  -> normalize candidates
  -> filter navigation candidates
  -> ensure parent navigation links
  -> validate navigation candidates
  -> manual: .pending
     auto: write entry
  -> approve: write/merge Markdown entry
```

`enqueue_candidate()` 会把候选写入 `.pending/`，并用 source group + 标题等信息尽量保持幂等，避免重复候选。

`approve_candidate()` 才会把 pending candidate 提升为正式 Markdown entry。navigation 写入使用 `write_entry_preserving_navigation()`：同 slug 的 navigation 更新会合并 section 和 meta，不会简单覆盖旧文档。图片类 memo/capture asset 在 approve 时会被提升到 entry-local assets。

默认 gate 是 `manual`。`auto` gate 已有，但只适合显式导入链路中的低风险场景；普通 task evidence 不再通过 gate 生成候选。

### 4.5 当前能力边界

基于现有实现，必须把下面几点当作事实边界：

- KB 总开关默认是 disabled，需要配置启用。
- 当前 KB retrieval 是 term overlap，不是 embedding/vector，不会做复杂语义重排。
- task prompt 只注入有限引用和短摘要；完整正文需要 agent 按 path 读取。
- `navigation/index` 置顶，但 module/flow detail 默认不全量注入。
- Project Map 已有自然语言经 nav 扩展的查询能力，但触发点仍是 `/aha map query` 或 Web 查询。
- task token saving 启用且 provider 为 `map` 时，普通用户消息前已会生成 Context Pack；但不会自动执行 planner 之外的 map query。
- Project Map 不会在普通 prompt 生成时自动 build/refresh；refresh 是显式动作。
- 当前自增长/自修复已经从“自动生成候选”改为“记录 evidence + 在任务提示词中要求 agent 直接维护项目 KB 原文”；是否真正写入取决于 agent 本轮是否有足够证据。
- 当前已具备本轮 query/evidence 级 stale hint 暴露与降权：resolver 输出 `stale_path_hints`，ranking 记录 downrank，evidence/API/UI 暴露 `routing_health`、`gap_reasons` 和 stale path diagnostics。跨任务长期统计降权仍未自动化；普通任务链路优先窄范围 repair/deprecate 项目 KB 原文，不做全库重建。
- `task final` 不再作为新设计主链路；即使代码里保留兼容 hook，也不能把它当成自增长能力的核心。

### 4.6 对 Context Planner 的设计约束

因此新设计第一步不是再造知识库，也不是让 AHA 用关键词替 agent 选择知识，而是把现有组件串成 agent-pull 的上下文契约：

```text
用户自然语言
  -> AHA 注入 KB/Map Pull Contract
  -> agent 自己判断语义相关性并读取 navigation/solutions/map/source
  -> agent 执行、验证，并在有证据时直接维护项目 KB 原文
  -> AHA 记录 evidence 和命中/失配信号
  -> 手动 /aha nav、/aha kb、capture/bootstrap 仍走 sidecar/pending/approve
```

这意味着后续讨论重点已经从“是否注入 planner”转向两个闭环：

1. 已落地：自然语言 turn 进入后端 agent 前生成 KB/Map Pull Contract。
2. 已落地：AHA 把现有 nav/map/entry 的入口和使用方法交给 agent，而不是用关键词替 agent 选内容。
3. 已落地：agent 执行过程中的 Context Pack、map query、runtime-inferred result 和显式 `agent_kb_feedback` 会进入 task-scoped EVD。
4. 暂缓：自动 reset/compact 不再绑定 finalization；当前保持手动，由用户决定何时 reset。

## 5. 最难的问题：准确知识库

当前设计最大的难点不是生成 prompt，而是建立一个长期准确的知识库。这个知识库必须具备三种能力，并且前两者必须自动化，不能主要依赖用户手工反馈：

1. **自增长**：做任务时能发现新入口、新流程、新经验，并直接沉淀到项目级 KB 原文。
2. **自修复**：当旧知识与实际代码不一致时，能标记、修正或降权。
3. **自然语言自主定位**：用户只说自然语言需求时，agent 能基于 AHA 提供的入口和规则，自己判断该读哪些 nav/map/entry；AHA 不用关键词把噪音预塞进 prompt。

### 5.1 自增长

任务执行过程中，AHA 应自动记录 agent 实际验证过的事实：

- 实际读取了哪些关键文件。
- 实际修改了哪些文件。
- 哪些函数/API/路由/事件是入口。
- 哪些测试或命令真正有效。
- 哪些旧知识被 agent 主动读取并证明有效。
- 哪些新发现可以补充到 nav/entry，或用于改进 map query / map refresh / extractor / ranking。

这里的“自增长”不是扫描并重建完整知识库，而是对**当前任务证据**做增量 CRUD，并由 agent 直接维护项目级 KB Markdown：

- Create：本任务发现了新的模块入口、流程、诊断路径或经验，创建对应 project navigation/project solutions 条目。
- Read：本任务读取 Pull Contract、agent 主动读取的 nav/map/entry、实际命令和实际文件路径，形成可追踪证据。
- Update：本任务发现已有 nav/entry 不完整，直接窄范围更新 approved Markdown 原文。
- Delete/Deprecate：本任务发现旧路径或旧经验失效，优先在原条目中标记 stale/deprecated 或修正内容；不直接物理删除长期 KB。

任务过程中或任务结束时，这些事实应该直接反映到当前项目 KB 的稳定原文：

- 发现新的模块入口或诊断路径 -> 更新 `nav`。
- 确认新的调用链、数据流、事件流 -> 更新 `navigation/flows/*.md`。
- 形成可复用修复经验或验证流程 -> 新增/更新 `entry`。
- 发现现有 map 缺失重要文件/符号/配置 -> 触发/建议 map refresh，或把稳定入口写入 project navigation；不手改 generated map cache。
- 发现 map 反复查不到已验证的关键路径 -> 记录 `map_stale_cache`、`map_extractor_gap`、`map_query_expansion_gap`、`map_ranking_gap` 或 `map_coverage_gap`，必要时修生成/查询逻辑。
- 如果 nav/map 没有定位到真实代码，或指向 stale/wrong 路径，agent 不能只绕过去完成任务；找到并验证真实源码路径后，必须新增或修正 project navigation，写入 verified files、entrypoints、flow 和验证命令。

这条自动链路不是全库重建，也不是把所有探索都写入长期知识。只有本任务已经读源码、跑命令或完成验证的项目事实才允许直接写入。手动 `/aha kb`、`/aha nav`、capture 仍是补充渠道，并继续走 pending/approve。

### 5.2 自修复

知识库需要知道“这条知识什么时候不再可信”。

触发自修复的信号：

- agent 主动读取的 nav/map/entry 指向的文件不存在或入口函数不存在。
- agent 按 nav/map 找代码失败，必须走其他路径。
- 验证结果与 entry 里的建议冲突。
- 同一自然语言需求反复命中同一条知识但任务失败。
- 代码改动影响了某个 nav/entry 的 related_files，或让 map 索引变旧。
- 用户明确指出知识不准确。

自修复动作应该基于当前任务证据直接修复项目级 KB 原文，并记录状态信号。它的作用域同样是当前任务证据，不做全库重建：

- 降低 confidence 或补充失效条件。
- 标记 stale/deprecated。
- 修正错误路径、入口或流程描述。
- 把失败路径写入 entry 的“无效方案/失效条件”。
- 对高风险或非项目级条目，改为要求用户显式确认或使用 `/aha kb` 候选链路。
- 对 generated map 触发 refresh；如果 refresh 后仍缺失，修 extractor/schema/resolver/query expansion/ranking 或记录对应 gap。

### 5.3 自然语言自主定位

自然语言到知识库的定位不能交给 AHA 关键词检索做主判断。AHA 的稳定职责是提供入口和操作方法，语义相关性判断交给 agent：

```text
用户需求
  -> AHA 注入 Pull Contract
  -> agent 先读 navigation/index
  -> agent 根据任务语义选择 modules/*、flows/*、solutions/wiki
  -> 必要时 agent 执行 /aha map query 或检查 map cache
  -> agent 读取真实源码并验证
  -> agent 在有证据时直接维护项目 KB 原文
  -> AHA 记录 evidence 和命中/失配信号
```

Pull Contract 必须给 agent 的信息：

- KB root、project key、navigation index path。
- `/aha map status|query|refresh` 的使用边界。
- 信任优先级：当前用户请求 > 当前源码 > 当前任务 evidence > project navigation > project solutions > general wiki。
- 相关性决策权：agent 可以读取、采用、忽略、标记不相关或标记过期。
- 预算规则：先读目录/摘要/导航，再按需读正文和源码；不要把无关 KB 正文带入上下文。

agent 读取原则：

- `navigation/index` 是入口，不是结论。
- `modules/*` 和 `flows/*` 用于缩小范围。
- `solutions/wiki` 只在语义确实相似时读取。
- `/aha map query` 是代码定位辅助，不是长期知识；generated map cache 只能 query/refresh，不能手改。
- 所有 KB/map 结果都必须回到真实源码验证。

### 5.4 检索责任边界

这里有两种实现路线，task-151 已证明关键词主链路会把无关 `wiki` 注入 prompt：

1. **AHA 内部检索优先**：AHA 代码根据自然语言自动召回 nav/entry、执行 map query，并生成 Context Pack。
2. **agent 自主检索优先**：AHA 只注入入口和使用契约，让 agent 在需要时主动查询知识库和 map。

设计结论修正：第一版主路径改为 **agent 自主检索优先**。

原因：

- 用户不应该知道也不应该操作 KB 检索细节。
- 但 AHA 关键词检索无法可靠判断自然语言语义，尤其会因为泛词命中把通用 wiki 噪音塞进 prompt。
- agent 已经要理解当前任务、读代码和验证，语义相关性判断天然属于 agent。
- AHA 更适合做边界清晰的基础设施：入口、工具、预算、证据记录、手动候选链路。

推荐执行策略：

```text
用户自然语言
  -> AHA 注入 KB/Map Pull Contract
  -> agent 自己读取 navigation/index 和必要条目
  -> agent 自己执行 /aha map query 或定向 rg
  -> AHA 观察实际读写路径、命令、验证和回复
  -> agent 基于当前任务证据直接 create/update/repair/deprecate 项目 KB 原文
```

也就是说，agent 不需要用户告诉它怎么检索，但 agent 要承担“语义选择”的主职责；AHA 只提供入口和记录反馈闭环。

## 6. Context Planner

Context Planner 在调用后端 agent 前运行。它把用户请求转换成有预算的 Context Pack。

### 6.1 已确认设计结论

第一版目标不是“智能全自动检索”，而是实现一个 **可解释、可控、可回退** 的上下文入口契约。

触发策略：

- task token saving 启用且 provider 为 `map` 时，普通用户消息进 agent 前经过 Planner；final/memo 和 `/agent` command 不走这条普通 turn planner。
- 新需求注入完整 Pull Contract：KB root、project key、navigation index、Project Map 使用方法、evidence 回写协议。
- 同需求 follow-up 使用轻量 Pull Contract 或 sticky delta：只补充入口/能力变化，不重复塞 KB 内容。
- 是否为新需求由 AHA 内部判断，不能把判断负担交给用户；但知识相关性由 agent 根据任务语义判断。

预算策略：

- Pull Contract 默认目标为 1200-2500 chars，硬上限 4000 chars。
- 不自动注入关键词命中的 `nav/entry/wiki`。
- 不自动注入 map query 结果。
- 只注入入口、路径、命令用法、信任优先级、evidence 协议。
- 大正文、大日志、大文件内容只能由 agent 按需读取 artifact/path/ref。

信任优先级：

```text
当前用户请求
  > 当前源码/命令验证结果
  > 当前任务 evidence
  > project navigation
  > project solutions
  > general wiki
```

低置信内容不应进入 prompt。宁愿只给入口说明，也不要把噪音放进 Context Pack。

agent 反馈策略：

- AHA 需要记录 `context_hit_ok`、`nav_stale`、`map_miss`、`entry_wrong`、`missing_nav`、`missing_entry` 这类结构化信号。
- 第一阶段尽量从实际行为推断：agent 读了哪些文件、map 结果是否被采用、最后改了哪些文件、验证是否通过。
- agent final 可以补充反馈，但不强迫每轮输出长报告。
- 如果 nav/map 没有把 agent 带到正确代码，agent 找到真实路径后要立刻把这个事实写回 project navigation；普通任务不改 generated map cache，AHA/map 任务才修 extractor/resolver/ranking/refresh 逻辑。

自修复边界：

- 普通任务 evidence 不自动生成 pending。
- 可以自动记录命中失败、降低本轮排序权重，并通过 advisory `maintenance_suggestions` 和结构化 `maintenance_plan` 提示 agent 做窄范围修复；建议/计划本身不自动写 KB。
- project navigation 和 project solutions 可以由 agent 直接改 approved Markdown 原文；general/personal/wiki 默认仍要求用户显式确认或走 `/aha kb`。
- 不扫描/重建完整知识库；只根据当前任务的 Pull Contract、agent 实际读取的 KB/nav/map、实际读写路径、命令、验证和回复摘录做增量维护。
- “删除”第一阶段只表达为 stale/deprecate/repair，不自动物理删除 KB 文件。
- 手动反馈只用于补漏和纠错，并作为 candidate-review path，不是自增长/自修复主路径。

完成与 reset/compact：

- 需求完成后先写 task summary、KB 维护结果、context evidence 和验证状态。
- 不在 final 后立刻盲目 reset。
- 下一个用户消息进入时，AHA 判断是 follow-up 还是新需求。
- 若判断为新需求，再 reset/compact，并用 durable state + KB/Map Pull Contract 重新启动。

map 边界：

- map 是代码定位器，不是长期知识库。
- map cache 是生成物，不能手改；map 生成/查询逻辑是产品能力，必须自增长、自修复。
- map missing/stale：提示或触发 refresh，并记录 `map_stale_cache`。
- map query miss：先换 query 或走 nav/rg 兜底。
- 多次 miss 且实际找到关键文件：修正 project navigation，或修复/记录 map extractor、schema、query expansion、ranking 改进事项。
- 不把 map 原始结果写入 KB，只把稳定入口、流程、诊断路径写入 navigation。

输入：

- 最新用户需求。
- 当前 run/task/agent 元数据。
- 已有 project navigation。
- 已有 Project Context Index 状态。
- KB root / project key / navigation index 是否存在。
- 如果是同一需求延续，可带最近 task summary。

注意：用户输入始终是自然语言。Context Planner 不要求用户指定 KB 类型、slug、模块名或检索参数；agent 根据自然语言和入口契约自行选择是否检索。

输出：

- 需求短摘要。
- KB/Map 入口说明。
- 使用方法和信任优先级。
- 当前任务级自增长/自修复协议。
- 给 agent 的明确边界：不要把 KB 当结论，读源码后再分析/修改。
- 本轮上下文预算。

示例：

```markdown
## 需求
修复 Observe Proxy recent 请求列表展示。

## 优先入口
- 模块：`modules/observe_proxy`
- 流程：`flows/observe-proxy-inspection`

## 相关文件
- `src/aha_cli/services/observe_proxy.py`
- `src/aha_cli/web/static/observe_proxy_panel.js`
- `src/aha_cli/web/static/styles.css`
- `tests/test_observe_proxy.py`
- `tests/test_frontend_static.py`

## 约束
- 先看上述文件；除非导航明显不准确，否则不要全仓库扫描。
- 优先跑 focused tests。
- full body 继续保持 lazy-load。

## 验证
- `node --check src/aha_cli/web/static/observe_proxy_panel.js`
- `python3 -m pytest tests/test_observe_proxy.py tests/test_frontend_static.py -q`
```

## 7. Agent 工作循环

agent 仍然可以发现新事实，但默认路径应该被 AHA 收窄：

```text
1. 阅读 Context Pack。
2. 优先检查列出的入口。
3. 如果入口错误，报告 mismatch 并窄范围搜索。
4. 只修改受影响文件。
5. 跑 focused verification。
6. 只有 focused path 不足时，才扩大搜索或跑更大测试集。
```

AHA 需要观察并记录：

- 实际读取的文件。
- 实际修改的文件。
- 有用的命令/测试。
- 哪些 nav/entry 是准确的。
- map query 结果是否把 agent 带到了正确文件。
- 哪些 nav/entry 是缺失、过时或错误的。
- 哪些 map query miss 暴露了索引或 ranking 缺口。
- 哪些意外文件或流程变成关键路径。

这些观测结果用于任务中的知识维护和任务结束后的质量度量。

## 8. 知识更新循环

agent 应该在解决任务时显式、窄范围地维护项目 KB，而不是静默全库改写。推荐流程：

```text
agent 执行任务
  -> 读取 navigation/solutions/map/source
  -> 验证事实
  -> 直接修正 project navigation / project solutions Markdown
  -> AHA 记录 evidence
```

直接维护类型：

- `nav`：模块职责、入口点、诊断路径新增或修正。
- `navigation/flows`：调用链、数据流、事件流确认或变化。
- `entry`：可复用修复、流程、失败模式、验证方式。
- `map` 改进不是直接编辑 KB cache：通常表现为刷新 Project Context Index，或把稳定入口沉淀到 project navigation。

手动 `/aha nav`、`/aha kb`、capture/bootstrap 仍走 candidate-review。general/personal/wiki 默认不由普通任务自动直写。

## 9. 需求生命周期

AHA 需要区分“同一需求继续做”和“新需求开始”。session 策略要跟着这个边界走。

建议状态：

```text
active -> verifying -> finalizing -> completed -> archived/reset
```

完成信号：

- agent 已输出 final answer。
- 没有命令或工具仍在运行。
- 验证通过，或 final answer 明确说明未验证原因。
- 没有 pending sub-agent/work item。
- task 状态进入 done/awaiting_user 等非执行状态。
- 最新用户消息不是要求继续处理同一需求。

完成后：

1. 写 compact task summary：目标、关键决策、改动文件、验证、剩余风险。
2. 记录 KB 维护结果、map refresh 建议和 context evidence。
3. 在下一个无关需求前 reset 或 compact backend-native session。
4. 下一个需求从 AHA durable state + KB 重新生成 Context Pack，而不是继续依赖旧聊天历史。

## 10. Reset 与 Compact 策略

新需求：优先 reset。

同一需求继续，但上下文过大或包含大量探索历史：compact。

保留：

- 当前任务目标。
- 当前实现状态。
- 已确认决策。
- 改动文件。
- 验证状态。
- 已知 blocker 和风险。

丢弃：

- 完整旧聊天。
- 已有 artifact 的长命令输出。
- 对最终方案无贡献的探索过程。
- 上一个无关需求的历史。

## 11. Observer 的作用

Observe Proxy 是这个设计的度量层。

它应该回答：

- 本轮真实发给后端的 request body 是什么。
- 固定 system/developer/tool schema 占多少。
- 哪些历史消息仍在请求里。
- 哪些工具结果被重复携带。
- Context Pack 是否真的减少了 input。
- reset/compact 是否真的清掉了无关历史。

任何 token saving 改动都应该能在 observer 数据或 token metrics 上看到效果。

## 12. 实现阶段

### Phase 1：手动设计与观测

- 用 Observe Proxy 看真实请求。
- 手动使用 `/aha nav`、`/aha kb`、capture 验证候选结构；流程关系写 navigation flows。
- map 仍使用当前 `/aha map refresh|query` 手动刷新和查询。
- 用户手动确认保存。
- 用户手动 reset/compact。

验收：

- 能根据 observer 解释某个请求为什么大。
- 能从真实任务沉淀有用项目导航。
- 手动 reset 后能看到无关上下文减少。

### Phase 2：Context Planner MVP（第一刀已完成）

- 后端调用前增加 planner step。
- 生成小型 KB/Map Pull Contract。
- 只注入入口说明、使用方法、信任优先级和当前任务级 evidence 回写协议。
- 不自动检索 nav/entry/wiki 并塞入 prompt。
- 不自动执行 project map query 并塞入结果。
- 记录 Context Pack 交付证据；更细的实际读取、采用、跳过或发现缺失路径归入 Phase 3。
- 第一刀绑定现有 task token saving `provider=map` 开关：未启用时不改变 prompt。
- 第一刀只读取已有 map cache，不自动 refresh/build。
- 第一刀只输出入口和约束，不启动额外 agent 做检索。

验收：

- 新需求能从 KB/Map 入口契约开始。
- prompt 不再出现关键词误召回的无关 KB 条目。
- agent 少读无关文件，并在需要时主动读取 navigation/map。
- observer 中能看到有边界的 planner section。
- sticky session 后续轮次在 token saving 开启时也能收到小型 Pull Contract，而不是只收到裸用户消息。
- 无 KB、无 map 时安静降级，不影响原有 prompt。

当前实现切片：

- 新增 `src/aha_cli/services/context_planner.py`，负责每轮生成 bounded Context Pack。
- 新增 `backend_context_pack.md` 模板，明确 Context Pack 是 KB/Map Pull Contract，不包含自动关键词命中的 KB/map 内容，不在 prompt assembly 中 refresh/build map。
- `chat_prompt_context.py` 在非 final/memo、非 `/agent` command 的普通用户消息前调用 planner。
- full prompt 会把 Context Pack 附加到 task context 后面。
- sticky delta prompt 在 token saving 开启且有 KB/Map 入口时会携带 pack，即使该消息原本是 `plain_sticky`，也不会只透传裸用户消息。
- Context Pack 当前绑定已有 task token saving `provider=map` 开关，未启用时不改变 prompt。
- map cache 存在时只输出 map 入口和 `/aha map query <terms>` 用法，不输出 query 结果。
- KB 启用时只输出 KB root/project key/navigation index 入口，不输出 `retrieve_for_task()` 命中条目。

### Phase 3：知识反馈循环

- 自动对比 Context Pack 与实际读取/修改/验证路径。
- 由 agent 基于当前任务证据直接维护 project nav/solutions Markdown；流程关系写 navigation flows。
- nav/map 定位失败或不准确时，agent 找到真实代码后必须新增或修正 project navigation，而不是只在当前任务里临时绕过。
- 自动记录 map miss / stale cache / extractor gap / query expansion gap / ranking gap，必要时触发 refresh 建议、修 map 逻辑或沉淀稳定入口到 navigation。
- 自动生成 `context_hit_ok`、`nav_stale`、`map_miss`、`map_stale_cache`、`map_extractor_gap`、`map_query_expansion_gap`、`map_ranking_gap`、`map_coverage_gap`、`entry_wrong`、`missing_nav`、`missing_entry` 等结构化信号。
- CRUD 语义只作用于当前任务 evidence 和项目 KB 原文：create/update/repair/deprecate，不触发完整知识库重建。
- 手动 candidate-review 队列/UI 继续服务 `/aha nav`、`/aha kb`、capture/bootstrap。
- 标记 stale 或被现实代码反驳的条目。
- 手动 `/aha nav`、`/aha kb`、capture 只作为辅助补录入口，并走候选审核。

验收：

- 新发现不用手动复制、也不依赖 task final 主动总结，就能进入 project navigation/project solutions。
- 错误导航可以被 agent 基于当前任务证据直接修正或标记 stale。
- 纯命中场景只记录 evidence，不制造无价值 pending；miss/stale/wrong/missing 驱动窄范围 KB 原文维护或 map 生成/查询逻辑修复。
- 更新后的知识能改善下一次同类任务。

当前实现切片：

- 新增 `src/aha_cli/services/context_evidence.py`，会把 Context Pack 交付和 turn 后推断结果写入 `tasks/<task-id>/context_evidence.jsonl`。
- `chat.py` 在 prompt metrics 后记录 `context_pack_recorded`，并在普通 agent turn 结束后调用 evidence distill。
- 已有信号包括 `context_hit_ok`、`nav_stale`、`map_miss`、`map_stale_cache`、`map_extractor_gap`、`map_query_expansion_gap`、`map_ranking_gap`、`map_coverage_gap`、`entry_wrong`、`missing_nav`、`missing_entry`。
- 目前 evidence 记录 signals、crud_actions、commands、actual_files、map_diagnostics、routing_health、kb_scope_policy、maintenance actions 和结构化 maintenance_plan；这些是 agent-owned 维护动作，不是给用户处理的建议。
- `map_diagnostics` 会暴露 `gap_reasons`、`stale_path_hints` 等更细原因；`routing_health` 汇总本轮需要 downrank/prioritize 的路径和 score 调整，帮助后续修 nav 或 map logic。
- `kb_scope_policy` 明确 project navigation 可基于当前任务证据直写，general/personal/wiki 默认走 manual candidate review；普通任务不把非项目级 wiki 当作自动直写目标。
- `maintenance_plan` 在旧 suggestions 兼容字段之上补充 `target_path`、`target_kind`、`signals`、`source_files`、`validation`、`write_policy` 和 `execution`：project navigation / reusable project solutions 可基于当前任务证据直写 approved Markdown；generated map cache 只能 refresh/status，不能手改；map logic gap 指向 `project_context_index.py`/`project_context_resolver.py` 等源码和测试。
- `GET /api/task/<task-id>/context-evidence` 可以读取最近 task-scoped evidence、latest result、routing health、KB scope policy、聚合后的 maintenance actions 和 maintenance plan；任务列表的 `Chat / Logs / Ctx / Evidence` 视图里已有只读 Context evidence 页面展示这些数据，移动端入口收在 `+` 操作面板里。
- 后续 turn 的 Context Pack 会在已有 task evidence 时追加 compact “Current task evidence recap”，包含最近 signals、actual/referenced files、map gap、stale path hints、routing health、KB scope policy、map query、maintenance plan 和 maintenance actions。这个 recap 是 task-local hints，不替代源文件验证；只有存在 evidence 时才把预算提高到 4000 chars 硬上限，避免关键 source-check 提醒被裁掉。
- agent-pull 入口契约本身不带具体 `map.files`；这种 entrypoint-only pack 即使本轮读取了源码，也不应被误判成 `missing_nav` 或 `missing_entry`。
- 真实 `/aha map query` 和 Web Knowledge Map query 结果会记录为 `project_map_query` evidence，并在同一 agent turn 的 prompt 之后发生时并入 `context_evidence_result`。

### Phase 3.1：EVD 面板产品定义

EVD 面板不是普通 debug dump，而是 **单个 token-saving task 的 KB/map 使用闭环观测中心**。它回答的是“这个 task 里，知识库和 project map 是否帮助 agent 更快完成任务，以及 agent 是否基于当前任务证据完成自增长/自修复”。

作用域：

- 只面向当前 task；未开启 token saving 的 task 不应展示复杂 KB 诊断。
- 数据随 task 多轮更新，展示整个 task 的累计状态，同时保留 turn-by-turn 证据时间线。
- 面板不是全局 KB 管理器，也不是 generated Project Map cache 编辑器。

应该优先展示的人类可读层级：

1. **固定状态摘要**：KB/map 当前对这个 task 是 helped、stale、needs repair、observing 还是 no evidence，同时展示下一步动作和最近 evidence 来源。
2. **Growth tab**：KB maintenance actions 和 KB growth state，回答“agent 已经/正在/必须执行哪些 KB 或 map 维护动作”。
3. **Feedback tab**：显式 `agent_kb_feedback`，回答 agent 认为 KB 是否帮上忙、哪里 stale/missed/updated/pending。
4. **Evidence tab**：signals、actions、actual files、referenced files，回答本轮证据事实。
5. **Diagnostics tab**：routing health、map diagnostics、map queries，保留原始诊断但不挤在主状态区。

当前数据来源分三类：

- `context_pack`：发给 agent 前由 AHA runtime 记录，说明本轮提供了哪些 KB/map 入口。
- `project_map_query`：agent 执行 `/aha map query` 或 Web Knowledge Map query 时记录，说明查询了什么、命中了什么、是否使用 navigation 扩展。
- `context_evidence_result`：agent 一轮结束后由 AHA runtime 根据 prompt metrics、map query events、命令路径、git dirty paths、reply excerpt 和 exit code 推断，生成 signals、routing health、maintenance plan 等。
- `agent_kb_feedback`：agent 在 `record_task_update` action 里附带 `kb_feedback` 时记录，用于表达 KB 是否帮上忙、哪里 stale/missed、已经 updated 什么、还有什么 pending。

注意：`context_evidence_result` 主要是 runtime-inferred evidence，不等同于 agent 的显式 KB 使用反馈；`agent_kb_feedback` 是当前第一版显式反馈入口。后续可继续把它从 task-update 扩展为更细粒度的每次 KB 使用反馈。

- `helped`: KB/nav/map 是否准确定位到代码。
- `stale`: 哪些路径、入口或说明过期。
- `missed`: KB/map 没有覆盖但 agent 已验证出的真实路径。
- `updated`: agent 已经更新了哪个 project navigation/project solution。
- `pending`: 仍建议后续 refresh/repair/manual review 的项目。

### Phase 3.2：EVD 降噪与回归

不用重做 Context Planner 主链路，目标是把 EVD 从“能看见原始 evidence”收敛成“能解释这个 token-saving task 的 KB/map 是否真的起作用”。

当前切片状态：

1. **历史 evidence 读侧降噪：已接入**

   写入侧已经会把 workspace source、KB/navigation 文件和命令噪声分开；读侧现在也会兼容清洗旧 `context_evidence.jsonl` 里的 `bin/bash`、`KB/map`、外部 KB 路径等历史噪声，不改历史 jsonl 原文：

   - `actual_files` 只展示 workspace 内真实源码、测试、文档。
   - `knowledge_files` 单独展示 KB/navigation 路径。
   - `ignored_command_paths` 或 raw diagnostic 可保留 shell/命令噪声，但默认不要进入主摘要。
   - `map_missing_files`、`routing_health.prioritize_paths`、maintenance files 等派生字段也会避免被历史噪声污染。

2. **任务级摘要优先：已接入，仍需真实任务观察**

   EVD 首屏应该先回答四个问题，而不是先展示 raw signals：

   - KB/map 对这个 task 是否帮上忙。
   - 下一步应该 repair nav、refresh map、write solution，还是无需动作。
   - 哪些 evidence 证明 KB/map 被采用或失配。
   - agent 已经写回、准备写回或仍待人工确认的 KB 自增长/自修复动作是什么。
   - raw signals、actual/referenced files、routing health、map diagnostics 和 map queries 默认进入 tabs 分区，避免多个短卡片挤在主状态区。

3. **KB growth hard loop：已接入**

   当 `maintenance_plan` 里出现 project navigation / project solution 写回需求时，turn-end `context_evidence_result` 会生成 `kb_growth_state`：

   - `pending`：本轮有 KB 成长需求，但没有看到对应 project navigation/solution 写回。
   - `applied`：通过 `agent_kb_feedback.updated` 或 dirty path 观察到对应写回。
   - `not_required`：本轮没有项目 KB 原文成长需求。

   EVD 首屏会把 pending growth 提升为 `KB growth pending`，后续 Context Pack recap 会继续携带 `kb_growth_state`，直到 agent 写回并通过 `agent_kb_feedback.updated` 或相关路径变更证明完成。

4. **EVD 面板 UI 降噪：已接入**

   `conversation_panel.js` 默认展示固定 task summary，下面用 `task-evidence-tabs` 切换 Growth / Feedback / Evidence / Diagnostics；tab panel 内部继续使用 `task-evidence-stack` 单列分组。`event_bindings.js` 处理 `data-context-evidence-tab` 的本地切换，不重新请求后端，并把当前 tab 存到 `window.__ahaContextEvidenceActiveTab`，自动刷新重渲染后继续停留在用户当前 tab。EVD 展示的 timestamp 要走 `localizeTimestampText` 转成本地时间。`styles.css` 对 EVD 容器、tabs、panels、chip、code/path 和 stack 使用 `min-width: 0`、`overflow-x: hidden`、`overflow-wrap: anywhere` 和 `white-space: normal`，避免长路径撑出左右滚轮。

5. **显式 agent KB feedback 回归：接口已接入，仍需真实任务验证**

   后端 agent 使用 KB/nav/map 后，若本轮返回 AHA `record_task_update` action，应附带可选 `kb_feedback`：

   ```json
   {
     "helped": ["navigation/flows/token-saving.md 定位到 context_evidence.py 和 task_routes.py"],
     "stale": ["旧 evidence 里仍有 bin/bash 噪声，需要读侧清洗"],
     "missed": [],
     "updated": ["docs/token-saving-context-planner.md"],
     "pending": ["真实 token-saving task 回归验证 EVD 摘要"]
   }
   ```

   EVD 应把这类 `agent_kb_feedback` 和 runtime-inferred `context_evidence_result` 分开展示，同时在 task-level summary 中合并解释。

6. **真实任务回归：下一步**

   新开一个开启 token saving/provider=map 的 task，验证完整链路：

   - 首轮 prompt 有 KB/Map Pull Contract。
   - agent 会先读 project navigation，再按需 `/aha map query` 或读源码。
   - map query 会记录 `project_map_query`。
   - turn 结束会记录 `context_evidence_result`。
   - `record_task_update.kb_feedback` 会记录 `agent_kb_feedback`。
   - EVD 面板随多轮任务更新，展示整个 task 的累计 KB 效果，而不是只展示首轮。
   - 旧噪声不会进入主摘要；诊断细节仍可追溯。

下一任务优先入口：

- `src/aha_cli/services/context_evidence.py`
- `src/aha_cli/services/context_evidence_growth.py`
- `src/aha_cli/services/context_evidence_maintenance.py`
- `src/aha_cli/services/context_evidence_paths.py`
- `src/aha_cli/services/context_planner.py`
- `src/aha_cli/services/task_updates.py`
- `src/aha_cli/web/task_routes.py`
- `src/aha_cli/web/static/conversation_panel.js`
- `src/aha_cli/web/static/i18n.js`
- `tests/test_context_evidence.py`
- `tests/test_chat_prompt.py`
- `tests/test_task_updates.py`
- `tests/test_web_task_routes.py`
- `tests/test_frontend_static.py`

建议验证命令：

```bash
python3 -m pytest tests/test_context_evidence.py tests/test_task_updates.py tests/test_web_task_routes.py tests/test_frontend_static.py -q
python3 -m pytest tests/test_chat_prompt.py -k 'context_pack or context_evidence' -q
python3 -m pytest -q
git diff --check
node --check src/aha_cli/web/static/conversation_panel.js
node --check src/aha_cli/web/static/i18n.js
```

### Phase 4：生命周期 reset/compact

- 判断需求完成。
- 生成 compact task summary。
- 在无关新需求前自动 reset/compact。
- 新需求只从 Context Planner + durable task state 启动。

当前实现切片：

- Web 侧已丢弃 `/aha final` / finalization 作为常规完成路径，因此它不作为 token-saving lifecycle 触发点。
- 自动 reset/compact 暂不推进，避免误判新旧需求边界并破坏 sticky session 连续性。
- reset/compact 保持手动，由用户自己决定何时执行；现有手动入口是 `POST /api/task/<task-id>/session/compact-reset` / UI compact-reset 动作。

验收：

- AHA 不把旧需求历史带入新需求。
- 同一需求 follow-up 保留足够连续性。
- reset/compact 行为可解释、可回退。

## 13. 风险

- reset 太激进会丢失 follow-up 所需上下文。
- 检索不准会让 agent 缺上下文，反而增加搜索。
- 直接维护知识库可能固化错误结论。
- Context Pack 本身如果不控预算，也会变成新的大 prompt。
- 仅靠模型措辞判断需求完成不可靠。

缓解：

- 初期 manual curation + 显式 reset/compact。
- Context Pack 严格预算。
- 用 observer 验证真实请求大小。
- 自动完成必须结合验证状态、task 状态、工具状态。
- session rotate 前先保存 durable task summary。

## 14. 开放问题

- map gap 信号如何进一步精确归因：哪些是 stale cache，哪些是 extractor/schema 缺口，哪些是 query expansion/nav hint/ranking 问题。
- 是否存在足够可靠的新旧需求边界信号，可以在未来重新考虑自动 reset。
- 新需求开始时 reset 是否默认执行，还是先询问用户。
- 多条 nav/map/entry 冲突时如何做跨任务长期排序和降权。
- stale 知识的跨任务统计、展示和自动 repair/deprecate 工作流如何收敛。
- 如果未来重新引入 AHA 内部自然语言检索，使用什么组合：关键词、结构化字段、向量、历史命中反馈。
- 非项目级知识自修复何时允许直写，何时必须走 `/aha kb` 候选链路。
- EVD 历史 jsonl 已先做读侧兼容清洗、不改历史原文；未来是否还需要一次性迁移/重写工具仍待观察。

## 15. 进展日志

- 2026-07-05：初版设计文档创建，覆盖 Context Planner、nav/map/entry、知识更新、observer 度量和 reset/compact 生命周期。
- 2026-07-05：根据讨论更新为中文文档，并把“准确知识库”列为核心难点：知识库必须具备自增长、自修复，以及从自然语言精准定位 nav/map/entry 的能力。
- 2026-07-05：确认产品边界：用户只负责自然语言需求，不负责检索知识库。后续修正为 agent-pull 主路径：AHA 提供入口和规则，agent 判断语义相关性。
- 2026-07-05：修正 map 设计基线：当前 AHA 已有通用 Project Context Index 和 `/aha map status|refresh|query`。本文档后续讨论基于现有 generated project map；人工维护的流程关系优先写入 `navigation/flows/*.md`，不在第一版新增独立 KB map 类型。
- 2026-07-05：补齐当前 AHA 知识库实现基线：长期 KB 存储模型、task final/`/aha kb`/`/aha nav`/capture/project-nav bootstrap/project-map refresh 等生产者，task prompt、sticky delta、Project Map capability、`/aha map`/Web UI 等消费者，以及 pending/approve/manual gate 的审核写入路径。
- 2026-07-05：确认 Context Planner MVP 设计：token saving provider=map 的普通用户消息经过 planner；Pull Contract 预算目标 1200-2500 chars、硬上限 4000 chars；普通任务 evidence 不自动生成 pending；map 只作为代码定位器，不写长期 KB。
- 2026-07-05：完成 Context Planner MVP 第一刀：接入 full prompt 和 sticky delta prompt；只注入 KB/Map Pull Contract，不自动注入关键词 KB 命中或 map query 结果；只读取已有 map cache，不自动 refresh。
- 2026-07-05：纠正知识生产主链路：`task final` 不再作为新设计主生产者，只能视为历史兼容/补充入口。自动观测驱动的自增长、自修复是 token saving 方案核心；手动 `/aha nav`、`/aha kb`、capture 只作为辅助。
- 2026-07-05：明确自增长/自修复的实现边界：只对当前任务证据做增量 CRUD，由 agent 直接维护 project navigation/project solutions 原文；不扫描或重建完整知识库，也不自动删除长期 KB 文件。
- 2026-07-05：根据 task-151 首轮 prompt 问题修正设计：AHA 关键词检索不再作为自然语言到 KB 的主链路；Context Pack 改为 KB/Map Pull Contract，只注入入口说明、使用方法、信任优先级和当前任务级 evidence 协议，语义相关性由 agent 主动判断。
- 2026-07-05：根据用户确认修正写入策略：普通任务 evidence 不进入 pending candidate；项目 navigation/solutions/map 使用权交给 agent，其中 generated map 只能 query/refresh，稳定路线写回 navigation。手动 `/aha kb`、`/aha nav`、capture/bootstrap 才是候选审核路径。
- 2026-07-05：细化 map 自增长/自修复边界：generated map cache 不能手改，但 extractor、schema、resolver、query expansion、ranking、refresh 逻辑必须能被当前任务 evidence 驱动修复；新增 map gap 信号用于区分 stale cache、extractor gap、query expansion gap、ranking gap 和 coverage gap。
- 2026-07-05：同步当前实现状态：Phase 2 第一刀已接入 `context_planner.py`/`backend_context_pack.md`/sticky delta；Phase 3 已有 `context_evidence.py` 的记录和信号推断，但自动 KB 原文维护、stale 展示/降权和 Phase 4 生命周期 reset/compact 仍待推进。
- 2026-07-05：收紧 Phase 3 signal 判断：Context Pack 只有 KB/map 入口、没有具体 referenced files 时，本轮实际读源码不再自动产生 `missing_nav`/`missing_entry` 假阳性；只有实际 map query observed 或 navigation index 明确缺失等场景才触发 missing-nav 类信号。
- 2026-07-05：接入 `/aha map query` evidence：slash query 成功后记录紧凑 `project_map_query`，并在同一 agent turn 的 prompt 之后发生时合并进 `context_evidence_result`，用于 `context_hit_ok`、`map_miss` 和 map gap 判断。
- 2026-07-05：接入 Web Knowledge Map query evidence：`/api/kb/project-context-index/query` 在能由 `run_id`/`task_id` 或唯一 workspace task 关联到 task 时记录 `project_map_query`；前端打开已有 map 查询时也随 payload 传递 `run_id`。
- 2026-07-05：补充 maintenance actions：context evidence 会把 miss/stale/wrong/missing 信号转成 agent-owned create/update/repair/refresh/deprecate 动作，用于驱动 agent 窄范围维护 project navigation、project solutions 或 map 生成/查询逻辑；普通任务仍不生成 pending candidate。
- 2026-07-05：补充 task-scoped evidence 只读 API：`GET /api/task/<task-id>/context-evidence` 返回最近 evidence、latest context result 和聚合后的 maintenance actions，方便后续 UI 展示和调试。
- 2026-07-05：接入 Web 只读展示：任务列表 view switcher 新增 Evidence 视图，移动端 `+` 操作面板新增 Evidence 入口，展示 signals、crud actions、actual/referenced files、maintenance actions、map diagnostics 和最近 map query；面板只调用只读 API，不触发 KB 写入。
- 2026-07-05：Context Planner 后续 turn 接入 compact task evidence recap：当 task 已有 context evidence 时，在 KB/Map Pull Contract 中携带最近 signals/files/map query/actions，减少重复定位；该 recap 仅作 task-local hints，仍要求重新验证当前源文件。
- 2026-07-05：补齐结构化 `maintenance_plan`：turn 后 evidence 会把泛化 actions 扩展为可执行计划，包含目标 KB/source/cache 路径、写入策略、来源文件、触发信号和验证命令；API、Evidence 面板和后续 Context Pack recap 都会优先暴露该计划；generated map cache 仍只能 refresh/status，不能手改。
- 2026-07-05：根据 Web 已丢弃 `/aha final` 的现状，撤回 finalization lifecycle advice；Phase 4 暂时保持手动 compact/reset，由用户决定何时清 backend session。
- 2026-07-05：补齐剩余 Phase 3 闭环：resolver 输出 stale path hints 并从正向 hint 剔除，map ranking 对 stale hints 降权，context evidence 增加 `gap_reasons`、`routing_health`、`kb_scope_policy` 和 maintenance plan `execution`，API/UI/后续 Context Pack recap 同步展示；evidence recap 预算提升到 4000 硬上限以保留 source-check 边界。
- 2026-07-06：明确 EVD 面板产品定义：它是单 token-saving task 的 KB/map 使用闭环观测中心，应默认展示任务级状态、下一步动作、KB 效果证据和自增长/自修复状态；当前实现以 runtime-inferred evidence 为主，后续补结构化 agent KB feedback。
- 2026-07-06：EVD 第二刀开始落地：命令路径会区分 workspace source、KB/navigation 文件和 shell/命令噪声，避免 `bin/bash`、外部 KB 文件污染 `actual_files`/`map_missing_files`；`record_task_update` 支持可选 `kb_feedback`，记录为 `agent_kb_feedback` 并在 EVD summary/UI 中展示。
- 2026-07-06：补充下一任务接手说明：后续重点是 EVD 历史 evidence 读侧降噪、任务级摘要优先、显式 `agent_kb_feedback` 回归，以及新开 token-saving task 做端到端验证；自动 reset/compact 继续保持手动。
- 2026-07-06：EVD 历史 evidence 读侧降噪接入：`list_task_context_evidence()` 返回清洗后的展示视图，旧 jsonl 原文保持不可变；旧 `actual_files`/`map_missing_files`/`routing_health.prioritize_paths`/maintenance files 里的 KB 路径和 shell/ad-hoc 噪声会移到 `knowledge_files` 或 `ignored_command_paths`，Context Pack recap 同步受益。
- 2026-07-06：接入 KB growth hard loop：`context_evidence_result` 增加 `kb_growth_state`，对 project navigation/project solution 写回需求标记 pending/applied/not_required；EVD summary/UI 会将 pending 显示为 `KB growth pending`，后续 Context Pack recap 会继续携带 pending growth，直到 `agent_kb_feedback.updated` 或路径变更证明写回完成。
- 2026-07-06：EVD 面板首屏降噪和无横向滚动接入：主界面改为固定 task summary + Growth / Feedback / Evidence / Diagnostics tabs；每个 tab panel 内部用单列 stack 展示，长路径、chip 和 code 在面板内换行，不再撑出左右滚轮。
- 2026-07-06：修复 EVD 自动刷新重置 tab：tab 点击会写入 `window.__ahaContextEvidenceActiveTab`，`renderContextEvidenceTabs()` 重渲染时优先恢复该 tab，避免自动刷新回到 Growth。
- 2026-07-06：EVD Web 时间显示改成本地时间：summary 的 latest update 和 EVD list 中的 ISO timestamp 都通过 `localizeTimestampText` 渲染。
