# cursor-ssh-updatefix

修复 Cursor / VS Code 更新后，因远端 Server 下载、上传或安装失败造成的 SSH 连接超时。需要先确认普通 SSH 登录正常。

这个 Skill 先尝试编辑器自带的本机下载、上传功能；遇到不兼容的 SSH 入口时，再配置连接前脚本。脚本读取当前编辑器版本，复用已有安装和本机缓存，缺少时下载并上传安装。配置一次后，后续连接由编辑器自动调用。

可将 Server 和大体积安装临时文件放到个人工作盘，配置时同步编辑器的安装位置。脚本在上传前检查空间，发现空间不足或不完整安装目录时停止并报错，不覆盖已有文件。可选代理只用于本机下载，不向远端开放。

## 使用

```bash
git clone https://github.com/YunisZhang/cursor-ssh-updatefix.git
cd cursor-ssh-updatefix
```

用有本机文件和终端权限的 AI 编程工具打开该目录，发送以下内容，替换编辑器名称、SSH 别名和个人工作目录：

```text
请按 SKILL.md 排查更新后的 SSH 连接问题并完成配置。
编辑器：Cursor
SSH 主机别名：my-server
远端个人工作目录：/path/to/personal-work
```

首次运行脚本时，按编辑器提示确认。已有用户更新仓库后，还需备份并更新本机运行副本；配置、升级、缓存清理和回退步骤见 [SKILL.md](SKILL.md)。

## 支持范围与测试

支持 macOS / Linux 本机、稳定版 Cursor / VS Code，以及 Linux x64 / ARM64 远端。脚本需要 Python 3.8+ 和系统工具，依赖列表见 [SKILL.md](SKILL.md)。认证失败、实例不可用、运行库不兼容和未知安装布局需另行处理。

目前为试用版：

- 早期版本已验证 macOS → Linux ARM64 的官方 Server 包安装、缓存复用和本地回归测试，有使用者完成 Cursor 远端文件和终端连接的反馈。
- 回归测试覆盖自定义目录、空间检查、失败保护及配置升级；脚本模拟测试不能替代编辑器连接测试。
- 新版通用脚本的更新后连接与重连、Linux 大 heredoc 临时文件落盘、VS Code 编辑器连接、Linux 本机和 x64 远端尚未实测。

## 反馈与测试

可提交 Issue 或 PR。反馈时附上编辑器、扩展版本、系统、远端架构及日志，删去个人目录、主机地址和凭据。

本地回归测试不需要 SSH 连接：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```
