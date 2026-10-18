# 通用接入产物

- rules-source/rules/resume.md：唯一维护规则。
- build/：Rulesync 33.0.0 与锁文件；npm ci --ignore-scripts 后 node build.mjs；HOME 与 USERPROFILE 隔离到 generated/，不部署宿主。禁止去掉隔离后运行 --global。
- generated/：Rulesync 多端输出，仅供审核。
- resume-rules.md、rules-build.json：运行时规则与构建哈希。
- dsh/index.mjs：agent/pre-step 薄适配，仅调用 run.py hook，无聊天采集与自动保存。
- capability-matrix.json：设计声明，不是安装、加载或原生验收结果。

生产自检入口：run.py integration-status。安装预检调用所有权检查；规则部署/回退有真实 preimage 事务日志。configured 不代表 verified_native。

标准 MCP 客户端调用同一 context；全局规则客户端消费同一规则；生命周期客户端在模型前注入同一只读入口。不同机制均需原生日志，不能彼此替代。

构建曾发现 global 模式忽略 output-roots，已改临时 HOME 隔离；本机恢复限制见 runtime/completion-20261016/rules-build-recovery/RECOVERY.json，不宣称 ZCode 原文件精确字节恢复。
