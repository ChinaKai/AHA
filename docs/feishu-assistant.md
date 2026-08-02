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

事件订阅选择长连接，只需订阅 `im.message.receive_v1`。应用发布并安装到企业后，可以直接在 Web
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
这些设置只影响之后创建的专用 run 或新会话 task，不修改已有 task。
飞书设置不再出现在全局 AHA Settings 中；
App Secret 不回显，留空保存会保留已有值。控制面板不再重复显示安装命令。
面板使用 `/api/feishu` 脱敏接口，响应不会包含 App Secret。直接填写的密钥会保存在
AHA home 的 `config.json`，请限制该文件和 Web 控制台的访问权限。

## 真实 Agent 对话

- 所有非空文本（包括“帮助”“任务”或 `/help`）均原样交给 agent 理解，不做固定意图匹配。
- 所有飞书聊天固定绑定到名称精确为 `Feishu Assistant` 的独立 run；不存在时自动创建，旧会话绑定
  会自动迁移到该 run，不再跟随最近活动 run。
- 每个飞书会话优先复用该 run 内已绑定的活动 task；首次对话或旧 task 已结束时，会自动创建一个
  `Feishu Assistant · DM/Group · <短标识>` task，并把后续消息发送给它的 `main` agent。短标识由
  会话键稳定生成，不直接暴露 open_id/chat_id；同一会话重建时追加 `#2`、`#3`，避免同名任务。
- 助手 task 的职责是通过自然语言帮助查看或管理 run、task、memo、KB 和 Settings；高风险写操作
  仍应先向用户说明影响并确认。
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
- [lark-channel-sdk](https://pypi.org/project/lark-channel-sdk/)
- [网页应用（H5）概述](https://open.feishu.cn/document/client-docs/h5/introduction)
