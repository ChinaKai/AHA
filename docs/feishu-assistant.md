# 飞书助手接入

AHA 的飞书接入使用企业自建应用、应用机器人和飞书长连接。AHA 主动连接飞书，
本机不需要公网 IP、域名或事件回调端口。私聊普通文本进入私聊“管家” agent；
owner 私聊机器人菜单可打开 Memo/Task 表单和查询入口；群聊只有 `@机器人` 的消息会进入受限“数字人” agent。
agent 回复再推送回原飞书会话；群聊不使用关键词菜单或固定问答规则。

## 安装与飞书应用配置

源码安装时启用可选依赖：

```bash
python3 -m pip install -e ".[feishu]"
```

Release onebin 不内置飞书 SDK。使用 onebin 时，需要在运行 onebin 的同一个 Python
环境安装 `lark-channel-sdk>=1.2,<2`。

在飞书开放平台创建企业自建应用并启用机器人，至少申请：

- `im:message.p2p_msg:readonly`
- `im:message.group_at_msg:readonly`
- `im:message:send_as_bot`

事件与回调订阅选择长连接：消息事件订阅 `im.message.receive_v1`，机器人菜单事件订阅
`application.bot.menu_v6`，回调订阅新版卡片回传交互 `card.action.trigger`。应用发布并安装到企业后，可以直接在 Web
设置的 Integrations → 飞书助手控制面板填写 App ID 与 App Secret。App Secret 与 Codex/Claude provider API Key
采用相同方式：密码框不回显已有值，再次保存时留空会保留原值。

如不希望把凭据保存在 AHA 配置中，也可以使用进程环境变量：

```bash
export AHA_FEISHU_APP_ID=cli_xxx
export AHA_FEISHU_APP_SECRET=xxx
```

设置中的直接值优先于环境变量；直接值为空时才读取配置指定的环境变量名。打开
Integrations → 飞书助手，启用接入并配置：

- `Private chats list`：展示最近检测到和已授权的私聊 `open_id/chat_id`。私聊始终校验 allowed
  `open_id`；群聊采用“仅授权用户”策略时也会校验发送者 `open_id`。列表支持加入/移除 allowed
  `open_id`，以及把某个 allowed `open_id` 设为唯一 owner。owner 私聊 `chat_id` 由主人私聊机器人后自动记录，
  不在设置页手动维护或手填。
- `Group list`：允许使用机器人的群 `chat_id`。群聊必须先命中此列表；空列表表示不开放群聊。
  将机器人加入群并 `@` 一次后，群会出现在 `Group list`，点击“加入”并保存即可，不需要逐个添加
  全部群成员的 `open_id`。列表支持加入/移除 allowed `chat_id`。如果飞书事件或后续权限能提供名称，设置页会在 ID 旁展示可读名称；
  否则展示短 ID/指纹。
- `Group member access`：默认“仅授权用户”，即群 `chat_id` 和发送者 `open_id` 都必须获授权；选择
  “授权群内全部成员”后，该群任何成员均可使用助手，但私聊仍只接受 `Allowed open IDs` 中的用户。
- `Only handle group @mentions`：建议保持开启，群聊只响应 `@机器人`。
- `Push task status changes`：在普通项目 run 的 task 状态改变时，只向 owner 私聊推送只读状态卡，展示 run/task、新旧状态和变更来源；系统 Run/Task、群聊和其他授权用户私聊不会收到状态推送。进入 `busy` 显示用户触发消息，离开 `busy` 显示 agent 最后回复，系统迁移显示事件原因。关闭后仍会送达飞书助手对话的直接文本回复。
- `Default work Run`：飞书默认归属 Run。候选来自普通 Run 列表，系统管理的服务管家 Run 和
  `feishu-group` Run 不可选择。数字人转发待办、主人私聊创建 memo、主人私聊创建 task 默认落到这个 Run；
  单次操作仍可手动指定其他普通项目 Run。

保存后重启 AHA，使长连接使用新配置。在 Web 设置的 Integrations 区域点击
“飞书助手”，可在当前页面展开完整控制面板，配置 App ID、App Secret、授权用户、群聊策略、
安全模式、任务状态推送以及飞书助手专用的默认后端、模型、思考深度和代理开关，并查看 SDK 与长连接状态。
后端、模型和思考深度留空时继承全局 AHA 默认值；代理开关使用共享 Core 代理地址，并继承所选后端的默认开关。
这些设置只影响之后创建的系统管家会话，不修改已有 task。
飞书设置不再出现在全局 AHA Settings 中；
App Secret 不回显，留空保存会保留已有值。控制面板不再重复显示安装命令。
面板使用 `/api/feishu` 脱敏接口，响应不会包含 App Secret。直接填写的密钥会保存在
AHA home 的 `config.json`，请限制该文件和 Web 控制台的访问权限。

## AHA 服务管家 Agent

- 私聊所有非空文本（包括“帮助”“任务”或 `/help`）均原样交给 agent 理解，不做固定意图匹配。
- 飞书只是消息 Channel；真实身份是当前 AHA 实例的系统管家，不是用户项目任务。AHA 会创建一个
  `kind=system`、`system_purpose=service_assistant` 的可见 Run，并为每个私聊会话创建
  `AHA Assistant · DM · <短标识>` 系统 Task。旧版名称为 `Feishu Assistant` 的专用 Run
  会原位迁移，旧普通 Task 隐藏保留，不删除历史。
- 管家 Run 与 Task 的 Workspace 固定为当前 `AHA_HOME`，不继承最近 Run、默认 Run 或某个项目路径；
  sandbox 固定为 `read-only`、approval 固定为 `never`、禁止子 agent。系统 Run 会出现在普通 Run
  列表和全局搜索中并可正常打开，但删除、隐藏/归档和自动 retention 仍会拒绝操作。
- 专用提示词来自 `src/aha_cli/prompts/service_assistant_*.md`，不是硬编码在飞书路由中。提示词包含：
  服务平台、版本、监听地址/端口、AHA Home、服务工作目录和源码根目录；AHA Home 各目录用途；
  Run、Task、Memo、KB 与安全 Settings 的操作手册。服务启动时会把脱敏运行快照写到
  `AHA_HOME/runtime/service.json`，提示词按当前快照渲染。
- 管家只能通过服务端 `service_assistant` 动作读写 AHA 状态，不能直接改 `config.json`、plan、事件、
  session 或飞书状态文件。查询操作立即执行；创建 Run/Task、给 Task 发消息、完成/重开 Task、
  创建/修改 Memo 及安全 Settings 修改会先生成自然语言预览卡片，隐藏内部 JSON 动作体，由用户点击“确认 / 取消”。
  裸文本“确认/取消”会作为普通私聊消息交给 agent，不再绑定任何待确认操作，避免误确认其他上下文。
  需要主人在多个方案间选择时，管家应生成选择卡片；点击选项只把选择结果送回当前私聊管家继续处理，不直接执行写操作。
  同一群、同一提问人的连续补充、追问、催促或同一上下文下的输出请求会复用同一个 active 群聊转单线程；
  只有确实存在多个不同群/不同人/不同 active 需求时，服务端才会自动生成转单选择卡，不要求主人填写内部 `handoff_id`。
- 飞书入口的会话 Run 与工作 Run 分离：服务管家 Run 和 `feishu-group` Run 只保存会话/身份状态；
  飞书产生的工作 memo/task 默认写入绑定的普通工作 Run。执行类群聊转发由管家先分类：
  计划/可延后类先 `create_memo` 进入绑定 Run 的待办池，主人之后决定是否升级为 task；
  提交、构建等即时类在主人私聊明确确认时才走直接确认/派发链路，不为每条群聊请求自动建 task。
- 飞书图片、文件、音视频等消息会先归一化为附件 manifest，并作为资源摘要传给数字人/私聊管家；当前不会臆测附件内容。
  已有飞书资源 key 时，发送侧可构造图片/文件消息；本地文件上传、消息资源下载和内容解析属于后续二进制资源链路。
- 管家路由提交、推送、合并或回滚请求时，只转发用户意图和仓库约束，不指定 backend、model
  或 `Generated-by`。提交身份由目标 Task 当前执行 Agent 的 AHA commit policy 生成；服务端不会让
  显式 `Generated-by:` 进入目标 Task，避免飞书助手自身模型污染提交尾注。
  当前飞书用户消息是授权边界：只要求 `commit` 时，管家不得补充 `push`，服务端会保留用户原话，并把
  “仅本地提交、禁止 push、继承目标 Task 当前提交策略”作为独立可信元数据注入目标 Agent；用户明确同时
  要求两者时，必须拆为先提交、后推送的两次独立确认。
  一次性待确认凭证只保存在服务端安全状态中，不出现在文本或卡片 payload；仅原飞书用户和原会话可用一次，
  24 小时过期；确认前目标状态变化时拒绝执行。同一会话产生新预览时，旧卡片自动失效。卡片发送后会把
  飞书 `message_id` 与具体服务端确认记录绑定，旧卡片不能消费新操作；确认成功显示绿色已处理态，取消、
  超时或被新预览替代时显示灰色失效态，按钮会被移除。在线超时卡片最多约 30 秒完成视觉更新，服务离线
  期间凭证仍严格按 24 小时失效，重连后补更新卡片。
- Channel 进站、出站、卡片回调/更新、REST fallback、连接状态和失败会写入
  `AHA_HOME/logs/feishu/YYYY-MM-DD.jsonl`。日志文件权限为 `0600`，只保存消息内容摘要、状态、transport、
  `message_id`、run/task 和哈希后的 chat/open_id/session，不保存原始事件报文、动作 JSON、App Secret、
  token、Authorization 或 Cookie。`feishu/subscriptions.json` 的读取、订阅变更和发送去重状态使用跨进程
  advisory lock，避免 Web 长连接和 backend 通知进程互相覆盖。
- 服务重启/升级、凭据/ACL/认证、全局 sandbox/approval 设置、原始文件写入、KB 审批/同步以及删除类操作不向
  飞书管家开放。需要项目分析或代码修改时，管家应创建普通项目 Task 或向已有 Task 发消息，
  不在 AHA Home 中直接完成项目工作。
- 入站 `message_id` 做 24 小时幂等；私聊按企业与用户隔离。最近检测到的私聊和群只作为本地管理页候选，
  不会自动获得访问权限；记录保存在
  `AHA_HOME/feishu/recent_chats.json` 和 `recent_groups.json`（均为 `0600`）。可读名称缓存保存在
  `identity_profiles.json`（`0600`）。飞书长连接为 `connected` 且配置了 App ID/Secret 时，设置页会 best-effort
  调用飞书“获取单个用户信息”和“获取群信息”接口补齐用户名/群名；列表展示为 `id.name`，权限不足或接口失败时
  降级为 `id.未解析`，并在设置页显示飞书返回的权限错误。用户名称通常需要 `contact:contact.base:readonly`
  等通讯录权限；群名称需要 `im:chat:readonly`、`im:chat` 或 `im:chat:read` 之一。
  Channel 审计仍只保存 chat/open ID 哈希。
- 私聊管家会话绑定与回复订阅保存在 AHA home 的 `feishu/` 私有目录。消息送达 agent 后，AHA 自动订阅该
  task，把 agent 回复推回原飞书会话。
- 管家通过 `send_task_message` 把需求派发给普通 Task 后，会持久记录原飞书会话与目标 run/task 的 handoff。
  目标 Agent 的本轮真实回复会自动以“`AHA 跟进已完成`”推回原会话，不依赖全局状态推送开关，也无需用户
  再次要求跟进；该会话随后的一条通用完成状态会被抑制，避免重复通知。
- 管家通过飞书创建 Memo/Task 时不是直接落库：Memo 先给主人发送属性选择卡，再发送最终确认卡；Task
  先发送创建配置卡，再发送最终确认卡。Task 创建配置卡覆盖 Run、workspace、backend/model、代理开关和
  AHA KB 开关；Run 下拉只包含非系统管理 Run，按 `Run 名称.run_id` 展示。model 选项由服务端动态读取
  backend 支持列表，并合并已配置的 env group。Task 执行模式固定为 `auto`，不再提供执行模式选择。
  复杂字段仍可由主人在自然语言里明确补充后重新生成卡片。
- owner 私聊机器人菜单是同一套管家入口的显式触发方式，只支持 owner。飞书开放平台可配置事件型菜单：
  `aha_create_memo`、`aha_create_task`、`aha_list_memos`、`aha_list_tasks`。创建菜单复用上面的表单卡和最终确认；
  菜单创建 Memo/Task 时会在表单内提供标题和正文输入，其他属性与 Web 创建表单使用同一套真实字段；
  Memo/Task 查询提交后会把原查询表单卡直接替换为结果卡，不再额外展示“已选择方案”；结果卡可直接取消并结束本次查询，卡片更新失败时发送新结果卡兜底；
  查询 Task 后可确认进入 `Task Chat`，后续私聊文本直接发给所选普通 Task。Task Chat 会逐条即时转发 Web 当前主 Agent 会话在 `Chat` 筛选下可见的普通消息、Agent update 和 Agent error，不合并、不限频；
  飞书入站文本不回显，原始 AHA action 信封不转发，Agent update 后生成的同文最终消息只保留一份；
  普通状态文本不推送，但 `awaiting_user` 和终态仍发送退出/保留控制卡，重新进入 `running` 时只使旧控制卡失效。
  这些状态不排队、退出后也不补发；其他 Task 的状态仍按 owner 全局推送规则处理。
  数字人转单卡可即时送达，但不会覆盖或退出当前 Task Chat。发送 `退出 task` 可显式返回 AHA 管家。
  确认、取消或控制操作成功后，原卡片的终态更新就是处理凭证，不再额外发送重复的成功提示；卡片更新失败、
  操作失败、需要下一步表单或存在实际后续结果时仍会发送新消息。
  群聊仍只响应 `@机器人`，不开放菜单管理入口。

状态推送开启时，非系统 run/task 的状态变化只会以只读卡片推送到已解析的 owner 私聊；
卡片不包含按钮，标题颜色按当前状态区分：`running` 蓝色、`awaiting_user` 橙色、`completed` 绿色、
`failed/blocked` 红色，其他状态灰色。`running` 对外显示为 `busy`，`awaiting_user` 显示为
`awaiting`，正文保留以下字段：

```text
Time: 2026-08-05 15:48:41+00:00
Task: Run A.task-001
Task Title: Task 1
Status: busy -> awaiting
Message: agent 最后一条回复
```

开关开启时，agent 最终回复与状态变化合并为一张状态卡，避免重复推送；关闭时
不发状态通知，但助手对话的直接 agent 文本回复仍正常送达。确认卡和 Task Chat 控制卡不受影响。

## 群聊数字人模式

- 群聊身份与私聊管家分离。群聊只处理已授权群中的 `@机器人` 消息，不监听全量消息；未 `@` 的群消息会被忽略。
- AHA 会创建长期系统 Run：`feishu-group`，标记为 `kind=system`、`system_purpose=feishu_group`。
  Workspace 固定为 `AHA_HOME/feishu_group_state/`，权限为 `read-only`/`never`，禁止子 agent；这些资产属于服务状态，
  不写入任何项目 repo。
- 数字人 Task 按飞书发送者 `open_id` 建立映射：`Feishu Digital Human · User · <短标识>`。同一用户跨群复用同一
  task 记忆；不同用户互相隔离。被 `@` 的原群、原消息和提问者身份只作为本轮回复投递元数据保存，不作为普通群订阅。
- 主人身份是单一收件人，不是列表；优先使用 `integrations.feishu.owner_open_id`/`owner_chat_id`；未配置时使用首次私聊管家
  成功建立的 owner 私聊状态；若仍没有状态且 `allowed_open_ids` 只有一个值，则把该值作为 owner。群聊提问者
  `open_id` 只用于数字人 per-user 记忆，不会被当成主人。
- `allowed_open_ids` 表示谁可以私聊使用飞书助手；`owner_open_id` 是其中唯一主人。保存 Feishu 设置时，
  AHA 会自动保证 owner 在 allowed 列表中，并基于 owner 私聊状态清理旧 app/旧 owner 留下的 p2p 订阅和 owner 状态。
  飞书助手设置面板也提供“清理旧 App 状态”按钮，执行前会预览将清理的授权用户、owner 记录、私聊记录和订阅数量。
  群订阅不参与这类清理，群聊访问仍由 `allowed_chat_ids` 和 `group_access_mode` 控制。
- 直接问答：模型判断可公开回答时，数字人在原群、原消息下回复，并由服务端自动携带提问者飞书 `@`。
- 数字人忙碌时，同一 Task、同一群、同一提问者积压的连续 `@` 消息会按发送顺序合并为下一轮输入，只调用一次模型，
  并在最新一条消息下回复一次。不同提问者、不同群以及浏览器/命令消息保持隔离；已开始处理的当前轮不会被后到消息改写。
  单批最多保留最新 20 条、约 8000 字符和 8 个附件，超出部分会在输入及 `feishu_group_messages_coalesced` 事件中标记，
  防止刷屏无限放大模型调用和上下文。
- 数字人问答的信息源包括 AHA 知识库索引、已配置 `workspace_roots`/已注册 workspace 的项目路径索引、
  当前群聊 `@` 上下文和同一数字人 task 的近期群聊上下文。Prompt 只注入索引、路径候选、README/docs/src/tests
  等标记和知识库条目元数据，不把知识库正文或项目文件正文全量塞进 prompt；需要更多细节时由 agent
  按最小范围读取相关资料。
- 执行类但信息不足：数字人先在群里简短追问，直到需求明确。
- 需求明确且需执行、需授权、涉及私密内容、承诺/争议类请求：数字人只能触发 `feishu_group_handoff`，服务端在群里发送固定话术
  `您的问题已记录，我已转发给主人，有进展给您回复`。数字人 action 只携带公开安全摘要、转发原因和可选
  `details`/`merge_handoff_id`，不会把“管家应该如何处理”的 SOP 写进转单消息；私聊管家的处理 SOP 固定在管家专属提示词中。
- 服务端会把转单事实信封投递到主人私聊管家 Task，并同时给 owner 私聊发送“转单处理”卡。Owner 只需二选一：
  `整理为待办` 或 `无需处理`。卡片中的群聊和发送人按 `id.name` 展示；需求摘要来自数字人 `summary`，
  需求详情来自 `details`，缺省时用原始群消息和转发原因兜底。
- 选择 `整理为待办` 后，服务端先确定性检查是否已有同需求 active Memo；命中时直接关联并回群同步，未命中时根据数字人的
  `summary`、`details` 和原始群消息形成可编辑草稿，并立即打开与 Web 一致的 Memo 表单卡和最终确认卡，不依赖管家 backend
  再生成一轮消息。即使 owner 正处于 Task Chat，后续卡也使用原转单卡绑定的身份与会话发送，且不覆盖 Task Chat 订阅。Memo 确认创建成功后，转单终态为
  `memo_created`，数字人自动回原群原消息并 @ 提问者：`主人已经加入待办，请耐心等待，有进展我会同步。`
- 选择 `无需处理` 后，转单终态为 `dismissed`，不创建 Memo/Task，也不回群。转单处理卡不提供取消按钮。
- 管家只在主人私聊中整理和确认；群聊外显身份只有数字人。群聊转单默认不直接创建 Task，也不提供代发回复、本人处理、
  拒绝处理等分支；主人要公开说明时可以直接在群里回复。
- 计划/可延后类转单进入 Memo 待办池时，也必须先经过 Memo 属性选择卡和最终确认卡；不会因为数字人转发就跳过主人确认。
- 当前群聊数字人公开回复只支持文本；图片、二进制、文档等文件不能由数字人直接发到群里。涉及这类内容时，
  数字人应告知需要主人本人发送或走其他渠道。
- 转发单有显式终态。Owner 处理卡当前只产生 `memo_created` 或 `dismissed`：创建 Memo 后终态为
  `memo_created`，无需处理时终态为 `dismissed`，避免 pending 单悬空。
- 同一群同一提问人的连续补充、追问、催促或同一上下文下的输出请求不会创建多个 pending 转单；服务端会合并到 active
  handoff thread，保留原始群消息作为默认回群锚点，并记录最新补充消息。刚刚代发过的 `delivered` 单在短时间内收到同一上下文追问时会重新打开为 pending。
- 数字人 prompt 会列出当前用户的 active/recent 转单线程摘要；模型判断当前 @ 是同一需求的补充、追问、催促、继续执行或请求输出时，应在
  `feishu_group_handoff` action 中带 `merge_handoff_id` 复用该单。只有明确是独立新需求时才设置 `new_handoff: true`。
- 数字人全局红线：不碰钥匙、不泄底、不替你做主。它不能提交、推送、合并、改设置、承诺结果、透露 AHA 内部、
  密钥、权限结构或其他 task 私密内容。
- 当前实现的上下文窗口以本次 `@` 消息为锚，并保留线程/root/parent 标识供后续历史抓取扩展；默认不额外拉取群历史。
  若后续启用飞书历史消息 API，应继续遵守 2000 token 上限、自然边界停止和“引用消息单次只读”的隐私约束。

## Tailnet HTTPS 网页入口

当前推荐给单用户部署的完整 Web 访问链路是：

```text
飞书 H5/浏览器 -> Tailscale Serve HTTPS（tailnet only） -> 127.0.0.1:8766
```

AHA 继续只绑定本机回环地址；Tailscale Serve 同时代理 HTTP 和 WebSocket，并用 Tailnet 身份控制
谁能访问。飞书网页应用主页直接填写 `https://<node>.<tailnet>.ts.net/`，无需飞书 OAuth 回调，
AHA Settings 也不再提供 `Enable secure Web access`、Public HTTPS origin 或 Web session lifetime。

这等同于信任能够访问该节点的 Tailnet 成员。仅本人使用时可以直接开放完整 Web 功能；如果未来
Tailnet 增加其他成员，应改用 Tailscale ACL、AHA Web token 或独立的认证代理收紧访问。不要启用
Tailscale Funnel，也不要把 `8766` 直接监听到公网网卡。

## 微信迁移与限制

新配置默认隐藏并停用微信入口、keepalive 和微信通知，但不会删除旧微信登录状态。
首版飞书接入采用单进程本地队列，入口队列容量为 128；数字人忙碌期间的同用户群消息会在持久化 inbox 消费端合并，
但超过入口队列容量的突发流量仍会收到忙碌提示。AHA 重启期间不会接收长连接消息，发送失败会记录为
run event，但暂不提供持久化重试队列。生产使用时还应在飞书后台限制应用可用范围，
定期轮换 App Secret，并保持 `allowed_open_ids`、`allowed_chat_ids` 为最小授权集合。若无明确需求，群成员
策略保持 `allowed_users`，避免授权群中的任意成员获得 AHA 管理能力。

飞书官方资料：

- [通过 Channel SDK 将 Agent 接入飞书](https://open.feishu.cn/document/mcp_open_tools/integrating-agents-with-feishu/integrate-feishu-channel)
- [处理卡片回调](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks?lang=zh-CN)
- [lark-channel-sdk](https://pypi.org/project/lark-channel-sdk/)
- [网页应用（H5）概述](https://open.feishu.cn/document/client-docs/h5/introduction)
