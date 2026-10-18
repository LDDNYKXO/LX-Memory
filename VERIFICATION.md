# 验证范围

此公开源码使用合成测试数据，不包含真实用户的记忆、登录状态、客户端配置或历史验收日志。测试命令可在安装匹配的依赖后运行 `python -B -m unittest discover -s tests -v`。

已验证的范围分别记录为：单元与恢复测试、隔离模拟用户配置、本机已登录连接器的只读状态检查。它们不替代所有客户端原生调用，也不代表任意操作系统或真实新电脑均已验收。

MCP 核心保持九个接口（`status/search/read/prepare/commit/receipt/context/validate/archive`）；其中 `prepare` 传 `target_path` 表示整页替换，`archive` 只对已有证据证明可删的页面生成删除预览，删除不可恢复。写回需要用户对精确内容的授权。`context` 分层返回必需背景与个人层，`validate` 在项目交接提交前预演替代链，未通过不写入。账号身份、写前记录、重复提交保护、读回核对和异常结果的只读查证保留。匿名下载不使用 GitHub 凭据；GitHub 下载失败与灵犀登录失效分开报告。

本轮具体检查见 [AUTO-INSTALL-VERIFICATION.md](AUTO-INSTALL-VERIFICATION.md)。仅支持 Windows x64、本地 stdio MCP；仅支持远程 URL 的客户端不在范围内。灵犀上游尚无已验证的稳定公开第三方写入契约，无法确认权限或写入结果时保留限制并如实报告。
