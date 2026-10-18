# 第4步：诊断、受控升级与恢复

本轮仅工作树交付。以下是新增接口说明，不表示已部署，也不授权执行命令、安装或发布。适用范围仍是Windows x64、本产品现有安装；无后台更新、无联网下载更新器，不升级Python或依赖，不改宿主配置。

## 默认离线诊断

Agent使用已登记的安装入口：

```text
python -B agent_install.py --doctor
```

该分支在run.py/bootstrap之前处理，只用标准库读取本产品登记、受管代码、依赖目录、Edge存在性、构建回执、连接描述及维护事务。不联网、不启动服务、不导入产品第三方依赖、不安装或修改文件；JSON中的actions提供中文下一步。路径、文件存在不代表依赖实际能导入；dependencies_import_verified恒false。

默认running_build来自本地连接描述，不是进程存活证明；描述不存在只称未观察到，描述过期不能证明服务仍在运行。installed_build是磁盘代码构建标识，协议version:1不作为产品版本。服务启动时固定loaded_build；MCP新进程的serverInfo.version带相同构建标识。已有宿主是否刷新工具定义仍为未验证。

显式选择探测时才执行：

```text
python -B agent_install.py --doctor --probe mcp
python -B agent_install.py --doctor --probe online
```

mcp启动短暂的独立stdio进程，只initialize/tools-list，不调用写入工具；它不是任何桌面宿主的原生验收。online仅对已存在且归属匹配的本产品服务调用一次status，autostart=false，不自动启动服务、打开本人登录流程或采集聊天。不支持的旧描述、归属不符或服务不可达均明确失败，不换安装目录。

输出不含Cookie、Bearer token、账号标记、候选正文或聊天。提案过期沿用写回业务错误，不因此触发重装、自动续签或重复保存。unknown提交只允许回执核对；维护器看到未知回执或损坏回执会阻断升级。

## 旧安装基线与本地候选

没有维护回执的旧安装先报告UNMANAGED_BUILD，不能自动信任当前文件。Agent需按已验证交付证据核对完整受管文件集合，准备schema=1、product="LX Memory"、files（相对路径→SHA256）及dependency_lock（requirements-portable.lock哈希）的基线清单，并展示供确认。清单哈希应来自独立核对，不能用包内自带哈希宣称发布者身份。

```text
python -B agent_install.py --maintenance adopt --manifest <安装根中的基线清单> --expected-sha256 <已核对哈希>
python -B agent_install.py --maintenance adopt --manifest <同一清单> --expected-sha256 <同一哈希> --apply
```

默认只预览；apply才建立本产品runtime/session/maintenance/install.json。登记包含根目录、解释器路径及文件哈希、依赖锁哈希、受管文件与构建标识。已有基线绝不静默覆盖。

升级首轮只接受安装目录之外的已展开本地候选目录及独立指定的清单SHA；不是第二套可登录安装。候选必须列出完整代码集合，声明依赖锁不变。允许受管的src/lingxi_connector/*.py、docs/*.md和稳定run.py；Python、依赖、规则、登录、提案、回执和其它runtime状态不在替换集。run.py在本阶段必须与现版逐字相同，更新启动器本身需要单独受控交付。候选有迁移要求、哈希不符、语法不合法、路径越界、符号链接或未知本地变更时，修改前拒绝。

```text
python -B agent_install.py --maintenance plan --manifest <候选目录/package.json> --expected-sha256 <审核哈希>
python -B agent_install.py --maintenance apply --manifest <同一清单> --expected-sha256 <同一哈希> --plan-id <预览返回值> --apply
```

plan仅读取并返回确定的plan_id、前后构建和文件改动；apply重算计划，发生变化即拒绝。无需用户手工编辑JSON，由Agent完成材料核对与预览；预览不能替代实际升级授权。

## 升级事务及失败处理

本产品维护锁排除并发升级。修改前新建事务，保存每个变化文件的原文与哈希，并先持久化journal。先通过本服务维护门闩确认无在途请求、写回锁或unknown回执，再停服务；临时未保存的已登录会话会阻断操作，避免悄悄丢失登录。

持有broker.lock防止其它调用方在文件变更期间重启服务；稳定run.py与更新器共享startup.lock，覆盖关键产品导入，避免新stdio进程读到混合文件。普通CLI/hook在完成导入后先释放启动锁，再访问或启动服务，不能阻塞自己的子服务。active.phase=ready只表示文件集合可加载，服务写入门闩仍保持到事务提交。

候选即使遇到原服务停止，也必须受控启动、验证运行构建、工具契约及相应登录状态，再恢复原停止状态；不能跳过验证直接提交。必要工具及参数类型须兼容，允许兼容新增工具。失败只进行一次回退尝试，不循环升级或无限修复。

## 中断恢复与显式回退

```text
python -B agent_install.py --maintenance recover --transaction <本产品32位事务ID> --apply
```

recover恢复该事务之前的版本；对已完成事务调用也表示明确回退。恢复前核对安装归属、当前回执、完整受管文件集合、每份备份以及维护门闩的事务归属。只有变化文件允许出现该事务的前/后哈希，未变化文件也必须匹配；发现外部修改、损坏日志或备份时，任何恢复写入之前停止，不覆盖现场。

恢复成功后核对旧文件集合和服务，再持久化rolled_back，清理匹配门闩并恢复服务请求。无论中断发生在清理前还是清理后，再次recover都能幂等收尾。它不恢复登录数据库、旧proposal/receipt或云端内容；不把未知保存重发一遍。描述缺失时仍需取得本产品broker锁，无法证明服务已停止则拒绝。

首轮不提供发布签名服务、自动下载、依赖迁移或跨版本回执转换。uv的安装归属、Inspector的分层探测和TUF的可信元数据区别仅为机制借鉴；未复制第三方实现或新增依赖。部署、真实云端探测与宿主验收需另行授权，不能用这些合成测试代替。
