# 版本记录

## 未发布

- 更新/编辑：`prepare(target_path=...)` 生成整页替换预览（同时展示旧正文、旧标题与旧/新 `sources`/`related`），通过 `PUT /pages/<path>` 提交；默认保留原有元数据，仅显式传入 `sources`/`related` 才改变。commit 前拒绝目标页面消失（`TARGET_MISSING`）、正文变化（`TARGET_CHANGED`）、元数据变化（`TARGET_METADATA_CHANGED`）与读回失败（`TARGET_UNREADABLE`），避免覆盖他人编辑或丢失正文/来源。
- 删除/归档：新增 `lingxi_memory_archive`（`DELETE /pages/<path>`）。证据必须通过项目记录结构校验（schema=1、project、唯一完整 `supersedes` 路径列表、`record_kind`），且只能来自项目页或目标页自身的完整 `test`/`history` 记录；commit 前重新读取目标与证据页，证据消失/被改/不可读分别返回 `EVIDENCE_MISSING` / `EVIDENCE_CHANGED` / `EVIDENCE_UNREADABLE`，所有拒绝路径不调用 DELETE。`core/` 与 `wiki/profile/` 始终受保护。
- 删除 unknown 回执：`receipt` 按 create/replace/delete 分流；删除只做只读对账，目标不存在才记 `deleted_verified`，仍存在返回 `TARGET_STILL_EXISTS`，AUTH/权限/限流/网络错误不解释为已删除，且不重发 DELETE、不新建提案。
- 已知残余：上游没有可验证的条件写（If-Match/版本号）契约，替换只能把最终完整读取放到 PUT 前，无法完全消除“最终读取后、PUT 前”的跨客户端竞态；不声称该窗口已关闭。
- MCP 工具由 8 个增加到 9 个；`prepare` 新增可选 `sources`/`related`；全量测试 506 项通过。

## 0.3.2

DSH 配置兼容：profile 补丁层里的 `!!js` 表达式（如 `path: !!js dshHomePath('session-index.sqlite')`）按不透明标量原样保留，安装时不再因无法解析而拒绝接入 DSH；局部更新只重写目标条目，未触及条目的原始字节不变。DSH 配置解析失败时，报错补充具体原因（标签名与位置）。新增 `test_dsh_js_extension_tag_is_supported` 回归测试。

## 0.3.1

GitHub 首次分发版本。修正维护入口的旧安装提示，补充更新、退出登录与停止连接说明。保留 v0.3.0 的便携运行环境、四端配置、六 MCP 工具与授权新增记忆机制。

## 0.3.0

便携 Python 与依赖；Windows 安装向导；配置预检、备份和写前恢复记录；分账号校验与未决记录保留；不分发本机登录状态。
