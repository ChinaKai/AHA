# AHA 知识库重构：Obsidian 结构 + 自动沉淀闭环 + Web 星球图

> 状态: **设计文档（待评审）**。目标是把 AHA 知识库从「默认关闭、人工维护、线性条目」升级为「默认必填、自动沉淀、网状知识 + 图可视化」的正向循环系统。
>
> 设计原则：**任务越多 → 知识库越丰富准确；知识库越丰富 → 任务越轻松 → 数字人分担越多 → 人越轻松。**

---

## 0. 现状问题（重构动机）

### 0.1 已有基础（代码已实现，文档须基于此升级而非从零设计）

调研代码库与现有 KB，以下能力**已存在**，重构是「升级增强」而非「新建」：

| 已有能力 | 位置 | 说明 |
|---|---|---|
| KB 是 git 仓库 + 远端同步 | `knowledge_git.py` | `auto_commit_after_change`/`auto_pull_before_task`/`auto_push` 已实现，默认关 |
| git 冲突安全 | `knowledge_git.py:422` | rebase 冲突自动 `--abort`，repo 不残留中间态 |
| `/aha kb` CLI 全家桶 | `cli_parser.py:252` | init/status/map/pending/list/show/search/approve/reject/add/capture/distill 全有 |
| agent 直接写 KB 候选 | `knowledge_command.md` + `knowledge_sidecar.py` | `/aha kb add --pending` + `<aha_knowledge_candidates>` sidecar 提取 |
| `kb_feedback` 自动 commit | `orchestrator.py:97` | agent 的 `record_task_update.kb_feedback.updated` 自动 commit |
| capture → distill → pending → approve | `knowledge_capture*.py` + `knowledge_distill.py` | note → agent 蒸馏 → pending → 审批全链路 |
| 过期复核 | `knowledge.py:606` `list_stale_entries` | `review_after` 到期条目需复查 |
| 图片/SVG 资产 | `knowledge_assets.py` + `knowledge_capture.py` | `assets/<entry-slug>/`、SVG 支持、capture 图片 |
| project navigation 生成 | `knowledge_navigation.py` | `scan_workspace` + agent bootstrap 候选 |
| 检索注入 | `knowledge_retrieval.py` | 词项评分（含中文 bigram）+ nav index 注入 |
| Web 端 | `knowledge.html` + `knowledge_routes.py` | entries 树/nav/pending/capture/skills/settings/sync 全 tab |

### 0.2 核心差距（重构要解决的）

| # | 差距 | 现状 |
|---|---|---|
| P1 | **默认关闭** | `default_knowledge_config.enabled = False`，用户不手动开就没有知识库 |
| P2 | **沉淀靠人工** | `curation.gate = "manual"`，agent 产出的候选全部进 pending，需人工审批 |
| P3 | **同步靠手动** | git `auto_commit/auto_push/auto_pull = False`，知识不跨设备流动 |
| P4 | **线性条目，无网络** | 无 `[[双链]]`/标签/反链，检索是关键词评分，知识不形成网络 |
| P5 | **Web 无图可视化** | KB 页只有 entries 树/导航/pending/capture 列表，没有 Obsidian 式图视图 |
| P6 | **蒸馏需手动触发** | capture 后要手动点 distill，任务结束不自动沉淀 |
| P7 | **nav 缺知识层** | nav 是代码路由，不承载决策/踩坑/设计沉淀 |
| P8 | **skill 无系统/个人分层** | 无 `/skill` 命令，无系统内置 vs 用户个人区分 |

| # | 问题 | 现状 |
|---|---|---|
| P1 | **默认关闭** | `default_knowledge_config.enabled = False`，用户不手动开就没有知识库 |
| P2 | **沉淀靠人工** | `curation.gate = "manual"`，agent 产出的候选全部进 pending，需人工审批 |
| P3 | **同步靠手动** | git `auto_commit/auto_push/auto_pull = False`，知识不跨设备流动 |
| P4 | **线性条目，无网络** | 无 `[[双链]]`/标签/反链，检索是关键词评分，知识不形成网络 |
| P5 | **Web 无图可视化** | KB 页只有 entries 树/导航/pending/capture 列表，没有 Obsidian 式图视图 |
| P6 | **蒸馏需手动触发** | capture 后要手动点 distill，任务结束不自动沉淀 |

---

## 1. 目标架构

```
┌─────────────────────────────────────────────────────────┐
│                  正向循环（核心）                          │
│                                                         │
│  任务开始 ──► KB 自动注入相关笔记（双链传播）              │
│    │                                                    │
│    ▼                                                    │
│  agent 执行 ──► 产生 final / round summary / kb_feedback  │
│    │                                                    │
│    ▼                                                    │
│  自动蒸馏 ──► 质量门控 ──► 写入 KB（双链 + 标签）          │
│    │                                                    │
│    ▼                                                    │
│  git 自动 commit + push ──► 多设备共享 ──► 知识网络生长   │
│                                                         │
│  知识越丰富 ──► 检索命中率越高 ──► 任务越轻松 ──► 人越轻松  │
└─────────────────────────────────────────────────────────┘
```

三层：
1. **存储层**：Obsidian 结构 git 仓库（原子笔记 + MOC + 双链 + 标签）
2. **沉淀层**：任务结束自动 distill + 质量门控 + 自动 git
3. **展示层**：Web 端新增「星球图」（力导向图）+ 双链/反链/标签检索

---

## 1.5 Project Navigation：一个具体项目的知识库（核心组成部分）

### 1.5.1 现状与差距

当前 `navigation/` 已是一个项目知识库的雏形（AHA 项目 31 篇 modules/flows，fw-omni-builder 8 篇）：
- `navigation/index.md`：项目入口路由（项目介绍/编译使用/注意事项/编码规范/核心 Nav）
- `navigation/modules/*.md`：模块职责 + 关键文件 + 入口点 + 排查提示
- `navigation/flows/*.md`：跨模块流程
- 生成方式：`scan_workspace` 扫代码结构 + agent 按 `knowledge_navigation_bootstrap.md` 生成候选

**差距**：nav 目前定位是「代码结构路由」——回答"这个项目怎么走、去哪看代码"。它**不承载**项目的**知识沉淀**（架构决策、踩坑记录、组件设计权衡、历史结论）。这些散落在 `solutions/`（大多为空）、`worklog/`（任务流水）、agent 上下文里，任务做完就丢了。

### 1.5.2 目标：nav = 项目的「活的百科全书」

把 nav 从"代码路由"升级为"项目知识库入口"，四个层次：

| 层次 | 内容 | 当前 | 目标 |
|---|---|---|---|
| **路由层** | 代码结构、模块职责、入口点、编译使用 | ✅ 已有 | 保持 |
| **知识层** | 架构决策、设计权衡、踩坑记录、组件关系 | ❌ 缺 | **新增**：任务结论自动蒸馏进 nav 的知识小节 |
| **经验层** | 可复用排障、历史解决方案 | ❌ solutions 大多空 | 任务 final 自动提炼进 solutions，nav 链接 |
| **流水层** | 任务执行记录、时间线 | ✅ worklog 有 | 保持（worklog 是原始流水，nav 是提炼后知识） |

### 1.5.3 新增：nav 的「知识」承载方式

在现有 modules/flows 之外，每个项目 nav 新增 **`knowledge/` 子目录**（或并入模块文档的知识小节）：

```
projects/<key>/navigation/
├── index.md                    # 路由（保持）
├── modules/*.md                # 模块职责（保持）
├── flows/*.md                  # 跨模块流程（保持）
└── knowledge/                  # ★ 新增：项目知识沉淀区
    ├── decisions.md            #   架构决策记录（ADR）
    ├── pitfalls.md             #   踩坑记录（带复现+修复）
    ├── components.md           #   组件设计权衡
    └── <topic>.md              #   按主题的深度知识
```

**自动沉淀**：任务结束时，蒸馏出的"项目级结论"（架构决策、踩坑、设计权衡）自动进 `knowledge/<topic>.md` 并挂到 index 的知识区 + MOC。这样 nav 既是路由又是知识库。

### 1.5.4 nav / solutions / worklog / MOC 的职责边界

| 区域 | 职责 | 生命周期 |
|---|---|---|
| `navigation/` | **项目怎么走**（路由）+ **项目有什么知识**（沉淀）| 长期，随项目演进 |
| `navigation/knowledge/` | 项目级**决策/踩坑/设计**（提炼后） | 长期，随任务累积 |
| `solutions/` | **可复用排障**（跨任务的 how-to） | 长期，按问题检索 |
| `worklog/` | 任务**执行流水**（原始记录） | 任务即写 |
| `MOC/<project>.md` | 项目知识**图入口**（Obsidian 导航中心） | 自动生成 |

关系：**worklog 是流水，solutions 是可复用结论，knowledge 是项目级深度知识，nav 是路由 + 知识入口，MOC 是图视图入口**。任务沉淀时：可复用 → solutions，项目级 → nav/knowledge，流水 → worklog，全部由 MOC 和 nav index 汇聚。

### 1.5.5 nav 与星球图

nav 的 modules/flows/knowledge 条目是星球图的**项目子图**：
- `navigation/index.md` → 项目中心节点（MOC）
- `navigation/modules/*` → 模块节点
- `navigation/flows/*` → 流程节点（跨模块连线）
- `navigation/knowledge/*` → 知识节点（连到相关模块 + solutions）
- 项目切换 → 星球图聚焦该项目子图（`?project_key=`）

这样：**nav 不再只是列表，而是项目知识的可视化星球**。

---

## 1.6 Skills：系统技能 + 个人技能（特定任务的补充）

### 1.6.1 现状

skill 已是 KB 的一部分（`knowledge/skills/<id>/SKILL.md` + agents/references/scripts），任务通过 `task_skills` 启用并注入 prompt。但有两个缺口：
- **没有系统/个人区分**：AHA 内置 skill 与用户添加的 skill 混在同一个 `knowledge/skills/`，来源标记只有 `knowledge`/`aha_home`
- **没有 `/skill` CLI 命令**：创建 skill 只能走 Web 端 New skill，agent 无法直接创建

### 1.6.2 目标：系统技能 + 个人技能 双层

| 层 | 内容 | 位置 | 谁维护 |
|---|---|---|---|
| **系统技能（AHA 内置）** | AHA 自带的能力（硬件调试、浏览器、本地升级等） | 打包进 onebin（`src/aha_cli/.../skills/`）→ 首次运行复制到 `knowledge/skills/` | AHA 升级自动更新 |
| **个人技能（用户添加）** | 用户/agent 创建的特定任务能力 | `knowledge/skills/<id>/` | 用户 + agent 共创 |

区分实现：
- 系统技能 frontmatter 加 `"source": "system"`；个人技能 `"source": "personal"`
- 系统技能在 AHA 升级时**自动覆盖更新**（AHA 维护），个人技能**从不被覆盖**（用户资产）
- 目录可分开：`knowledge/skills/system/` + `knowledge/skills/personal/`（或同一目录用 frontmatter source 区分）
- Web 端 skill tab 分区展示：系统技能只读 + 个人技能可编辑/删除

### 1.6.3 `/skill` 命令：创建 AHA 技能

新增 CLI 命令 `/skill`（用户在 Web 输入框或 chat 中触发）：

**行为**：当用户输入 `/skill` 时，AHA 自动在用户消息前注入「技能创建说明提示词」，引导 agent 生成一个完整技能：

```
/skill                                    → 注入技能创建向导，agent 引导用户描述技能
/skill <name> <description>               → 直接创建（agent 自动补全 SKILL.md 结构）
/skill list                               → 列出系统 + 个人技能
/skill edit <id>                          → 编辑个人技能
/skill disable/enable <id>               → 启停（任务注入控制）
```

**注入的「技能创建说明提示词」**（`prompts/skill_creation_guide.md`）：
- 目标技能解决什么问题、适用场景
- SKILL.md 结构：`name` / `description` / 工作流程 / 边界与约束
- 是否需要 `scripts/`（shell/python 脚本）、`references/`（参考文档）、`agents/`（openai.yaml）
- 命名规范、如何与现有技能避免重复
- 创建后如何绑定到任务（`task_skills`）

**创建流程**（agent 执行）：
1. 解析用户意图 → 生成 SKILL.md + 可选 scripts/references
2. `save_managed_skill` 写入 `knowledge/skills/personal/<id>/`
3. git 自动 commit（技能也是知识，进 KB 仓库）
4. Web 端 skill tab 立即可见，可编辑

### 1.6.4 KB 里如何展示 skill

skill 在 KB 星球图中的角色：
- **技能节点**：每个 skill 是一个节点，连到它的 `references/` 里的知识条目 + 它服务的项目/模块
- **标签**：`#skill #system|#personal` 区分来源
- **入口**：`MOC/skills.md`（技能总览，自动生成）聚合所有技能

星球图交互：
- 系统技能节点 = 固定图标（⚙），个人技能 = 用户图标（🛠），颜色区分
- 点击技能节点 → 显示 SKILL.md 内容 + 引用文档 + 绑定任务
- 按来源过滤：只看系统 / 只看个人

### 1.6.5 与任务的绑定

- 任务 `task_skills` 已支持选择技能；增强为可从星球图拖拽技能到任务
- 任务注入时：技能 SKILL.md + 其 references 进 prompt（现有 `task_skills_context_for_prompt`）
- 技能也可被蒸馏：任务中验证好用的技能 → 提升为系统技能候选（进 pending，人工批准）

---

## 1.7 附件支持：PDF / Word / TXT / MP4 等 + 附件内图片展示

### 1.7.1 现状与差距

现有资产能力（`knowledge_assets.py` + `knowledge_capture.py`）**只支持图片**：
- entry 图片：png/jpeg/svg/webp，单张 5MB / 总量 20MB，`assets/<entry-slug>/`
- capture 图片：`capture/assets/<note-id>/`
- 路由 `/api/kb/entry/image` + `/api/kb/capture/image` 仅服务图片

**差距**：KB 需要存放 **PDF / Word(.docx) / TXT / Markdown / MP4 / 其他二进制** 附件；且附件**内部的图片**（如 PDF 里的截图、Word 里的插图）要在 Web 端展示。

### 1.7.2 通用附件存储

**目录**：每个 entry 附件的统一目录（扩展现有 `assets/` 语义）：
```
projects/<key>/solutions/<slug>.md
└── assets/<entry-slug>/
    ├── spec.pdf                # PDF 附件
    ├── datasheet.docx          # Word 附件
    ├── notes.txt               # 文本附件
    ├── demo.mp4                # 视频附件
    └── diagram.png             # 正文引用的图片（现有）
```

**frontmatter** 扩展 `assets` 元数据，统一记录所有附件（不再只图片）：
```json
"assets": [
  { "path": "assets/<slug>/spec.pdf", "name": "spec.pdf",
    "original": "原始文件名.pdf", "mime": "application/pdf", "size": 123456, "kind": "file" },
  { "path": "assets/<slug>/diagram.png", "name": "diagram.png",
    "original": "图1.png", "mime": "image/png", "size": 45678, "kind": "image" }
]
```
- `kind: image`（图片，Web 内联展示）/ `kind: file`（其他附件，下载/预览）
- 新增 `kind: video`（mp4，Web 播放）

**允许的附件类型**（白名单，防恶意）：
| 类别 | mime 白名单 | Web 展示 |
|---|---|---|
| image | png/jpeg/svg/webp/gif | 内联 `<img>` |
| pdf | application/pdf | 内嵌 PDF 预览（`<embed>`/iframe） |
| text | txt/md/csv/log/json | 纯文本渲染（`<pre>`） |
| office | docx/xlsx/pptx | 下载 + 提示用外部查看器（Web 无法原生渲染） |
| video | mp4/webm | `<video>` 播放 |
| 其他 | 白名单外拒绝 | — |

**大小**：单附件上限可配（默认 20MB）；git 仓库对大附件不友好，可选**大附件走独立存储**（如 `~/.aha/knowledge-assets/` 软链或 LFS 风格），KB 仓库只存引用 + 校验和。

### 1.7.3 附件内图片的 Web 展示

PDF/Word 等**容器文档内部的图片**，Web 端展示方案：

1. **PDF**：Web 用 `<embed src="/api/kb/attachment?id=..&path=..">` 内嵌浏览器原生 PDF 查看器（Chrome/Edge 支持）——PDF 内图片自然可见，无需额外提取。
2. **Word/PPT**：
   - **提取插图**（推荐）：入库时（agent/CLI 上传时）用脚本从 docx/pptx 提取内嵌图片 → 存为 `assets/<slug>/extracted/<n>.png` → 正文自动插入图片引用 + 标注来源页。
   - **或**：提供下载，用户用本机 Office 打开。
3. **TXT/MD**：无内嵌图片，正文直接渲染。
4. **视频**：`<video>` 标签播放，首帧截图作为封面（提取 `poster`）。

**提取插图的工具**（零重依赖约束）：
- docx/pptx 本质是 zip：用 `zipfile` 标准库读 `word/media/*`、`ppt/media/*` → 得到图片字节 → 存 extracted/。**无需第三方库**。
- 图片转码（webp→png）如需要，调用已有工具（无则保持原格式）。

### 1.7.4 Web 端展示

**条目详情弹窗**（`kb-detail-fullscreen-modal`）新增「附件区」：
```
┌─ 附件 ────────────────────────────┐
│ [📄 spec.pdf] [📄 datasheet.docx] │  ← 图片内联、pdf 内嵌、
│ [▶ demo.mp4]                      │     docx 下载、mp4 播放
│   diagram.png  ──► 内联展示       │
└──────────────────────────────────┘
```

- **图片附件**：内联 `<img>`（现有）
- **PDF 附件**：`<embed>` 内嵌查看器（可滚动、可放大），或缩略图 + 点击全屏
- **文本附件**：`<pre>` 渲染，大文件截断 + 展开
- **Office 附件**：下载按钮 + 文件卡片（名称/大小/原始名），提示外部查看
- **视频附件**：`<video controls>` 播放 + 封面
- **附件列表**：所有附件按 kind 分组的卡片网格

**路由**：新增 `GET /api/kb/attachment`（替代/扩展 `/api/kb/entry/image`），按 `mime` 决定 Content-Type + 是否内联展示：
```
GET /api/kb/attachment?id=<slug>&path=assets/<slug>/spec.pdf
→ 200 application/pdf（内联） / image/png（内联） / application/octet-stream（下载）
```

### 1.7.5 附件在星球图中

- 附件本身不是节点；但**含附件的 entry 显示附件计数角标**（📎 3）
- 视频/图片类附件多 → 节点可标记为"富媒体"
- 检索时附件**文件名/原始名**作为搜索词（`spec.pdf` → 命中"spec"）

### 1.7.6 与 capture 的关系

- capture note 可带任意附件（`capture/assets/<note-id>/`），蒸馏成候选时**附件随候选带出**，approved 后落位到 entry 的 `assets/`
- 附件内图片提取（docx/pptx）在 distill 或 approved 时执行

---

## 1.8 用户协作 + 智能同步：定时/主动同步 + agent KB 维护任务

### 1.8.1 现状与差距

- KB 是**双写**系统：AHA/agent 自动沉淀 + **用户手动编辑**（在 Web 或本地 Obsidian/git 里改）
- 当前同步：`sync_status` 识别 `dirty/ahead/behind/diverged`；`pull` 用 rebase，**冲突时 `--abort` 干净回滚，只报告冲突、不解决**（`knowledge_git.py:449`）
- `diverged`（本地+远端都有提交导致 rebase 冲突）时：**无自动解决能力**，卡在等人工

**差距**：同步靠手动触发、冲突无自动解决、用户和 agent 共同编辑时的合并缺乏语义处理。

### 1.8.2 目标：定时同步 + 主动同步 + agent KB 维护任务

**三种同步模式**：

| 模式 | 触发 | 行为 |
|---|---|---|
| **定时同步** | AHA 服务定时器（可配间隔，如每小时）| 自动 pull + 自动 push + 冲突检测 |
| **主动同步** | 用户点「Sync now」/ `/aha kb sync` | 立即同步 + 报告结果 |
| **事件同步** | 任务开始前 pull + 每次沉淀后 commit/push | 已部分实现（`auto_pull_before_task`） |

**同步状态机**（扩展现有 `sync_status`）：
```
clean ──► dirty/ahead/behind ──► 自动 pull+push ──► clean
   └────► diverged（冲突）──► 触发 agent 维护任务 ──► 解决 ──► clean
```

### 1.8.3 agent KB 维护任务（冲突解决核心）

当同步进入 `diverged`/冲突状态，**调度一个真实 agent 子任务**（类似 sub-agent，但 scope 是 KB 维护）：

```
[冲突检测] ──► [spawn KB 维护 agent] ──► agent 分析冲突 ──► 语义解决 ──► 提交
```

**agent 的维护任务内容**：
1. **分析冲突**：读 `git status` + 冲突文件（两版本 + 共同祖先）
2. **语义解决**（agent 能力核心）：
   - 同主题两版更新 → **合并**（保留双方有价值内容，frontmatter 去重）
   - 一方删除/移动 → 判断是删除还是重命名
   - 用户手动改 vs agent 沉淀冲突 → **用户优先**（用户是 KB 所有者）
   - 无法判断 → 保留两版进 `conflicts/` 待人工，KB 主体用可合并部分
3. **提交解决结果**：`git add + commit "chore(knowledge): resolve sync conflict (agent)"`
4. **汇报**：给用户一条消息，说明冲突内容 + 解决策略 + 被保留/合并/待人工的部分

**关键设计：用户优先原则**
- 冲突双方：用户手写内容 > agent 自动沉淀内容（用户是 KB 所有者）
- agent 沉淀版本进 `git reflog`/历史，不丢失
- 用户可随时否决 agent 的解决（revert）

### 1.8.4 定时同步的实现

- **调度器**：AHA Web 服务加一个轻量定时器（`managed-process` 或服务内 `asyncio` 任务），按 `knowledge.sync.interval_minutes`（默认 60）跑同步
- **幂等**：同一时间只允许一个同步/维护任务在跑（锁）
- **失败隔离**：远端不可达 → 记录状态，下次再试，不阻塞任务（现有 `pull` 已失败隔离）
- **静默 vs 通知**：干净同步静默；冲突/维护完成才通知用户

### 1.8.5 用户手动编辑的入口

- **Web 端**：现有 entries 编辑弹窗（用户可改正文/frontmatter/附件）
- **本地 Obsidian**：用户可用 Obsidian 打开 KB 仓库直接编辑（Obsidian 结构天然支持）→ 保存后 AHA 检测 `dirty` → 定时/主动同步推上去
- **CLI**：`aha kb edit <id>` / 直接改文件

### 1.8.6 配置

```json
"knowledge": {
  "sync": {
    "interval_minutes": 60,
    "mode": "auto",              // auto=定时+事件, manual=仅主动
    "resolve_conflicts": "agent", // agent=自动解决, manual=只报告等人工
    "user_priority": true        // 冲突时用户内容优先
  }
}
```

### 1.8.7 与「正向循环」的关系

定时/主动同步让**多设备 + 用户 + agent 三方**的知识持续汇聚：
- 用户在某台设备 Obsidian 里写了一条经验 → 定时同步推远端 → 其他设备 agent 任务时注入
- 设备 A 的 agent 沉淀 + 设备 B 的用户编辑 → 同步时冲突 → agent 维护任务合并 → 知识不丢
- 冲突被 agent 语义解决 → 用户无需手动 git 操作 → 知识库真正"活"起来

---

## 2. 存储层：Obsidian 结构 git 仓库

### 2.1 目录布局（向后兼容 + Obsidian 化）

现有 `general/personal/projects/<key>/...` 保留（兼容已沉淀的 180+ 条目），新增 Obsidian 化能力：

```
~/.aha/knowledge/                     # git 仓库根（已是）
├── .aha/                             # AHA 私有元数据（不进 git）
├── MOC/                              # ★ 新增：Map of Content 入口层（Obsidian 导航中心）
│   ├── general.md                    #   通用知识 MOC
│   ├── personal.md
│   └── projects/<project-key>.md     #   每个项目一个 MOC
├── zettelkasten/                     # ★ 新增：原子笔记（无目录，靠双链组织）
│   └── <slug>.md                     #   如 cross-os-liveness.md
├── general/{wiki,solutions}/...      # 保留（兼容）
├── projects/<key>/{navigation,solutions,worklog}/...  # 保留
├── skills/                           # 保留
└── README.md                         # 自动生成：MOC 树 + 最近更新 + 统计
```

**为什么保留旧结构 + 新增 zettelkasten/MOC**：已沉淀的知识不动；新知识优先进 `zettelkasten/`（原子化）并通过 `[[双链]]` 挂到 MOC，避免大目录树难以浏览。

### 2.2 双链语法（Obsidian 兼容）

- 笔记正文用 `[[slug]]` 或 `[[标题]]` 互链（现有 entry slug 是稳定 key，直接可作链接目标）
- 链接解析：`[[X]]` → 匹配 `slug==X` 或 `title==X` 的 entry，生成双向 link
- **反链**：自动索引「谁链接了我」，检索时反链笔记可被召回
- **标签**：frontmatter `tags` 已支持；新增正文 `#tag` 行内标签解析，统一进 `tags`

### 2.3 frontmatter（JSON 块，保持 AHA 零依赖约束）

```json
{
  "type": "wiki",
  "scope": "project",
  "project_key": "aha-git-4117b370ee54",
  "slug": "cross-os-liveness",
  "title": "跨 OS 后端存活判断",
  "tags": ["backend", "wsl", "bug", "fix"],
  "confidence": 0.9,
  "created_at": "...",
  "updated_at": "...",
  "links": ["wsl-backend", "bridge-heartbeat"],
  "backlinks": [],
  "sources": ["task-004", "run-..."]
}
```

新增字段：`links`（显式双链，也可从正文 `[[...]]` 提取）、`backlinks`（反链，自动维护）、`sources`（溯源，指向产出它的 task/run）。

### 2.4 双链与反链的维护

- **写入时**：解析正文 `[[...]]` 提取 out-links → 写 `meta.links`；对每个 target entry 追加 `meta.backlinks`（写时更新，简单可靠）
- **`aha kb links`**：CLI 命令扫描全部 entry，重建链接索引（防孤儿/断链）
- **断链处理**：`[[X]]` 指向不存在条目时，Web 图视图显示为「虚线节点」，用户点击可创建

---

## 3. 沉淀层：默认必填 + 自动沉淀闭环

### 3.1 默认必填

- `default_knowledge_config.enabled = True`
- **任务创建时强制启用 KB**：`plan`/`create task` 时 KB 默认开（可显式关，但默认开）
- 任务 prompt 注入 KB 从「可选增强」变为「默认必有」（已有 `knowledge_context_for_task`，只需 enabled 默认 True）

### 3.2 审批档位扩展：`curation.gate`

| gate | 行为 |
|---|---|
| `manual`（现有） | 候选进 pending，人工审批 |
| **`agent-auto`（新增，默认）** | agent 产出候选 → 质量门控 → 高置信自动 approved 并 commit；低置信降级 pending |
| `none` | 全自动，不做门控 |

质量门控判定（`_quality_gate(candidate)`）：
- `confidence >= 0.8` 且 `relevance >= 0.7` 且**通过去重** → 自动 approved
- 否则 → pending（Web 端可见，可一键批准）
- 门控结果写 `knowledge_*_distilled` 事件，Web 端 pending badge 反映待审数

### 3.3 任务结束自动沉淀

**触发点**：`agent_finished`（main 每轮结束）+ `task_result_written`（任务 final 落地）。

**自动 distill 流程**（复用现有 `knowledge_distill` 管道）：
1. 收集本轮素材：final result、round summary、`kb_feedback.updated`、capture notes
2. `heuristic_solution_candidate` 提取「可复用结论」（已有）
3. agent 蒸馏（可选，用轻量模型）生成候选，带 `confidence/relevance/links/sources`
4. 质量门控：自动 approved 或进 pending
5. **双链自动挂载**：新条目自动链接到同项目 MOC + 相关条目（按标题/标签相似度）

**配置开关**（`knowledge.auto_distill`，默认开）：
```json
"auto_distill": {
  "enabled": true,
  "on": ["agent_finished", "task_result_written"],
  "gate": "agent-auto",
  "max_candidates_per_turn": 3,
  "link_similar_entries": true
}
```

### 3.4 git 自动同步

- `auto_commit = True`（每次 approved 入库自动 commit）
- `auto_push = True`（commit 后自动 push 远端，多设备共享）
- `auto_pull = True`（任务开始前 pull，已实现 `auto_pull_before_task`）
- commit message 带来源 task：`chore(knowledge): distill from task-004`

### 3.5 去重（防垃圾 + 防重复）

增强 `candidate_identity`：
- 语义相似度：标题 + 正文关键句的 token/Jaccard 相似度 > 阈值 → 视为重复
- 重复时**合并**（更新已存在条目，append sources + backlinks），不新建
- 冲突（同 slug 不同内容）→ 进 pending 人工裁决

---

## 4. 检索升级：双链传播

现有 `retrieve_for_task` 是关键词评分（含中文 bigram）。升级：

1. **一级检索**：现有关键词评分选 Top-K（保留）
2. **双链扩展**：对 Top-K 条目的 out-links + backlinks 扩展 1~2 跳，补充相关条目
3. **反链加权**：被引用越多的条目 `_score` 加权（`score * (1 + 0.1*backlink_count)`），视为更可信
4. **MOC 注入**：项目 MOC 总注入（类似 navigation index），作为路由
5. **注入预算**：`max_chars` 内优先填高置信 + 高相关，超出截断

这样：命中一篇「跨 OS 存活」→ 自动带出「WSL 后端」「桥心跳」等关联笔记，形成知识网络进入 agent 上下文。

---

## 5. 展示层：Web 端「星球图」（核心亮点）

### 5.1 效果目标

Obsidian 式 Graph View 的「星球」交互：
- **力导向图**：节点 = 知识条目，连线 = 双链/标签/项目关系；物理模拟让网络像星球/星云缓慢漂浮
- **可拖拽/缩放/旋转**：拖节点、滚轮缩放、拖背景平移
- **节点大小** ∝ 反链数（知识越被引用越大越亮）
- **节点颜色** 按 scope/kind（项目=蓝、通用=绿、solution=橙、navigation=紫、MOC=星形）
- **悬浮高亮**：悬浮一个节点，高亮它的直接邻居，其余淡出（观察关联）
- **点击节点**：打开该条目详情（复用现有 entry 详情弹窗）
- **搜索过滤**：输入关键词，匹配节点高亮，其余收缩
- **标签着色**：按 `#tag` 一键切换着色维度

### 5.2 前端实现（零重依赖约束）

AHA 声明零第三方依赖（`pyproject.toml dependencies=[]`），不能引 d3。方案：

**自研 ~400 行 Canvas 力导向图**（`knowledge_graph.js`）：
- **渲染**：`<canvas>` + `requestAnimationFrame`，节点画圆形/星形，连线画线段，标签用 canvas text
- **物理**：简单力导向——斥力（节点间）、引力（连线两端）、中心引力（星云不散）、速度阻尼
- **交互**：pointer 事件做拖拽/缩放/平移，`devicePixelRatio` 适配高分屏
- **性能**：节点数 < 300 时 60fps；节点 > 300 用距离裁剪（只画视口附近）
- **无 d3**：力模拟 ~100 行，完全可自研

前端页面新增 tab **`Graph`**（`knowledge.html`），API 拉取图数据。

### 5.3 后端图数据 API

新增 `GET /api/kb/graph`：

```json
{
  "nodes": [
    {
      "id": "cross-os-liveness",
      "title": "跨 OS 后端存活判断",
      "kind": "solutions", "scope": "project", "project_key": "...",
      "tags": ["backend","wsl"],
      "backlink_count": 4,
      "confidence": 0.9,
      "updated_at": "...",
      "color_group": "project"
    }
  ],
  "links": [
    { "source": "cross-os-liveness", "target": "wsl-backend", "type": "wikilink" },
    { "source": "cross-os-liveness", "target": "bridge-heartbeat", "type": "wikilink" },
    { "source": "MOC:aha-git-4117b370ee54", "target": "cross-os-liveness", "type": "moc" }
  ]
}
```

`links` 来源：`meta.links` + `meta.backlinks`（双链）+ MOC 挂载 + 同 project 关系（可选）。支持查询参数：`?project_key=&scope=&tag=&q=` 过滤。

### 5.4 Web 页面布局

```
┌──────────────────────────────────────────────────────┐
│ [Entries] [Nav] [Graph]★ [Pending] [Capture] [Skills] │
├──────────────────────────────────────────────────────┤
│  ┌─ 工具条 ──────────────────────────────────────┐   │
│  │ 🔍 [搜索] [项目:▾] [标签:▾] [着色:▾] [+新建]  │   │
│  ├───────────────────────────────────────────────┤   │
│  │            ┌─ 星球图 Canvas ─┐                │   │
│  │            │  ●──●            │  ← 拖拽/缩放  │   │
│  │            │ /  ✦  \          │  ← 悬浮高亮   │   │
│  │            │ ●────●           │  ← 点击详情   │   │
│  │            └──────────────────┘               │   │
│  └───────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

### 5.5 详情弹窗增强（Obsidian 化）

点击图节点/条目后，详情弹窗（复用 `kb-detail-fullscreen-modal`）新增：
- **反链面板**：「谁引用了这篇」列表，点击跳转
- **双链面板**：「这篇引用了谁」列表
- **标签行**：`#backend #wsl #bug` 可点击过滤
- **图定位**：点击「在图视图中定位」→ 切到 Graph tab 高亮该节点

---

## 6. 实施阶段（每阶段可验证）

> 标注：**升级** = 改现有代码；**新建** = 新增功能。多数是升级（基于 0.1 已有基础）。

### Phase 1：自动沉淀闭环（地基，最高优先级）
- [x] **升级** `enabled` 默认 True + 任务创建强制 KB（`default_knowledge_config`）
- [x] **升级** `curation.gate` 新增 `agent-auto` + 质量门控 `_quality_gate`（`knowledge_distill.py`）
- [x] **新建** `auto_distill`：agent_finished / task_result_written 自动蒸馏（挂 `orchestrator.py` 事件）
- [x] **升级** git 默认 `auto_commit/auto_push/auto_pull = True`（`knowledge_git.py`，机制已有）
- [x] **升级** 去重合并增强（`candidate_identity` → 语义相似度合并）——轻量版：标题 token Jaccard ≥ 0.6 或（tags 重叠 且 标题相似 ≥ 0.35）即合并历史候选，保留双方 body + 合并 sources
- **验证**：跑一个任务 → final 自动进 KB（approved）→ git 自动 push → 新任务注入该条目

### Phase 2：Obsidian 存储（网状知识）
- [x] **新建** `[[双链]]` 解析 + `meta.links/backlinks` 维护 + `aha kb links` 重建
- [~] **新建** `MOC/` 层 + 自动 MOC 挂载 —— 已收敛不做：`MOC/skills.md` 单页已实现（2c），通用 MOC 层留作后续可选
- [~] **新建** `zettelkasten/` 原子笔记 + 标签行内解析 —— 已收敛不做：当前用 `general/wiki` + `projects/<key>/navigation` 承载原子笔记，zettelkasten 目录留作后续可选
- **验证**：`aha kb links` 扫描 → 双链/反链正确；新条目自动挂 MOC

### Phase 2b：nav 升级为项目知识库
- [x] **新建** nav 的 `knowledge/` 子目录（decisions/pitfalls/components/topic）
- [x] **升级** 任务蒸馏分类落位：可复用→solutions，项目级→nav/knowledge，流水→worklog（`knowledge_distill.py`）
- [x] **升级** nav index 自动挂「知识区」链接 + 关联 solutions（`knowledge_navigation.py`）
- [x] **升级** `scan_workspace` 增强：除代码结构外识别架构决策/踩坑候选
- **验证**：跑一个项目任务 → 结论自动进 nav/knowledge + solutions，nav index 出现知识区（7 个单测：scan 识别决策/踩坑、sidecar 知识类目路由、worklog 路由、父级回填、solution 挂 nav index、知识 slug 校验通过/非法类目拒绝）

### Phase 2c：Skills 系统化
- [x] **升级** skill 区分系统/个人（frontmatter `source` + 目录 `system/`+`personal/`，`skill_management.py`）
- [x] **新建** `/skill` CLI 命令 + 技能创建说明提示词注入（`prompts/skill_creation_guide.md`）
- [x] **升级** Web skill tab 分区展示（系统只读 / 个人可编辑，`knowledge.html`）
- [x] **新建** skill 节点进星球图 + `MOC/skills.md` 技能总览
- [~] **升级** 技能绑定任务增强（图拖拽 / 蒸馏提升系统技能候选）——延后：任务绑定已具备（`task_skills` 注入），图拖拽/蒸馏提升系统技能候选后续做
- **验证**：`/skill` 创建技能 → 进 personal → Web Skills tab 可见/可编辑、系统技能只读（`scripts/verify_kb_skills.py` 真实浏览器）；星球图 skill 节点 + `MOC/skills.md`（9 个单测 + 全量 1658 通过）

### Phase 2d：通用附件支持（PDF/Word/TXT/MP4 + 附件内图片）
- [x] **升级** `knowledge_assets.py`：从"仅图片"扩为"通用附件"（白名单 mime + kind 分类 + 大小上限 + sha256）
- [x] **新建** `GET /api/kb/attachment`（按 mime 内联/下载 + Content-Disposition，路径校验防穿越）
- [x] **新建** docx/pptx 插图提取（标准库 `zipfile` 读 `word/media`/`ppt/media`，零依赖、失败隔离）
- [x] **升级** Web 条目详情「附件区」：图片内联 / PDF embed / 文本 pre / 视频播放 / Office 下载
- [x] **新建** 大附件独立存储（媒体/超大文件进 `<aha_home>/knowledge_local_assets/`，KB 只存引用 + sha256；gitignore 兜底排除媒体扩展名）
- **验证**：上传 PDF/docx/mp4 附件 → Web 详情附件区正确展示（pdf embed、mp4 video、txt pre）→ docx 插图自动提取内联（16 个单测 + 真实浏览器 `scripts/verify_kb_attachments.py` + git 排除实测）

### Phase 2e：智能同步 + agent KB 维护
- [x] **升级** `sync_status`/`pull`：冲突检测（`unmerged`/`rebase_in_progress`/`conflict` 状态）；agent 模式下 `pull` 保留 rebase 供维护任务处理（`knowledge_git.py`）
- [x] **新建** KB 维护 agent 任务（`knowledge_maintenance.py`）：真实 backend 分析 base/local/remote → JSON 计划 → 确定性应用（local/remote/merge/archive）+ 用户优先兜底 + rebase --continue + push + 状态落盘）
- [x] **新建** 定时同步调度器（`knowledge_sync_loop.py`：`knowledge.sync.interval_minutes`，服务内 asyncio 定时器 + 单飞锁 + 冲突派发维护）
- [x] **升级** Web 同步面板：冲突状态展示 + 「Resolve conflicts」按钮 + 轮询维护结果 + CLI `aha kb sync --resolve` / `aha kb sync-status`
- [x] **新建** `conflicts/` 保留无法判断的两版（archive action 把本地版存档到 AHA home 的 `conflicts/`，待人工）
- **验证**：本地双克隆模拟双设备各改 KB → sync 产生 diverged → agent 维护任务自动解决（用户优先）→ 知识合并不丢（`scripts/verify_kb_sync_ui.py` 真实浏览器 + 20 个单测 + CLI 端到端）

### Phase 3：检索升级
- [x] **升级** `retrieve_for_task` 双链传播 + 反链加权 + MOC 注入（`knowledge_retrieval.py`）——双链单跳传播 + 反链加权已完成（`_expand_by_wikilinks`/`_score`），MOC 注入随通用 MOC 层收敛未做（见 Phase 2 备注）
- **验证**：任务命中一篇 → 关联笔记随上下文注入

### Phase 4：Web 星球图
- [x] **新建** 后端 `GET /api/kb/graph`（`knowledge_routes.py`）
- [x] **新建** 前端 `knowledge_graph.js`（Canvas 力导向，自研零依赖）
- [x] **新建** Graph tab + 详情弹窗反链/双链/标签
- [x] **新建** 搜索/过滤/着色
- **验证**：KB 页 Graph tab 出现星球图，节点可拖拽/缩放，点击开详情，反链可跳

---

## 7. 数据流总览

```
用户/agent 倾倒 ─► capture/note
                      │ 手动/自动 distill
                      ▼
                  pending 候选
                      │ quality gate
          ┌───────────┴───────────┐
      approved(≥0.8)          pending(<0.8)
          │                       │
          ▼                       ▼
  写 zettelkasten/<slug>.md    Web 审批 ─► 写 entry
      + links/backlinks
      + MOC 挂载
      + 分类落位：
        · 可复用排障 ─► projects/<key>/solutions/
        · 项目级知识 ─► projects/<key>/navigation/knowledge/
        · 任务流水   ─► projects/<key>/worklog/
        · 新技能     ─► knowledge/skills/personal/（/skill 创建）
          │
          ▼
   git auto commit + push ─► 远端共享
          │
          ▼
   任务开始 ─► knowledge_context_for_task
             （关键词 + 双链传播 + nav index/MOC 注入
              → 路由层 + 知识层 + 经验层 + 技能层 四路注入）
```

---

## 8. 关键决策记录

| # | 决策 | 理由 |
|---|---|---|
| R1 | 保留旧目录 + 新增 zettelkasten/MOC | 兼容已沉淀知识，不破坏性迁移 |
| R2 | 双链用 `[[slug]]`/`[[标题]]` | slug 已是稳定 key，无需额外 id |
| R3 | `agent-auto` 为默认 gate | 自动沉淀是正向循环核心，人工审批是瓶颈 |
| R4 | 图可视化自研 Canvas，不引 d3 | AHA 零第三方依赖约束 |
| R5 | 节点大小 ∝ 反链数 | 反链多 = 被引用多 = 更可信，视觉直接表达 |
| R6 | auto_distill 每轮上限 3 条 | 防止噪音，保证沉淀质量 |
| R7 | skill 分系统/个人，系统升级覆盖、个人永不覆盖 | 系统技能随 AHA 演进，个人技能是用户资产 |
| R8 | `/skill` 注入创建说明提示词引导 agent | 让 agent 能共创技能，而不只是用户手写 |

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 自动 approved 引入低质知识 | 质量门控 + 置信度阈值 + git 可回滚 + Web 可编辑删除 |
| 双链断链/孤儿节点 | `aha kb links` 重建 + Web 虚线节点提示 + 点击创建 |
| 图视图性能（KB 增长到千级节点） | 距离裁剪 + 分层渲染 + 搜索过滤收缩 |
| 自动 push 泄漏敏感知识 | `auto_push` 可关；`knowledge.git.remote` 用户掌控 |
| 与已有 navigation/worklog 冲突 | 双链/MOC 是增量叠加，不动现有导航结构 |
| 大附件撑爆 git 仓库 | 大附件独立存储，KB 仓库只存引用+校验和；git 大小上限 |
| 恶意附件上传 | mime 白名单 + 大小上限 + `_sniff_*` 魔数校验（不信任扩展名） |
| docx/pptx 提取失败 | 提取失败回退"下载 + 外部打开"，不阻塞附件入库 |
| agent 误解决冲突（丢用户内容）| 用户优先原则 + agent 版本进 reflog 可回滚 + 用户可否决 |
| 定时同步频繁/网络抖动 | interval 可配 + 失败隔离（不可达静默重试）+ 幂等锁 |
| 用户 Obsidian 本地编辑未提交 | `dirty` 检测 + 定时/主动同步推送；agent 维护任务只处理冲突不覆盖用户未提交内容 |
