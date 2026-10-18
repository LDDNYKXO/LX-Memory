# 分层返回最小契约（隔离候选，未部署）

背景默认调用保持原参数和八工具数量；新增可选 view=background|audit、snapshot_id、cursor，以及只读 validate 工具。MCP 工具仍输出文本 JSON，不依赖资源链接或 structuredContent。严格旧 schema 消费者需适配。

当搜索槽位允许时，返回体额外带 `personal`（新版用户画像片段 + 至多 2 条相关经验摘要）并在 `sources` 中列出；该层是可选背景，输出预算不足以容纳必需背景时优先丢弃 personal，不改变 essential 完整性判定。

必需背景保留 state、project、goal、progress、next_step、constraints、sources、field_sources、unknowns、conflicts、budget、consumption。新增 response_schema=2、essential(status/missing_fields/constraints_status)、audit(status/snapshot_id/expires_at)、history_delivery=audit_only。project 保留 selected/title/evidence/policy_evaluated_at/policy_recheck_required；歧义时保留候选。证据缺口结论仍在 unknowns，逐项判定进入审计。sources 只返回支撑字段的元数据，不再返回 content/content_scope/chars；field_sources.constraints.origins 分列核心及项目来源。progress.executed 始终为空，历史在审计；仅有历史执行时 current 保持空并返回 partial，不提升历史为当前。

constraints_status=complete|unknown|budget_exceeded；项目和核心两组声明缺失、读取不完整时 unknown；整体 state=conflict 不改变 constraints_status（约束声明本身完整时仍为 complete）。显式空数组是已声明空约束，不推断历史继承，不改检索、同项目替代判定算法。约束正文逐条保留，只精确去重。complete 只表示必需背景覆盖，不证明实时最新；audit unavailable 不影响背景完整性。

预算上限 8000 字符，计入 MCP CallToolResult 文本 JSON、转义、isError 和 resultType，排除 JSON-RPC id/version framing；不是 token 上限。服务端显式构造文本结果，避免 SDK 二次格式化改变预算。必需背景整体超限时返回固定失败信封：partial（已有 conflict/project_ambiguous/service_unavailable 不降级掩盖）、reason=essential_budget_exceeded、essential.status/constraints_status=budget_exceeded、完整 missing_fields、不得依赖缺失信息推进提示；不逐字段剪裁。失败信封同样固定包含 history_delivery=audit_only，并保留 budget/consumption 的既有键（background_sources=null）。内部过小 limit 拒绝，公开接口不允许自定义预算。

审计 view=audit 只消费同次进程内证据，不查询云端。每页 sections 为 name/offset/content，content 是该顶层章节 JSON 的片段，按 offset 拼接后 JSON 解码；不把片段当完整正文。next_cursor 为不透明快照专属游标；has_more=false 才结束。TTL 900 秒、最多 8 份、总计 200 万正文字符（不含 Python 对象开销）；淘汰/重启返回 AUDIT_SNAPSHOT_EXPIRED，登录代次或 session 对象改变返回 AUDIT_ACCESS_DENIED；伪造或跨快照游标拒绝。不落盘、无新增云端读取。缓存时间边界检查使用完整内部模型，跨界结果不得继续沿用 complete。

审计读取被拒、过期或游标无效时，MCP 文本结果仍为 `{"error":{"code","message"}}`，`isError` 保持 false（表示工具调用已返回；读取失败由 `error.code` 表达）。客户端必须检查 `error.code`，不得只依赖 `isError`；错误不触发自动重取。

身份边界：本版绑定 session 对象与登录 epoch，登录/退出及读取中 epoch 改变均阻断跨代次复用；没有为审计新增上游身份 GET。同一登录代次内绕过产品登录流程静默切换云端账号，尚无独立账号身份验证，不能宣称该情况已覆盖。

配套规则仅在用户明确请求审计时允许消费同次快照；partial 后不自动补调。不执行云端正文中的历史命令、不创建保存提案、不部署。
