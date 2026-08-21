# 共享浏览器

AHA 的共享浏览器按 `run_id/task_id` 隔离。一个 Browser Bridge 独占一个
浏览器上下文；Web Browser 面板和 agent CLI 都通过同一个
`0600` Unix Socket RPC 访问，因此标签页、登录态和页面状态完全一致。

## 安装与诊断

```bash
python3 -m pip install -e '.[browser]'
python3 -m playwright install chromium
aha browser doctor
```

Playwright 是可选依赖。未安装 Python 包或 Chromium 时，AHA 的其他 CLI/Web
功能不受影响；Browser 面板和 `doctor` 会返回明确的安装提示。onebin 不内嵌
浏览器二进制。Browser Bridge 优先使用当前 AHA Python；Windows 当前解释器
缺少 Playwright 时，会依次检查 `AHA_RUNTIME_PYTHON`、onebin 同目录的
`install-report.json` 和默认 `%USERPROFILE%\.venvs\aha\Scripts\python.exe`，
仅选择已验证可导入 Playwright 的解释器。`doctor` 返回实际使用的
`python_executable` 和是否发生 `python_fallback`。

## 任务配置

新建任务和任务设置只保留四组常用项：浏览器模式、首页、agent 权限和
Profile。Runtime、显示方式、设备模式、下载、上传和代理放在 Browser 顶部
状态栏；Desktop/Mobile 直接切换，其余项目通过齿轮设置面板编辑。
两处入口都调用同一个增量更新接口，修改常用项不会覆盖状态栏中的高级设置。

完整配置结构如下：

```json
{
  "browser_control": {
    "mode": "managed",
    "start_url": "https://www.bing.com/",
    "agent_access": "read_only",
    "runtime": "playwright",
    "profile": "ephemeral",
    "profile_name": "",
    "display": "native",
    "device_mode": "desktop",
    "allowed_hosts": ["example.com", "*.example.org"],
    "downloads": "deny",
    "uploads": "deny",
    "proxy_mode": "custom",
    "proxy_server": "http://127.0.0.1:7890",
    "proxy_bypass": "localhost,127.0.0.1",
    "proxy_username": "",
    "proxy_password": ""
  }
}
```

- `mode`: `off|managed`。
- `start_url`: 默认 `https://www.bing.com/`；任务可显式改为其他 HTTP(S)
  地址。BrowserContext 首次启动、工具栏“＋”新建标签页及关闭最后一个
  标签页后创建替代页时都会使用该地址。User Chrome 恢复出的
  `chrome://newtab` 等内置新标签页也会自动跳转到该地址，避免内置桌面页面
  在 Mobile viewport 中产生横向溢出。
- `agent_access`: `read_only|read_write`；用户在 Browser 面板中的操作不受
  agent 权限影响。
- `runtime`: `playwright|user_chrome`。默认 `playwright` 由 Playwright
  直接启动 Chromium；`user_chrome` 先启动本机可见 Chrome/Chromium，再通过
  仅监听 `127.0.0.1` 的临时 CDP 端点连接。后者不添加 Playwright 的浏览器
  启动自动化标记，适用于要求用户在原生浏览器完成登录的站点。
- `user_chrome` 优先发现系统 Google Chrome、Chromium 或 Edge；都不存在时
  回退到已安装的 Playwright Chromium。该模式需要 AHA 主机有桌面显示，即使
  Browser 面板选择 embedded 也不会静默改成 headless。AHA 使用独立的 task
  或 named user-data-dir，不连接用户日常 Chrome 的默认 profile。
- `profile`: `ephemeral|task|named`。临时 profile 随 bridge 关闭删除；task
  profile 绑定当前 run/task；named 通过 `profile_name` 选择或创建，可跨
  run、项目和任务复用。持久 profile 均保存在 AHA home 的
  `browser/profiles/`，不会进入 run 归档。
- 命名 profile 是独占资源：同一时间只能由一个 Browser Bridge 打开；其他
  任务会收到 `browser_profile_in_use`，原占用 Bridge 退出后即可复用。
- `display`: `native|embedded`，默认 `native`。native 在运行 AHA 的主机
  桌面启动 headed Chromium 原生窗口；Linux 没有 `DISPLAY` 或
  `WAYLAND_DISPLAY` 时通常回退到 embedded。WSL2 若存在
  `/mnt/wslg/.X11-unix/X0`，Bridge 会为子进程安全补入 `DISPLAY=:0`，无需
  AHA 服务预先继承该变量。
- `device_mode`: `desktop|mobile`，默认 `desktop`。它与 AHA 面板本身的宽度
  无关，由 Browser 顶部 Desktop/Mobile 显式选择并持久化。切换设备模式会
  冷重启当前 run/task 的 Browser Bridge，启动前固定 UA、viewport 与触控；
  不会在运行中的页面上热改 viewport。Desktop 固定为 1280×720，Mobile 固定
  为 360×640（9:16）；task/named profile 的登录态保留。
- `allowed_hosts`: 协议兼容字段，空数组允许任意 HTTP(S) 主机；支持精确主机
  和 `*.example.com` 子域通配。Web UI 不再暴露该高级限制。
- `downloads`、`uploads`: 默认拒绝。下载由 bridge 级策略阻断；上传仅在
  `uploads=allow`、agent 具有 `read_write` 权限且使用最新 snapshot 的文件输入
  ref 时，才允许通过 `aha browser upload` 选择一个本地文件。
- `proxy_mode`: `direct|inherit|custom`。`inherit` 由 task 的
  `preferred_proxy_enabled` 开关控制，从 Core Settings 顶层 `proxy` 读取共享
  地址；`codex.proxy.enabled` / `claude.proxy.enabled` 只决定新 task/agent 的默认开关，旧 backend/task/run proxy 仅作
  兼容回退。Chromium 要求继承的 HTTP 与 HTTPS 地址一致。`custom` 支持
  HTTP(S)、SOCKS4、SOCKS5 server，以及 bypass、用户名和密码。
  `user_chrome` 的启动参数不能安全携带代理认证信息，因此该 runtime 只支持
  无用户名/密码的代理；否则返回 `user_browser_proxy_auth_unsupported`。
- 代理密码保存在 task 配置中，但 Web/status 只返回
  `proxy_password_configured`，不返回密码；prompt、事件、审计和 run archive
  也会脱敏。profile、display、downloads 或代理启动签名变更会让 bridge
  自动退出，并由 Web 重连启动新的 BrowserContext；`ephemeral` profile 会
  随之清空。因此从 ephemeral 切到 task 或 named 需要一次 bridge 重启，
  重启后才开始写入所选持久 profile。命名 profile 列表由 `/api/bootstrap`
  的 `browser_profiles` 返回。

对应接口：

- `POST /api/task/<task-id>/browser-control`
- `GET /api/task/<task-id>/browser-session`
- `POST /api/task/<task-id>/browser-session`，body 为
  `{"action":"start|restart|close"}`
- `GET /api/task/<task-id>/browser-bookmarks`
- `POST /api/task/<task-id>/browser-bookmarks`，body 的 `action` 为
  `add|remove|toggle`
- `GET /api/task/<task-id>/browser-io`
- `GET /ws/browser-session?run_id=<run-id>&task_id=<task-id>`

Browser WebSocket 只允许 loopback，或要求 AHA Web auth token。

native 模式下 Browser 标签只显示任务会话状态和“Focus window”，不再复制
浏览器画面；地址栏、书签、历史和标签页由 Chromium 原生窗口提供。embedded
模式按“控制栏、搜索/地址栏、标签页、共享主窗口”固定排列。Browser 与
Chat/Logs/Ctx/Evd 共用页面底部的原 composer 容器，但 composer 只允许 Chat
输入和发送；Browser 直接操作共享画面内的网页真实输入控件，其他页面禁用
composer。搜索/地址栏接受 HTTP(S) URL、域名和普通关键词；关键词使用 Bing
搜索。embedded 地址栏右侧的星标可收藏或取消收藏当前 HTTP(S) 页面；顶部
控制栏的纯图标收藏按钮打开悬浮列表，可在当前标签打开收藏，弹窗不会让
控制栏换行或挤压浏览器画面。列表不提供删除按钮，避免误操作；取消收藏需先
打开目标页面，再次点击地址栏星标。收藏只保存标题和 URL：named profile 按资料跨
run/task 复用，task/ephemeral profile 按当前 run/task 隔离。
native 窗口只显示在 AHA 主机桌面，远程或无桌面环境会回退 embedded。
`focus-window` 只把原生窗口带到前台，不改变 snapshot revision 或触发画面
采集；2.5 秒内的连续聚焦请求会合并，避免窗口反复重绘。

Browser 顶部管理栏不再显示 Desktop/Mobile 切换，新建任务默认使用 Desktop
画面；状态栏仅保留 Start/Close。Close 只关闭当前 run/task 的 Bridge，并写入显式关闭标记，
WebSocket 重连和 agent 命令不会自动拉起；Start 才会恢复。设备模式切换和
高级设置保存仍会在需要时自动冷重启当前任务。task/named profile 数据保留；
ephemeral profile 的登录态、历史和 Cookie 会随关闭或自动重启清理，UI 会在
操作前警告。管理栏动作统一为
固定尺寸的图标按钮，并通过 `title` 和 `aria-label` 保留操作含义。连接状态
固定为最左侧的纯圆点图标：绿色表示运行、红色表示异常、灰色表示停止或尚未
连接，详细状态保留在 `title` 和 `aria-label`；display 和 agent 权限不在
管理栏重复显示。窄屏下管理栏保持单行并压缩图标间距，不再换行。

Start 不只检查 Bridge PID，还会验证 task-scoped Unix Socket 是否仍在监听。
若状态声称 `running` 但 Socket 拒绝连接，说明旧进程卡在关闭清理：AHA 会先
按 run/task 校验进程归属，给予 2 秒优雅退出窗口，必要时终止该失效进程并重建
Bridge。用户无需刷新 AHA 页面。
Start/Restart 的 lifecycle API 会继续等待到新 Bridge 同时满足
`running`、进程存活和 Socket 可连接后才返回，前端不会在 `starting` 阶段提前
建立 WebSocket。

管理栏的齿轮面板编辑 Runtime、显示方式、下载、上传和代理。运行中
保存会把全部高级项作为一次原子更新，并冷重启当前任务的 Bridge；已关闭时只
保存配置，不会擅自启动浏览器。代理密码不会回填到 DOM，留空表示保留现有密码，
也可显式清除。该面板使用相对管理栏定位的悬浮层，展开时不参与 flex 排版，
因此不会撑高管理栏或把齿轮挤到新行。

`user_chrome` 无论 Browser 面板采用 native 还是 embedded，都会在 AHA 主机
打开真实浏览器窗口；`display` 只决定 AHA 页面显示“Focus window”状态卡还是
共享画面。原生窗口中的可信输入继续触发用户抢占。

## 画面与延迟

Web UI 默认固定使用 Desktop：1280×720 逻辑 viewport，使用 1.5 device scale factor
输出 1920×1080 JPEG；Mobile 保持 360×640 CSS viewport，使用 3.0 scale
输出 1080×1920 的后端兼容能力仍保留给旧配置和 API/CLI。清晰度提高不会改变网页 CSS 布局或点击坐标空间，但相比旧
360p 流量与解码成本约增加 9 倍。Mobile 模式同时启用 Android
移动 UA/client hints 与触控模拟，让网页从启动和导航阶段就按移动设备响应式
重排；frame 与点击坐标空间同步更新。CDP metrics 使用固定 CSS viewport，
避免没有 viewport meta 的页面退回移动浏览器默认 980px 布局并产生黑边。
User Chrome 的 CDP emulation session 会随页面保持，Bridge 在主 frame 导航
后重新应用当前配置，因此 reload、跨站导航和后续新建标签页都继续使用所选
模式。每个 Bridge 实例带独立代际标识；重启期间前端清空旧画面，并拒收旧
连接残留帧。AHA 面板的响应式断点不再自动改变设备模式。页面持续
变化或发生用户/agent 操作时，bridge 以约 150ms 间隔采集画面；连续静止后
逐步退避到最多 750ms。native 模式的 Web 会话不订阅连续画面帧。

画面采集不持有 browser action lock，因此较慢的 screenshot 不会排在点击、
键盘或导航之前。新订阅、页面导航、标签页切换和页面 mutation 都会唤醒一次
即时画面采集；纯窗口聚焦不会唤醒。

窄屏 Browser 面板采用单行紧凑控制栏、独立的全宽搜索/地址栏和横向标签栏。
共享主窗口放在独立比例容器中，Desktop 始终为 16:9，Mobile 始终为 9:16；
桌面宽屏同时受可用宽高约束，手机窄屏则按可用宽度确定画面尺寸。手机剩余
高度不足时 Browser 内容区纵向滚动，不能通过压缩画面适配软键盘。未使用区域
使用普通页面背景而非深色画面背景。手机切入 Desktop Browser 时会在同一次
导航手势中聚焦 Browser 专用键盘输入，默认展开软键盘；该默认动作每次进入
Browser 只执行一次。
点击网页可编辑控件后，Bridge 只返回 `accepts_text_input=true`，前端据此
聚焦 Browser frame 内部的不可见键盘输入。默认固定期间点击非输入区域不会
收起键盘；检测到 visual viewport 恢复到键盘打开前高度时，视为用户主动收回，
立即解除固定且不再自动弹回，之后点击网页可编辑控件才重新打开。
Bridge 不读取或返回网页字段内容。Browser 专用输入的普通 `input`、粘贴、IME
`compositionend`、删除和特殊 `keydown` 分别转发为 `text`/`press`，且不会
读取、清空或恢复页面底部 Chat composer。

页面底部 composer 只允许 Chat 使用；Browser、Logs、Final、Hardware、Ctx
和 Evd 保持相同布局但禁用 target、textarea、附件和发送按钮；手机端 `+`
模块导航按钮保持与 Chat 相同的视觉和交互。返回 Chat 时恢复控件原有 disabled
状态，Browser mount、重挂载、关闭或异步 tab 切换均不得
改写 Chat 草稿。同一 task 的 Browser 根节点被替换时只保留键盘捕获状态，不
保留或复制网页文本。
键盘打开期间 AHA 使用打开前的稳定布局高度，
Browser 捕获态的 inset 以键盘打开前的 layout viewport 为基准计算，兼容
Android 同时缩小 `innerHeight` 与 `visualViewport.height` 的行为。普通
Chat/表单不使用该稳定基线，只计算当前 layout viewport 未覆盖的区域，避免
动态 `100dvh` 已缩小时又把 composer 二次上移。AHA 的 body、顶栏和其他模块
不做位移，共享帧也不单独平移；禁用的 Chat composer 不随 Browser 键盘上移，
Browser 内容区可滚动到被键盘遮挡的部分。键盘打开时触摸共享画面会阻止宿主
页面默认的焦点切换，并在同一手势中把焦点保持在 Browser 专用键盘输入，因此点击
网页不会收起输入法。退出 Browser 时立即清理键盘 inset 和 body 状态。
触屏拖动会映射为远端页面滚动；节流期间累积完整位移，`touchend` 强制补发
剩余 delta，避免快速或连续滑动时丢步；
触摸轻点直接在 `touchend`/`pointerup` 转发，且会抑制随后合成的重复 click。
共享画面通过 `object-fit: contain` 完整显示，点击坐标按实际可见内容区域映射，
不会把留白算入网页坐标。冷重启后只有匹配新 `instance_id` 的首帧完成解码，
画面输入才恢复；映射同时使用该帧的逻辑 viewport 和图片实际像素比例，不能
沿用旧设备帧。

## Agent CLI

Windows onebin 不直接以无扩展名 `aha` 文件暴露给 agent shell。AHA 会生成并
优先加入 PATH 的 `backend-bin\aha.cmd` 和 `python3.cmd`，由当前 AHA 的控制台
Python 执行同一个 onebin，避免 Windows 弹出“选择打开方式”，也避免 agent
误用系统 Python 后把 Browser Bridge 判定为缺少 Playwright。

```bash
aha browser status <run-id> <task-id>
aha browser tabs <run-id> <task-id>
aha browser snapshot <run-id> <task-id>
aha browser navigate <run-id> <task-id> https://example.com
aha browser click <run-id> <task-id> '3:b12'
aha browser fill <run-id> <task-id> '3:b15' 'hello'
aha browser upload <run-id> <task-id> '3:b18' ./artifact.zip
aha browser press <run-id> <task-id> Enter
aha browser focus-window <run-id> <task-id>
aha browser screenshot <run-id> <task-id> --output page.png
```

元素 ref 与 snapshot revision 绑定。页面变化后使用旧 ref 会收到
`stale_ref`，应重新 snapshot。`upload` 还要求任务配置 `uploads=allow`，目标 ref
必须是 `input type=file`，本地文件路径必须存在。用户操作会提升 control epoch；
排队中的 agent 写操作会收到 `control_preempted`。

## 安全与数据

- Bridge 层再次校验 agent 读写权限和 host allowlist，不能依赖 prompt 或 UI。
- 页面文本视为不可信内容。prompt 明确要求 agent 不把网页文本当作指令。
- snapshot 不返回密码输入值，也不暴露 Cookie、localStorage 或 sessionStorage。
- `browser_io.jsonl` 只记录动作元数据；不记录输入文本，URL 只保留脱敏 origin。
- 连续画面帧不落盘。只有显式 `screenshot` 命令写入 task
  `browser_artifacts/`。
- task 进入 terminal 状态后 bridge 自动关闭；无人连接的 detached bridge
  会在空闲超时后自回收。

共享浏览器不是安全沙箱。启用 `read_write`、放开主机或传输能力前，仍需按任务
风险判断外部副作用。

## 验证

```bash
python3 -m pytest tests/test_browser_external.py tests/test_browser_bridge.py tests/test_web_task_api.py \
  tests/test_chat_prompt.py tests/test_frontend_static.py
```
