# 客户端接入能力与证据

产品核心为通用 stdio MCP。Generic 供其它兼容客户端接入，不以品牌限定产品范围；远程 URL 客户端及非 Windows 系统另行适配。

机制分为 `mcp_tool`、`rules_injection`、`lifecycle`。表中自动触发是适配设计能力，不代表本机已配置、已加载或原生验收通过。配置存在不能代替客户端原生调用验收。

| 宿主 | 机制 | 自动触发 | 入口 | 安装参数 | 已验收证据 | 真实限制 |
| --- | --- | --- | --- | --- | --- | --- |
| Codex | `rules_injection` | 是 | `~/.codex/AGENTS.md` | `-Clients Codex -Apply` | 10-16 桌面新会话 01a140aa-fd69-77f2-9d90-c44b0db76f7b；一次云端 context，48.280 秒 | 已按来源日期回答；云端记录停留在 10-15，不能证明最新现状；CLI 不替代桌面 |
| DSH | `lifecycle` | 是 | `agent/pre-step` 插件 | MCP: `-Clients DSH -Apply`；另配置 lifecycle | 首步适配有 Node 合成用例；本轮桌面捕获受阻，零次提交 | 已验证配置改后精确回退；未保留启用；全局规则不充当生命周期证据 |
| ZCode | `mcp_tool` | 是 | `~/.zcode/AGENTS.md` | `-Clients ZCode -Apply` | 无（本轮未做原生验收）；界面无可操作对话，零次提交 | 工具可用与服务说明被消费分别记录，不从配置推断自动接续 |
| WorkBuddy | `mcp_tool` | 否 | `~/.workbuddy/skills/lingxi-cloud-memory/SKILL.md` | `-Clients WorkBuddy -Apply` | 历史 Skill 静态验证 | 单独配置 UserPromptSubmit Hook 可提升为生命周期入口，须另证触发 |

统一规则只维护在 integrations/rules-source/rules/resume.md；Rulesync 33.0.0 是构建期依赖，package-lock 锁定，在临时 HOME 内生成暂存产物，最终用户无需安装。resume-rules.md 是生成物。

`run.py integration-status` 是生产只读自检入口，报告配置、规则和生命周期入口是否存在。verified_native、automatic_recall_verified 不因静态检查变真。cwd 仅作检索线索，不承诺单独确定项目；歧义只确认一次。

`run.py configure-rules --clients DSH,ZCode` 默认预览，--apply 后保留原内容，私有事务日志记录真正的修改前原文和哈希；未知规则块停止覆盖。`run.py rollback-rules <journal>` 核对全部 after 哈希，有后续用户改动就停止，能恢复旧文件或移除本产品新建且哈希匹配的文件。

原生门槛为至少两个不同 Agent 的普通新会话正确取得目标、状态与下一步，不提醒查记忆，不附答案。一次提交，失败保留，不自动重试。模拟、CLI、桌面和新电脑结果分列；无对应原生日志不得宣布自动接续通过。
