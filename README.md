# AHA

[简体中文](README.md) | [English](README.en.md)

AHA 是 `agent help agent`。

AHA 是一个本地 CLI 和 Web UI，用来协调按 task 隔离的 AI agent。它把
状态保存在 AHA home 中，用 run 和 task 组织工作，并可以从浏览器面板启动
Codex 或 Claude 后端 agent。

默认数据目录是 `~/.aha`。可以用 `--home <path>` 指定其他 AHA home。

## 从源码启动

直接从源码目录启动 Web UI：

```bash
PYTHONPATH=src python3 -m aha_cli ui --host 127.0.0.1 --port 8788
```

打开：

```text
http://127.0.0.1:8788
```

首次打开时，UI 会显示初始化表单。保存后才会在选定的 AHA home 中写入
`.aha/config.json`。之后先创建 run，再在 run 里创建 task。

## 打包 Onebin

从源码目录打包单文件 zipapp：

```bash
python3 scripts/build_onebin.py --output dist/aha
```

## 使用 Onebin 启动

在有 Python 3.10+ 的机器上直接运行：

```bash
./dist/aha --help
./dist/aha --home ~/.aha ui --host 0.0.0.0 --port 8788
```

onebin 包含 AHA Python 模块和浏览器静态文件。外部 agent CLI，例如
`codex` 和 `claude`，仍需要在目标机器上安装并完成认证。

onebin 面板启动托管 backend 时，会通过同一个 onebin artifact 启动子 AHA
backend 命令，不要求目标机器额外安装可 import 的 `aha_cli` Python 模块。

## 安装 / 升级 User Systemd 服务

不需要克隆源码时，可以直接使用 GitHub Release 里的安装脚本和 onebin：

```bash
curl -fsSL -o /tmp/install_aha.sh \
  https://github.com/ChinaKai/AHA/releases/latest/download/install_user_service.sh
bash /tmp/install_aha.sh
```

默认会下载 `ChinaKai/AHA` latest release 的 `aha` artifact，安装到
`~/.local/bin/aha`，并安装、启动 user systemd 服务。也可以安装指定版本或
本地 artifact：

```bash
bash /tmp/install_aha.sh --version v0.1.0
bash /tmp/install_aha.sh --artifact ./dist/aha
```

开发时如需从当前源码构建 onebin，再安装、启动 user systemd 服务：

```bash
scripts/install_user_service.sh --build-from-source
```

默认服务命令是：

```text
aha --home ~/.aha ui --host 127.0.0.1 --port 8788 --auth-token-file ~/.aha/web-token
```

安装脚本默认启用 Web UI token 登录，并在 AHA home 下生成或复用
`web-token`。默认登录 token 在 `~/.aha/web-token`，可用
`cat ~/.aha/web-token` 查看；如果传了 `--aha-home`，就在对应目录下找。

常用参数：

```bash
scripts/install_user_service.sh --port 8788 --aha-home ~/.aha
scripts/install_user_service.sh --port 8788 --run-id <run-id>
```

Web UI 的升级按钮会调用已安装 onebin 内置的
`aha service upgrade-user`，按 systemd unit 中记录的 release 来源下载新
artifact 并重启 `aha.service`，不再需要源码 checkout。

查看服务状态：

```bash
systemctl --user status aha.service
journalctl --user -u aha.service -f
```

如果希望服务在用户登录前也能启动，开启 lingering：

```bash
sudo loginctl enable-linger "$USER"
```

## 自动发布

推送 `v*` tag 会触发 `.github/workflows/release.yml`：构建 onebin、运行
release smoke、生成 `SHA256SUMS`，并把 `aha` 与 `install_user_service.sh`
发布到 GitHub Release。

更详细的设计说明在 `docs/` 目录。
