# 知识库 Capture（第三摄入通道）实施计划

> 实时进度文档。每个 Phase 完成后更新「进度日志」与勾选状态。

## 背景与目标

知识库现有两条摄入通道：① 任务 final/report 自动蒸馏（被动）；② `aha kb tutorial add` 手工导入干净成稿。
缺一条**主动捕获**通道：用户把零散/口语/截图等「乱七八糟」的原料先囤进去，按需调用 agent 整理成候选知识。

三段式管线：

```
原始笔记 (capture inbox) ──〔agent 整理〕──> pending 候选 ──〔approve〕──> 正式条目
```

## 已锁定的设计决策

- **新 scope `personal`**：默认落点；**不进任务开工注入**（`retrieve_for_task` 不收集 personal），只通过 `kb search` / `kb list` / Web 按需检索召回。
- **raw note（capture inbox）**：独立存储 `.capture/`（每条 JSON），**默认不进 git**（原始未审、可能含敏感信息），与 `.pending/` 同性质。只有 approve 成正式条目后才随 git 同步。
- **agent 整理**：one-shot 调用（非完整 task 编排），可选 backend/model；输出沿用 `<aha_knowledge_candidates>` JSON，复用 `split_knowledge_sidecar` 解析 → 入 pending。
- **图片**：纳入 KB 随 git 同步；同 scope 的 `assets/`；护栏=大小上限+降采样；agent 标注每图 `transcribed`（转录后弃）/ `keep-as-asset`（留原图、正文 `![]()` 引用）；有图必须多模态模型。
- **Web Capture tab**：类 Entries 的列表，每条 raw note 可查看/编辑/删除/「整理为候选」；慢 LLM 调用走 job + 轮询，不阻塞。

## 分期计划（依赖链：scope → inbox → distill → web → images）

- [x] **Phase 1 — `personal` scope**：store `SCOPES`/`scope_dir`/`init`/`iter_all_entries`/`status` 支持 personal；retrieval 注入天然排除 personal；CLI/Web scope 筛选加 personal。focused tests：personal 条目不被注入、能被 search/list/iter 召回。
- [x] **Phase 2 — capture inbox 存储 + CLI**：`.capture/` raw note CRUD（id/text/images/scope_hint/created_at/status/candidate_ids）；`aha kb capture add|list|show|edit|rm`。`.capture/` 加入 `.gitignore`。focused tests：CRUD、不进 git。
- [x] **Phase 3 — distill-on-demand（完成）**：`aha kb capture distill <id>` → one-shot agent distiller（可选 backend/model）→ 解析 sidecar → 入 pending；note 标 `distilled` 并记 `candidate_ids`，重跑替换上次候选。
  - 核对结论：AHA **无稳定 in-process「prompt→reply」API**——backends 是子进程 CLI（`run_claude_exec`/`run_codex_exec`，返回 `(exit_code, reply, session)`，仅 prompt/cwd/output_file 必填）。故按预案抽**窄 service seam**：`knowledge_capture_distill.CaptureAgent`（`ctx→reply_text`），默认实现 `default_capture_agent` 薄封装 run_claude_exec/run_codex_exec（无新依赖、best-effort、可替换），测试注入 deterministic stub。
  - 复用：`split_knowledge_sidecar` 解析 + `normalize_sidecar_candidates`（已扩展支持 personal scope）+ `enqueue_candidate` 始终入 pending（raw 必经人工审核）。project 候选无 key 时降级 personal。
  - 新增 `tests/test_knowledge_capture.py` Phase 3 用例（5）：入队+标 distilled、重跑替换、project 降级 personal、空候选、note 不存在。
- [x] **Phase 4 — Web Capture tab（完成）**：知识页第 4 个 tab，raw note 列表 + 查看/编辑/删除 + 新建 + 「整理为候选」（后台 job + 轮询，不阻塞）→ 跳 Pending 审批。
  - 核对结论：Web 无可复用的「一次性 job + 轮询」框架（`start_backend` 是 run/task 绑定的完整 agent 进程，太重）。故做**窄接口**：note 自身 `status`（raw→distilling→distilled|error）即 job 记录，distill 用 daemon thread 跑共享的 `run_distill_job`，前端轮询 note status；无新 job store、无新依赖。
  - 实现：API `GET/POST/PATCH/DELETE /api/kb/capture` + `POST /api/kb/capture/distill`（同步置 distilling 后台跑，立即返回）；dispatch 经 `knowledge_routes.dispatch_distill_job` seam（测试替换为同步 stub）。前端 Capture tab（列表/新建/内联编辑/删除/整理+轮询）+ i18n 中英。复用 CLI 同一 `distill_note`/`run_distill_job` service。
  - 测试：route CRUD+distill（同步 seam+stub agent，断言 distilling→distilled、入 pending）、distill 缺失 note 404、create 需 text；service `run_distill_job` 状态机成功+失败（error+last_error）。`node --check` i18n.js 与 knowledge.html 内联 JS 通过。
- [x] **Phase 5 — 图片/资产**：5a（capture 侧摄入）✅ `c37f868`；5b（approve 资产迁移）✅（见下定稿，本轮实现，未提交）。真实视觉仍待 backend 图片输入能力（不在本期）。

## Phase 5 设计定稿（图片资产，保守方案）

**已锁定决策（用户拍板）**
- raw note 阶段图片落 `.capture/assets/<note-id>/<filename>`，随 `.capture/` **不入 git**。
- approve 成正式条目时，再把图片复制到该条目的 assets 目录，随条目入 git 同步。
- **不长期把 base64 存 JSON**：上传可走 base64 / data URL，但**落盘为文件**；note 只保存 `{path（相对 KB root）, mime, size, filename}`。
- 护栏（**无新依赖版本**）：仅允许 `png/jpeg/webp`（按 magic bytes 嗅探，不信任扩展名）；单图与单 note 总大小上限，超限**拒绝**；降采样若现有依赖不支持则**先不做**。
- 多模态：**核对结论 = 当前 `build_claude_exec_command`/`build_codex_exec_command` 无任何图片/附件入参，exec 只传文本 prompt（stdin/`-p`）**。故本期 distill **不做真实视觉**——仅把图片清单（filename/mime/size/path）写入 prompt，并显式标注「图片未被视觉理解，待 backend 图片输入能力接入」。**不得伪装已看图**。

**拆期**
- **5a（本期，最小闭环）**：note 图片上传/列出/删除（落盘 `.capture/assets/<id>/`、护栏、note.images 元数据）；图片读取路由（缩略图预览）；distill prompt 注入图片清单 + 未视觉理解声明；Web Capture tab 上传 + 预览 + 删除；focused tests。
- **5b（in_progress）**：approve 时把 note 资产复制进条目 assets 目录、条目正文/metadata 留可追溯引用；候选↔note 资产关联；backend 具备图片输入后接真实 vision。

### Phase 5b 设计定稿（approve 资产迁移，保守）

**边界（用户拍板）**
- approve **只负责复制**：把 capture 资产从 `.capture/assets/<note-id>/` 复制到正式条目的 assets 目录，并在条目正文/metadata 留可追溯引用。
- approve 流程内**不执行 git commit/push**：只让文件进入「可被现有同步/提交机制管理」的状态（条目所在 tracked 树）；提交仍由既有 `auto_commit_after_change`/`kb sync` 等机制负责。
- 候选↔来源 note 关联：**优先显式 `source_note_id`**（distill 时写入候选），同时保留 `note.candidate_ids` 反查兼容路径。
- **幂等**：重复 approve/重试不重复复制、不覆盖无关或已存在文件（dest 已存在则跳过）；raw `.capture/assets` **不删除**（仅用户删 note 时随 note 清理）。

**落点**
- 条目资产目录：`<entry_dir>/assets/<slug>/`（即 `<scope>/<kind>/assets/<slug>/`，与 `<slug>.md` 同级的 `assets/` 子目录；`list_entries` 只 glob `*.md`，不受影响）。条目正文相对引用 `![](assets/<slug>/<file>)`。
- 条目 meta 加 `source_note_id` 与 `assets:[{name,mime,size,path}]`。
- 实现位置：`knowledge_capture.promote_assets_for_entry(...)`（lazy import 进 `store/knowledge.approve_candidate`，避免 import 环）；`distill_note` 给候选写 `source_note_id`。

## Phase 6 设计定稿（速记 Markdown 渲染 + 图片入正文，Level B，in_progress）

**目标**：速记编辑区复用 memo 的 Markdown 渲染；图片像 memo 一样嵌进正文 markdown，而非独立缩略图条。

**核对结论**：memo 渲染器 `window.AHATaskMemoMarkdown.renderMarkdownPreview` 是无第三方依赖的 DOM 版渲染，可复用；但 `memoImageSrc` 只认 memo 资产前缀（`task_memo_assets/`、`/api/task-memo-assets/`、`data:image/`），**capture 的 `/api/kb/capture/image?…` 不被解析**（会退化成纯文本）。故需给共享渲染器加一个**向后兼容的 `options.imageSrc` 解析钩子**（默认 = 现有 `memoImageSrc`，memo 行为不变），capture 传入「capture URL 原样返回」的解析器。

**本期实现（capture 侧 Level B）**
- 共享模块 `task_memo_markdown.js`：`createImageMarkdownNode` 增加 `options.imageSrc` 钩子（默认 memoImageSrc）。memo 不受影响。
- `knowledge.html`：load `task_memo_markdown.js`；速记展开视图用 `renderMarkdownPreview(note.text, {imageSrc: capture 解析器})` 渲染（替代 `<pre>`）；编辑仍为 textarea（原始 md）。去掉独立缩略图条，图片改为正文 md 内联渲染；保留「+图片」上传按钮。
- store `add_note_image`：持久化文件 + 注册 `note.images`（保留，供护栏/生命周期/5b）之外，**把 `![<original>](/api/kb/capture/image?id&name)` 追加进 `note.text`**（服务端、原子、可测）。

**保留/暂不改**
- `note.images` 仍是文件登记表（护栏：单图/单 note 上限；删 note 清资产）。
- **5b approve 迁移仍按 note.images 复制 + 追加 `## 附图`**（保持正确：图片照样进条目）。可选增强（Level B-2，后续）：approve 扫描正文 md 的 capture 图片 URL，复制并就地改写为 `assets/<slug>/<file>`，实现条目内联图片；当前不阻塞。

## 进度日志

- 2026-06-20：计划成文。开始 Phase 1（personal scope）。
- 2026-06-20：**Phase 1 完成**。store 新增 `personal` scope（`PERSONAL_DIR`/`SCOPES`/`scope_dir`/`init`/`iter_all_entries`/`status`）；retrieval 注入天然排除 personal（`retrieve_for_task` 只收集 project/general）；CLI `kb add`/`kb list` `--scope` 加 personal；Web scope 筛选下拉 + i18n（个人/Personal）。新增 `tests/test_knowledge_personal.py`（3 项：存储+status、不注入但可 search 召回、CLI add/list）。验证：`pytest tests/ -q` → 695 passed；`node --check i18n.js` OK。改动未提交。
- 2026-06-20：**Phase 2 完成**。新增 `store/knowledge_capture.py`（`.capture/` raw note CRUD：create/list/read/update/delete，id=`cap_<uuid>`，字段 text/title/scope_hint/images/status/candidate_ids/时间戳）；`.capture/` 自动写入 KB `.gitignore`（保留既有 `.pending/`）。CLI 新增 `aha kb capture add|list|show|edit|rm`（支持 `--text-file -` 读 stdin）。新增 `tests/test_knowledge_capture.py`（5 项：CRUD、非法 scope 回落 personal、gitignore、CLI 增删查、add 需文本）。验证：`pytest tests/ -q` → 700 passed；端到端 add(stdin)/edit/list/gitignore OK。改动未提交。下一步 Phase 3。
- 2026-06-20：**Phase 3 完成**。核对发现 AHA 无稳定 in-process one-shot API（backends 为子进程 CLI exec），按预案抽窄 seam `services/knowledge_capture_distill.py`（`CaptureAgent` 可替换，默认薄封装 `run_claude_exec`/`run_codex_exec`，无新依赖）；pipeline = note→agent→`split_knowledge_sidecar`→`normalize_sidecar_candidates`（扩展支持 personal）→`enqueue_candidate`（始终 pending）→note 标 distilled+candidate_ids，重跑替换。CLI `aha kb capture distill <id> [--backend --model]`。验证：`pytest tests/ -q` → 705 passed（+5 stub 测试）；CLI distill 缺失 note 优雅报错。**真实模型调用**：默认 seam 已接 run_*_exec，但本环境未跑真实模型；pipeline 由 stub 全覆盖，真实调用保持可替换。改动未提交。下一步 Phase 4（Web Capture tab，job+轮询）/ Phase 5（图片资产）。
- 2026-06-20：**Phase 4 完成**。核对发现 Web 无可复用的一次性 job/轮询框架（`start_backend` 太重、run/task 绑定），改用窄接口：capture note 的 `status` 即 job 记录，daemon thread 跑共享 `run_distill_job`，前端轮询，不阻塞 HTTP。新增 API `/api/kb/capture`（GET/POST/PATCH/DELETE）+ `/api/kb/capture/distill`（dispatch seam，测试可替换为同步）；store `update_note` 加 `last_error`；service 加 `run_distill_job`（raw→distilling→distilled|error 状态机）；前端 Capture tab（列表/新建/内联编辑/删除/整理+轮询）+ i18n 中英。验证：`pytest tests/ -q` → 709 passed（+4）；`node --check` i18n.js 与 knowledge.html 内联 JS 通过。改动未提交。下一步 Phase 5（图片资产）。已提交 `6b3c404`。
- 2026-06-20：**Phase 5a 完成**（设计定稿见上）。backend 核对：`build_claude_exec_command`/`build_codex_exec_command` 无图片入参 → 本期**不做真实视觉**，distill prompt 仅注入图片清单 + 「NOT visually analyzed」声明，不伪装看图。实现：store `knowledge_capture` 加图片护栏（magic-byte 嗅探，仅 png/jpeg/webp；单图 5MB / 单 note 总 20MB 上限）+ `add/remove/read_note_image`，落盘 `.capture/assets/<id>/`（随 `.capture/` 不入 git）、note 仅存 `{name,original,mime,size,path}`；delete_note 连带清资产目录。API `/api/kb/capture/image`（POST base64/data_url、GET 流式、DELETE）。前端 Capture tab 缩略图 + 上传 + 删除 + CSS + i18n。验证：`pytest tests/ -q` → 715 passed（+6）；`node --check` 两文件通过。改动未提交。**待续 5b**：approve 时把 note 资产复制进条目 assets 并随条目入 git；backend 具备图片输入后接真实 vision。已提交 `c37f868`。
- 2026-06-20：**Phase 5b 完成**（设计定稿见上）。`approve_candidate` 在写条目前调用 `knowledge_capture.promote_assets_for_entry`（lazy import，approve 内**不 commit/push**）：把来源 note 的图片从 `.capture/assets/<note-id>/` 复制到 `<entry_dir>/assets/<slug>/`，条目正文追加 `## 附图` + `![](assets/<slug>/<file>)`，meta 加 `source_note_id` + `assets[]`。候选↔note 关联：distill 给候选写显式 `source_note_id`，并保留经 `note.candidate_ids` 反查兼容路径。幂等：dest 已存在则跳过、绝不覆盖；raw `.capture/assets` 不删除。验证：`pytest tests/ -q` → 719 passed（+4：approve 端到端迁移、幂等不覆盖、反查兼容、无来源返回 None）；既有非 capture approve 不受影响。改动未提交。**剩余**：真实 vision 待 backend 图片输入能力接入。已提交 `7a5b597`。
- 2026-06-20：**补 Web 缺口**——Capture tab 之前无 backend/model 选择器（CLI/API 早已支持，前端没暴露）。新增「整理用 agent」backend + model 下拉：前端 `GET /api/backends` 拉取选项（复用建 task 同一来源），`distillNote` 把所选 backend/model 随 distill POST 传给后台 job。空值回退服务端默认。新增 route 测试断言 distill 端点把 backend/model 透传到 dispatch seam。验证：`pytest tests/ -q` → 720 passed；`node --check` 两文件通过。改动未提交。
- 2026-06-20：**Capture tab UX 收紧**（用户反馈）。① 速记卡片紧凑（更小内边距/标题、隐藏 note id）；② 移动端 4 个 tab 一行（`.kb-tabs` mobile `repeat(4)`，同步更新 `test_frontend_static`）；③ 速记列表加 View 折叠/展开（默认折叠一行预览，View 展开全文，与知识列表一致）；④ backend/model 选择器从常驻行改为**点 distill 时弹出小浮层**（记住上次所选并预填，空值回退默认）。纯前端（knowledge.html/i18n.js）+ 测试断言更新。验证：`pytest tests/ -q` → 720 passed；`node --check` 通过。改动未提交。
- 2026-06-20：**Capture 卡片再收紧 + 图片入正文**（用户反馈）。① 操作按钮（distill/view/edit/delete）压成紧凑一行：移动端新增 `.kb-cap-actions` grid `repeat(4)`，候选数/错误信息从按钮行移到 muted 状态行。② 图片改为**始终在正文里**（缩略图 + 上传控件不再仅 view 时出现，与 memo 一致）。纯前端（knowledge.html）。验证：`pytest tests/ -q` → 720 passed；`node --check` 通过。改动未提交。
- 2026-06-20：**Phase 6（速记 Markdown 渲染 + 图片入正文，Level B 用户选定）capture 侧完成**（设计定稿见上）。共享渲染器 `task_memo_markdown.js` 加向后兼容 `options.imageSrc` 钩子（默认 memoImageSrc，memo 不变）。`knowledge.html` 载入该模块；速记展开视图用 `renderMarkdownPreview(note.text, {imageSrc: capture 解析器})` 渲染（替代 `<pre>`），编辑仍 textarea；去掉独立缩略图条，保留「+图片」上传。store `add_note_image` 持久化+登记 `note.images` 之外，把 `![original](/api/kb/capture/image?id&name)` 追加进 `note.text`（服务端、原子）。5b 迁移仍按 note.images（图片照样进条目），inline-rewrite 留作 Level B-2 可选增强。验证：`pytest tests/ -q` → 721 passed（+1 store 测试）；memo 渲染测试不受影响；`node --check` task_memo_markdown.js 与 knowledge.html 内联 JS 通过；frontend_static 锁定载入与 renderMarkdownPreview 复用。改动未提交。
