# 第三方组件

公开仓库分发本产品源码与依赖版本清单，不携带本机 Python、依赖目录或登录资料。安装时从 python.org 获取必要的官方 Python 运行环境，从官方 PyPI 获取锁定依赖。Python 来源与哈希记录在 PORTABLE-RUNTIME-SOURCE.json，依赖版本见 requirements-portable.lock。

主要依赖包括 MCP SDK、Playwright、aiohttp、PyYAML、pywin32。使用与再分发遵循各组件许可证，安装得到的包保留相应许可文件。Microsoft Edge、WPS 灵犀和各 Agent 客户端由各自厂商提供；本产品为独立连接器，不宣称厂商官方授权或合作。


## 构建工具与生命周期参考

Rulesync 33.0.0 (https://github.com/dyoshikawa/rulesync), MIT，作为构建期依赖，锁文件记录 npm 完整性；不随最终安装分发 node_modules。依赖包中的 LICENSE 保留。

DSH agent/pre-step 中间件形状与 producer snapshot 处理参考并适配自 zilliztech/memsearch，MIT，参考树 09e07d86b31ff81938b4f44a57099b6a9fc8723f，plugins/dsh/index.js。未引入采集、索引、Milvus 或模型调用。

上游完整 MIT 许可（Copyright (c) 2025 Zilliz Inc.）保留在 integrations/dsh/LICENSE.md。

OpenViking 仅参考机制，未复制其 AGPL 源码。
