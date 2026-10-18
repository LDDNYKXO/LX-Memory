# 版本记录

## 未发布

- 更新/编辑：`prepare(target_path=...)` 生成整页替换预览（同时展示旧标题与旧正文），通过 `PUT /pages/<path>` 提交；commit 前拒绝目标页面消失（`TARGET_MISSING`）、预览后被改动（`TARGET_CHANGED`）与读回失败（`TARGET_UNREADABLE`），避免覆盖他人编辑或丢失正文。
- 删除/归档：新增 `lingxi_memory_archive`（`DELETE /pages/<path>`），只对“已被另一条项目记录的 `supersedes` 明确替代”或自身声明 `record_kind=test/history` 的页面生成删除预览；普通页面返回 `TARGET_NOT_SUPERSEDED`，`core/` 与 `wiki/profile/` 始终受保护。删除不可恢复，提交后读回确认 404，并以 `evidence` 字段说明可删依据；可用 `superseded_by` 显式指定替代页，避免依赖搜索命中。
- MCP 工具由 8 个增加到 9 个；全量测试 491 项通过。

## 0.3.2

DSH 配置兼容：profile 补丁层里的 `!!js` 表达式（如 `path: !!js dshHomePath('session-index.sqlite')`）按不透明标量原样保留，安装时不再因无法解析而拒绝接入 DSH；局部更新只重写目标条目，未触及条目的原始字节不变。DSH 配置解析失败时，报错补充具体原因（标签名与位置）。新增 `test_dsh_js_extension_tag_is_supported` 回归测试。

## 0.3.1

GitHub 首次分发版本。修正维护入口的旧安装提示，补充更新、退出登录与停止连接说明。保留 v0.3.0 的便携运行环境、四端配置、六 MCP 工具与授权新增记忆机制。

## 0.3.0

便携 Python 与依赖；Windows 安装向导；配置预检、备份和写前恢复记录；分账号校验与未决记录保留；不分发本机登录状态。
