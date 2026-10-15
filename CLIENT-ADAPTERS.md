# 可选客户端配置适配

产品核心是同一个通用 stdio MCP 服务。默认 `Generic` 输出标准配置，由当前 Agent 按它实际支持的设置入口注册。以下适配只帮助写客户端设置，不是使用范围限制，也不要求同时安装其它 Agent。

| 可选适配 | 安装参数 |
| --- | --- |
| Codex | `-Clients Codex -Apply` |
| DSH | `-Clients DSH -Apply` |
| WorkBuddy | `-Clients WorkBuddy -Apply` |
| ZCode | `-Clients ZCode -Apply` |

可按用户授权选择多个适配，以逗号分隔。安装前预检配置，保留其它 MCP 设置、备份和恢复记录；出现同名未知连接或用户修改过的 Skill 时停止覆盖。配置存在不能代替客户端原生调用验收。

其它支持本地 stdio MCP 的客户端可通过通用配置接入。不要猜测未知客户端的配置路径；先核对该客户端文档或其设置入口。仅支持远程 URL 的客户端和其它操作系统尚未适配。
