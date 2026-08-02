# 飞书助手接入

AHA 的飞书接入使用企业自建应用、应用机器人和飞书长连接。AHA 主动连接飞书，
本机不需要公网 IP、域名或事件回调端口。机器人收到的普通文本会直接进入真实
AHA agent，agent 回复再推送回原飞书会话；不使用关键词菜单或固定问答规则。

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

事件与回调订阅选择长连接：消息事件订阅 `im.message.receive_v1`，回调订阅新版卡片回传交互
`card.action.trigger`。应用发布并安装到企业后，可以直接在 Web
设置的 Integrations → 飞书助手控制面板填写 App ID 与 App Secret。App Secret 与 Codex/Claude provider API Key
采用相同方式：密码框不回显已有值，再次保存时留空会保留原值。

如不希望把凭据保存在 AHA 配置中，也可以使用进程环境变量：

```bash
export AHA_FEISHU_APP_ID=cli_xxx
export AHA_FEISHU_APP_SECRET=xxx
```

设置中的直接值优先于环境变量；直接值为空时才读取配置指定的环境变量名。打开
Integrations → 飞书助手，启用接入并配置：

- `Allowed open IDs`：允许访问 AHA 的飞书用户 `open_id`，逗号分隔；空列表拒绝所有用户。
  未授权用户私聊机器人时，拒绝提示会回显本次检测到的 `open_id`，可直接复制到此字段；
  群聊中不会公开该标识。
- `Only handle group @mentions`：建议保持开启，群聊只响应 `@机器人`。
- `Push task status changes`：在任意 run 的 task 状态改变时，向已建立的飞书会话推送 run/task、新旧状态和变更来源。进入 `busy` 显示用户触发消息，离开 `busy` 显示 agent 最后回复，系统迁移显示事件原因。关闭后仍会送达飞书助手对话的直接回复。

保存后重启 AHA，使长连接使用新配置。在 Web 设置的 Integrations 区域点击
“飞书助手”，可在当前页面展开完整控制面板，配置 App ID、App Secret、授权用户、群聊策略、
安全模式、任务状态推送以及飞书助手专用的默认后端、模型、思考深度和代理开关，并查看 SDK 与长连接状态。
后端、模型和思考深度留空时继承全局 AHA 默认值；代理开关使用所选后端已配置的代理地址。
这些设置只影响之后创建的系统管家会话，不修改已有 task。
飞书设置不再出现在全局 AHA Settings 中；
App Secret 不回显，留空保存会保留已有值。控制面板不再重复显示安装命令。
面板使用 `/api/feishu` 脱敏接口，响应不会包含 App Secret。直接填写的密钥会保存在
AHA home 的 `config.json`，请限制该文件和 Web 控制台的访问权限。

## AHA 服务管家 Agent

- 所有非空文本（包括“帮助”“任务”或 `/help`）均原样交给 agent 理解，不做固定意图匹配。
- 飞书只是消息 Channel；真实身份是当前 AHA 实例的系统管家，不是用户项目任务。AHA 会创建一个
  `kind=system`、`system_purpose=service_assistant` 的可见 Run，并为每个飞书会话创建
  `AHA Assistant · DM/Group · <短标识>` 系统 Task。旧版名称为 `Feishu Assistant` 的专用 Run
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
  创建/修改 Memo 及安全 Settings 修改会先生成预览卡片，由用户点击“确认 / 取消”。
  文本兼容模式也可直接回复“确认”或“取消”，无需复制 token。
- 管家路由提交、推送、合并或回滚请求时，只转发用户意图和仓库约束，不指定 backend、model
  或 `Generated-by`。提交身份由目标 Task 当前执行 Agent 的 AHA commit policy 生成；服务端不会让
  显式 `Generated-by:` 进入目标 Task，避免飞书助手自身模型污染提交尾注。
  当前飞书用户消息是授权边界：只要求 `commit` 时，管家不得补充 `push`，服务端也会清除模型擅自加入的
  push 或提交身份指令并正常生成提交确认；用户明确同时要求两者时，必须拆为先提交、后推送的两次独立确认。
  一次性待确认凭证只保存在服务端安全状态中，不出现在文本或卡片 payload；仅原飞书用户和原会话可用一次，
  五分钟过期；确认前目标状态变化时拒绝执行。同一会话产生新预览时，旧卡片自动失效。
- 服务重启/升级、凭据/ACL/认证、sandbox/approval、原始文件写入、KB 审批/同步以及删除类操作不向
  飞书管家开放。需要项目分析或代码修改时，管家应创建普通项目 Task 或向已有 Task 发消息，
  不在 AHA Home 中直接完成项目工作。
- 入站 `message_id` 做 24 小时幂等；单聊按企业与用户隔离，群聊按企业与群隔离，并默认只处理
  `@机器人` 的消息。
- 会话绑定与回复订阅保存在 AHA home 的 `feishu/` 私有目录。消息送达 agent 后，AHA 自动订阅该
  task，把 agent 回复推回原飞书会话。

状态推送开启时，所有 run 的 task 状态变化会推送到已建立助手会话的飞书聊天；
`running` 对外显示为 `busy`，`awaiting_user` 显示为 `awaiting`：

```text
run-a task-001:
status: busy->awaiting
message: agent 最后一条回复
```

开关开启时，agent 最终回复与状态变化合并为一条飞书消息，避免重复推送；关闭时
不发状态通知，但助手对话的直接 agent 回复仍正常送达。

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
首版飞书接入采用单进程本地队列；AHA 重启期间不会接收长连接消息，发送失败会记录为
run event，但暂不提供持久化重试队列。生产使用时还应在飞书后台限制应用可用范围，
定期轮换 App Secret，并保持 `allowed_open_ids` 为最小授权集合。

飞书官方资料：

- [通过 Channel SDK 将 Agent 接入飞书](https://open.feishu.cn/document/mcp_open_tools/integrating-agents-with-feishu/integrate-feishu-channel)
- [处理卡片回调](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks?lang=zh-CN)
- [lark-channel-sdk](https://pypi.org/project/lark-channel-sdk/)
- [网页应用（H5）概述](https://open.feishu.cn/document/client-docs/h5/introduction)
