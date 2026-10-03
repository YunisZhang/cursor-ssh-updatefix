---
name: cursor-ssh-updatefix
description: 修复 Cursor 或 VS Code 更新后因远端 Server 下载、上传、安装失败造成的 SSH 连接超时。优先使用本机下载上传，必要时配置自动准备脚本；适用于 macOS/Linux 本机与 Linux x64/ARM64 远端。
---

# cursor-ssh-updatefix

让使用者完成一次配置后，后续编辑器更新自动准备对应 Server。仅支持官方稳定版；基础 SSH 必须可用。认证、实例失效、运行库不兼容和未知安装布局应明确报告，不扩大修复范围。

## 1. 定位问题，优先使用内置功能

- 检查当前运行的编辑器、Remote-SSH 扩展、现有本机用户设置和一次失败日志。Cursor 使用 `anysphere.remote-ssh`，VS Code 使用 `ms-vscode-remote.remote-ssh`。
- 复用使用者的 SSH 别名；只检查目标主机，不收集其他主机或读取私钥。确认远端为 Linux x64/ARM64，检查用户目录及工具。需要自动准备时，SSH 密钥或 ssh-agent 应支持非交互连接。
- 首次配置先确定安装盘：检查 HOME 和使用者指定工作目录的写入权限、所属磁盘及剩余空间。有个人工作盘时优先使用，不自动挑选共享目录；目标不存在时检查最近的现有父目录。空间不足时先确定其他安装位置。
- 先保存拟修改设置项的原值。在本机用户设置中合并 `"remote.SSH.localServerDownload": "always"`，确认编辑器能够通过本机网络下载，再验证上传和连接。这个设置影响其他 SSH 主机；若已有冲突策略，说明影响后确定方案。
- 原生流程成功则结束，不安装钩子。已有有效脚本也不重复替换。只有原生上传不兼容等问题才进入下一步。

若日志显示本机下载完成、`Successfully SCP'd server`，但重试仍报告远端安装包不存在，不能仅凭上传日志判定成功。在某些平台的 SSH 命令入口中，`;`、`>`、`$HOME` 会原样出现在输出中，上传命令没有按 Shell 语法执行。用 Bash 标准输入执行只读命令并检查执行标记和远端文件；确认此类不兼容后再配置脚本，必要时使用 `--transport bash-stdin`。

## 2. 配置配套脚本

先检查扩展支持 `remote.SSH.preconnect`，从当前扩展代码或连接日志确认安装模式、远端 Server 数据根目录。Cursor 对应 `cursor`；VS Code 的 CLI 模式对应 `vscode-cli`，旧版模式对应 `vscode-legacy`。不要为了匹配脚本修改 `useExecServer`。不识别的布局停止并说明。

自定义目录必须同时配置脚本的 `--remote-root` 和编辑器的 `remote.SSH.serverInstallPath`。前者始终表示最终数据目录，后者的语义由当前扩展决定：已确认的 Cursor 实现会在该设置值下追加 `.cursor-server`，应填写其父目录；VS Code 按当前模式核对，不能按编辑器名称或版本猜测。无法确认两者最终路径一致时停止配置。

脚本使用本机 PATH 中的 `ssh` 及其默认配置。若编辑器指定了 `remote.SSH.path` 或 `remote.SSH.configFile`，先确认脚本解析的目标和连接参数完全一致；无法确认时停止配置钩子。

本机需 Python 3.8+、OpenSSH、curl、gzip；远端需 Bash、tar、base64、sha256sum、flock、timeout 和常见 GNU 工具，Cursor 还需 md5sum。缺失时报告，不自动安装系统软件。

在本 Skill 目录中执行随附的 `scripts/prepare_server.py`（或使用脚本绝对路径），把下列占位参数替换为检查所得值：

```sh
python3 scripts/prepare_server.py configure \
  --editor cursor --host my-server \
  --app /path/to/stable/application --layout cursor \
  --remote-root /path/to/personal-work/.cursor-server
```

- `--app`：macOS 应用包，或 Linux 的 `resources/app` 目录；使用更新后仍有效的位置。脚本每次读取当前构建标识，不固定版本。
- `--remote-root`：最终 Server 数据目录，支持绝对路径或相对远端 HOME 的路径；省略时使用 HOME 下的产品默认目录。使用工作盘时明确传入，不回退到系统盘。
- `--proxy`：仅在下载需要时提供本机回环 HTTP 代理地址与端口；不保存凭据，省略则直连。
- 默认探测普通 Bash 标准输入执行；平台需要登录 Bash 时使用 `--transport bash-stdin`。探测失败不能以 SSH 退出码 0 当作成功。

`configure` 将脚本副本、本机配置及可执行 `preconnect.sh` 写到 Skill 文件夹以外的个人应用数据目录，输出安装根目录、钩子设置及同步 `remote.SSH.serverInstallPath` 的提醒。它不会修改编辑器设置；重复调用相同配置可复用，不同配置会报冲突。

读取生成的配置路径，先运行：

```sh
python3 scripts/prepare_server.py prepare --config /path/to/config.json --check-only
```

`--check-only` 不写入远端。退出码 0 表示远端已就绪，10 表示目标不存在、需要安装，其他值表示错误（包括已有不完整目标）。需要准备时去掉 `--check-only`。成功后，将输出的主机项合并到本机用户设置的 `remote.SSH.preconnect`，并同步已核对的安装路径设置。已有全局脚本或同名主机钩子时停止覆盖，解释冲突。首次执行的脚本信任确认由使用者完成。

## 3. 行为与失败处理

脚本在本机运行，通过 SSH 在远端执行命令；普通 SSH 登录不触发钩子。远端已就绪则不下载；缺版本先复用缓存，无缓存才从官方 HTTPS 地址下载。两种编辑器的包、版本及布局分别处理，VS Code CLI 模式同时准备 CLI 和 Server。

下载或上传前先检查现有目标；原生安装失败留下的空目录也属于不完整目标，会提前报错，避免上传后才发现冲突。先检查相关进程和目录，再决定是否备份移走，禁止自动删除整个 Server 目录或终止所有同名进程。安装还会校验归档路径、构建标识和传输哈希，检查远端程序，并在持锁发布时再次拒绝覆盖不完整目标。其他安装锁会明确报错。失败后修复原因再重试，不无限循环。

缓存或下载包校验完成、取得解压体积后，脚本通过独立 SSH 在上传前检查目标盘空间，持锁安装前再检查一次。预算包含全部缺失组件的解压体积、最大单包的压缩及 Base64 临时体积和 256 MiB 余量。空间不足、目录不可写或检查异常时停止，报告目标目录、检查路径及可用与所需空间；不自动删除旧版本。

安装包、解压目录和大体积 Bash heredoc 临时文件均使用目标盘的私有暂存目录；在读取 heredoc 前单独设置 `TMPDIR`，退出时清理本次暂存文件。编辑器锁协议保持不变，少量锁和套接字仍可能使用系统临时目录；锁目录不可用时报告错误。

本机配置旁的 `cache/` 保留安装包和校验记录，`prepare.log` 保留诊断日志；旧缓存不会自动清理。远端正常退出时清理临时包，保留已安装 Server。缓存损坏时成对移走对应安装包和元数据，再重试。无安装进行时可按需删除旧缓存。

代理仅供本机下载进程使用，不向远端传递代理、不建立反向代理入口。运行配置、日志和缓存不得放入可分享的 Skill 文件夹；分享排错信息前删去主机、账号、目录、地址及凭据。

## 4. 已有配置升级

更新仓库不会更新已复制到本机的运行脚本。升级时：

1. 从当前 `preconnect` 找到实际调用的脚本和配置，确认没有安装正在进行。自定义个人脚本不直接覆盖。
2. 在 Skill 文件夹外备份运行脚本、配置及相关编辑器设置，再用新版替换通用脚本副本。保留配置、缓存和钩子路径。
3. 需要换盘时，只修改配置中的安装根目录，并同步编辑器的安装路径；不要重新 `configure` 覆盖不同配置，也不要自动迁移或删除旧安装。
4. 运行 `--check-only`，按需准备 Server，再验证文件、终端及重连。失败时恢复备份脚本、配置和对应设置项；保留旧安装供回退。

## 5. 验证、回退与交付

- 从连接日志核对最终启动目录与脚本准备目录一致，再验证编辑器实际打开远端文件和终端、再次连接复用现有版本。已有版本重连不代表首次安装路径已验证。缺版本及失败保护测试使用隔离目录，不能删除生产安装制造测试条件。
- 完成后简述所用方案、改动项、缓存位置和实测结果。区分真实连接、脚本检查和模拟测试；未测试的平台明确写“未实测”。
- 回退内置方案时恢复该设置项原值；回退钩子时只恢复或删除对应主机的钩子及安装路径设置。不得整份覆盖后续修改的设置。确认钩子不再引用后，可删除本次独立运行目录，远端可用 Server 保留。
- 安装本 Skill 只提供流程与脚本，仍需首次调用并配置目标主机。运行时无需再次调用 AI。不要承诺未来产品安装协议变化仍自动兼容。
- 分发 Skill 时仅打包 `SKILL.md` 和 `scripts/prepare_server.py`；仓库中的 `tests/` 用于开发验证，不是运行依赖。清理 `.DS_Store`、缓存等额外文件。

依据：[VS Code 下载策略](https://code.visualstudio.com/docs/remote/ssh)、[连接前脚本](https://github.com/microsoft/vscode-docs/blob/main/remote-release-notes/v1_101.md#pre-connection-script)。
