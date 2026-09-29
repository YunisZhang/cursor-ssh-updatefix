---
name: cursor-ssh-updatefix
description: 修复 Cursor 或 VS Code 更新后因远端 Server 下载、上传、安装失败造成的 SSH 连接超时。优先使用本机下载上传，必要时配置自动准备脚本；适用于 macOS/Linux 本机与 Linux x64/ARM64 远端。
---

# cursor-ssh-updatefix

让使用者完成一次配置后，后续编辑器更新自动准备对应 Server。仅支持官方稳定版；基础 SSH 必须可用。认证、实例失效、运行库不兼容和未知安装布局应明确报告，不扩大修复范围。

## 1. 定位问题，优先使用内置功能

- 检查当前运行的编辑器、Remote-SSH 扩展、现有本机用户设置和一次失败日志。Cursor 使用 `anysphere.remote-ssh`，VS Code 使用 `ms-vscode-remote.remote-ssh`。
- 复用使用者的 SSH 别名；只检查目标主机，不收集其他主机或读取私钥。确认远端为 Linux x64/ARM64，检查用户目录、工具及空间。需要自动准备时，SSH 密钥或 ssh-agent 应支持非交互连接。
- 先保存拟修改设置项的原值。在本机用户设置中合并 `"remote.SSH.localServerDownload": "always"`，确认编辑器能够通过本机网络下载，再验证上传和连接。这个设置影响其他 SSH 主机；若已有冲突策略，说明影响后确定方案。
- 原生流程成功则结束，不安装钩子。已有有效脚本也不重复替换。只有原生上传不兼容等问题才进入下一步。

## 2. 配置配套脚本

先检查扩展支持 `remote.SSH.preconnect`，从日志确认安装模式、远端 Server 数据根目录。Cursor 对应 `cursor`；VS Code 的默认 CLI 模式对应 `vscode-cli`，旧版模式对应 `vscode-legacy`。不要为了匹配脚本修改 `useExecServer`。不识别的布局停止并说明。

脚本使用本机 PATH 中的 `ssh` 及其默认配置。若编辑器指定了 `remote.SSH.path` 或 `remote.SSH.configFile`，先确认脚本解析的目标和连接参数完全一致；无法确认时停止配置钩子。

本机需 Python 3.8+、OpenSSH、curl、gzip；远端需 Bash、tar、base64、sha256sum、flock、timeout 和常见 GNU 工具，Cursor 还需 md5sum。缺失时报告，不自动安装系统软件。

在本 Skill 目录中执行随附的 `scripts/prepare_server.py`（或使用脚本绝对路径），把下列占位参数替换为检查所得值：

```sh
python3 scripts/prepare_server.py configure \
  --editor cursor --host my-server \
  --app /path/to/stable/application --layout cursor
```

- `--app`：macOS 应用包，或 Linux 的 `resources/app` 目录；使用更新后仍有效的位置。脚本每次读取当前构建标识，不固定版本。
- `--remote-root`：日志显示自定义安装根目录时必须提供，支持绝对路径或相对远端 HOME 的路径。
- `--proxy`：仅在下载需要时提供本机回环 HTTP 代理地址与端口；不保存凭据，省略则直连。
- 默认探测普通 Bash 标准输入执行；平台需要登录 Bash 时使用 `--transport bash-stdin`。探测失败不能以 SSH 退出码 0 当作成功。

`configure` 将脚本副本、本机配置及可执行 `preconnect.sh` 写到 Skill 文件夹以外的个人应用数据目录，并输出待合并的设置。它不会修改编辑器设置；重复调用相同配置可复用，不同配置会报冲突。

读取生成的配置路径，先运行：

```sh
python3 scripts/prepare_server.py prepare --config /path/to/config.json --check-only
```

退出码 0 表示远端已就绪，10 表示缺少版本，其他值表示错误。需要准备时去掉 `--check-only`。成功后，将输出的主机项合并到本机用户设置的 `remote.SSH.preconnect`。已有全局脚本或同名主机钩子时停止覆盖，解释冲突。首次执行的脚本信任确认由使用者完成。

## 3. 行为与失败处理

脚本在本机运行，通过 SSH 在远端执行命令；普通 SSH 登录不触发钩子。远端已就绪则不下载；缺版本先复用缓存，无缓存才从官方 HTTPS 地址下载。两种编辑器的包、版本及布局分别处理，VS Code CLI 模式同时准备 CLI 和 Server。

安装先校验归档路径、构建标识和传输哈希，再检查远端程序并发布。已有不完整目标或其他安装锁会明确报错；先检查相关进程和目录，再决定是否备份移走，禁止自动删除整个 Server 目录或终止所有同名进程。失败后修复原因再重试，不无限循环。

本机配置旁的 `cache/` 保留安装包和校验记录，`prepare.log` 保留诊断日志；旧缓存不会自动清理。远端正常退出时清理临时包，保留已安装 Server。缓存损坏时成对移走对应安装包和元数据，再重试。无安装进行时可按需删除旧缓存。

代理仅供本机下载进程使用，不向远端传递代理、不建立反向代理入口。运行配置、日志和缓存不得放入可分享的 Skill 文件夹；分享排错信息前删去主机、账号、目录、地址及凭据。

## 4. 验证、回退与交付

- 验证编辑器实际打开远端文件和终端，再次连接复用现有版本；已有版本重连不代表首次安装路径已验证。缺版本及失败保护测试使用隔离目录，不能删除生产安装制造测试条件。
- 完成后简述所用方案、改动项、缓存位置和实测结果。区分真实连接、脚本检查和模拟测试；未测试的平台明确写“未实测”。
- 回退内置方案时恢复该设置项原值；回退钩子时只恢复或删除对应主机项。不得整份覆盖后续修改的设置。确认钩子不再引用后，可删除本次独立运行目录，远端可用 Server 保留。
- 安装本 Skill 只提供流程与脚本，仍需首次调用并配置目标主机。运行时无需再次调用 AI。不要承诺未来产品安装协议变化仍自动兼容。
- 分享前确认文件夹仅含 `SKILL.md` 和 `scripts/prepare_server.py`，清理 `.DS_Store`、缓存等额外文件。

依据：[VS Code 下载策略](https://code.visualstudio.com/docs/remote/ssh)、[连接前脚本](https://github.com/microsoft/vscode-docs/blob/main/remote-release-notes/v1_101.md#pre-connection-script)。
