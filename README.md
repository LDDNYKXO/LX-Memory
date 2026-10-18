# 灵犀记忆 MCP

让支持本地 stdio MCP 的 Agent 共用当前用户授权的灵犀云端记忆。默认提供通用 MCP 接口，不要求安装任何指定品牌的 Agent。当前运行环境为 Windows 10/11 x64 和 Microsoft Edge。

## 开始使用

把 **[Agent 安装说明](AGENT-INSTALL.md)** 链接交给你正在使用的 Agent，请它按说明安装并接入灵犀记忆。

1. 先检查本机是否已有连接器和有效登录。
2. 已安装就复用；未安装则自动获取源码、准备运行环境。
3. 通过当前 Agent 的 MCP 设置入口注册标准配置。
4. 已登录直接使用；未登录才提示本人完成正常灵犀登录。

用户无需手动下载、解压产品安装包。源码公开，下载不需要 GitHub 账号、令牌或 GitHub CLI。仅在本机正常登录灵犀；记忆和登录状态不会上传到 GitHub。安装后的本机连接检查不依赖 GitHub。

## 通用接入

默认选择 `Generic`，输出标准 MCP 的 `command`、`args`、`env` 配置，由 Agent 通过客户端支持的入口注册。不同客户端的配置入口可能不同；支持 MCP 不代表都支持自动改配置，也不代表所有客户端都已验收。

[可选客户端适配](CLIENT-ADAPTERS.md) 可帮助已知客户端自动写配置，不改变同一个 MCP 核心。只支持远程 URL 的客户端暂时不能直接使用本地 stdio 服务；其它操作系统尚未适配。

## 日常使用

可以说“查询灵犀记忆，继续上次项目”，或“请记住：……”。先检查连接，再读取相关记忆。只有用户明确要求保存时才生成预览、按授权提交、读回验证；普通聊天不自动保存。写入默认新增 wiki 条目；编辑既有页面走 `prepare(target_path=...)` 的整页替换预览，删除只允许已被明确替代或自身声明为测试/历史记录的页面，`core/` 与 `wiki/profile/` 始终受保护。

九个 MCP 工具为 `status`、`search`、`read`、`prepare`、`commit`、`receipt`、`context`、`validate`、`archive`。`context` 分层返回必需背景，并在可用时附带 profile/lessons 个人层；`validate` 在项目交接提交前模拟替代链，只有验证通过才应 commit；`prepare` 传 `target_path` 表示整页替换，默认保留原页面的 `sources`/`related`，仅在显式传入时改变，并在预览中同时给出旧正文、旧元数据与新元数据；`archive` 只对通过项目记录结构校验、且已被明确替代（或自身为 `test`/`history` 记录）的页面生成删除预览，提交前会重新读取目标与证据页再确认，删除不可恢复。支持 Skill 的客户端可以另外安装配套 Skill；MCP 本身不依赖客户端具备 Skill 功能。灵犀桌面助手不必一直开着。

同一 Windows 用户的多个 Agent 复用一份本产品登录。换电脑首次登录一次；登录失效时按正常流程重新登录。首次开启保存功能仍需本人主动选择安装向导中的验证步骤，本入口不自动写入测试记忆。

[公开范围与隐私检查](PUBLICATION-SCOPE.md) · [当前验证范围](VERIFICATION.md) · [源码安装验证说明](AUTO-INSTALL-VERIFICATION.md)
