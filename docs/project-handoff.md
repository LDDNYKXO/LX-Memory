# 最小项目交接与保存前评估

本轮为工作树实现，未部署、未改变宿主规则。只支持项目交接的确定性评估，不能称全库查重或自由文本语义冲突检测。非projects分类保留原有预览/保存流程，assessment.state=not_applicable。

## Agent起草格式

只使用当前用户陈述、已读取的云端来源和本轮已验证证据；缺口写“未知”。下方是模板，替换占位符后才是草稿；事实截至时间未知时不得编造ISO时间以通过校验。

```markdown
<!-- memory-project-record {"schema":1,"project":"<明确项目名>","supersedes":[],"as_of":"<已知事实截至时间，带时区ISO>","record_kind":"handoff"} -->
### 项目身份与目标
<范围、目标及已知仓库；未知明确注明>
### 已确认进展
<已执行且有证据的结果>
### 当前阶段
<现状与适用时间；区分工作树、部署、验收、发布>
### 待验证
<尚未证明的事项>
### 下一步
<建议，不是执行授权>
### 边界
<用户限制、不自动保存、不采集全部聊天>
### 证据
<来源路径及对应事实；缺失标未知>
```

沿用schema=1的用途、替代、时间字段。有效区间可选，使用valid_from/valid_until，左闭右开。未知不强加区间。不能因页更新时间更新就推断事实更新。supersedes只能写确有证据的同项目路径；预览声明不删除旧页。

首行可选facts对象：至多8个稳定字段，每键1–64字符、值1–200字符且必须是字符串。只有确已证明同一项目、同一字段及可比时间时，值变化才提示potential_conflict。机器、环境、对象等不同范围必须体现在字段身份中，不能把不同机器的runtime硬当同一事实。此提示仍需人裁定，不是自动判错。首行总长仍最多2,048字符。不能自动从聊天抽取facts以冒充已验证事实。

## prepare流程与分类

原category/title/summary/content参数和返回preview/sha256/related_existing保持；项目评估最多1次标题搜索、3个去重路径、读取预算12,000字符（每页最多4,000）、网络收集阶段30秒，不追加分页、替代路径或其它会话。单页超预算明确partial，可由用户决定是否另行核查，不把截断当无重复。

- exact_duplicate：同项目声明且标题、摘要、完整正文一致，仅统一换行，不删除语义空白，不忽略as_of或supersedes。优先建议不新增，仍不自动合并或删旧页。
- related_changed：同项目正文或标题/摘要有变化，不证明语义冲突或新版本更正确。
- potential_conflict：可比的显式facts同字段有不同值；facts的as_of相同，或双方都声明了完整且重叠的适用区间。仅as_of不同不能自动裁决。
- foreign_project：明确异项目，不作为当前项目的重复/冲突。标题相同不证明身份。
- explicitly_superseded：有限同项目替代图明确覆盖该页；旧页保留。历史、测试、过期或尚未适用记录不冒充当前状态。
- scope_unknown/unverified：身份不足、未完整读取、来源或契约异常；state=partial。循环/竞争替代保留conflict。checked只表示返回候选范围内检查完成，global_dedup_verified恒false。

assessment不返回候选私人正文，仅保留路径、更新时间、正文/元数据哈希、用途时间及判定。前3候选不足以保证全库召回，也不保证发现近义重复。

## 缺口确认与精确授权分离

needs_confirmation=true时，prepare仍生成完整精确预览和assessment_review_key，但blocked_reason=ASSESSMENT_REVIEW_REQUIRED，commit不POST。Agent展示具体缺口/冲突点供用户确认，不把“接受风险”编造成事实已核实。

只有用户明确确认这些具体事项后，才能再次prepare并将该key作为可选assessment_confirmation。它绑定草稿、账号/epoch、候选正文与元数据及当前评估；证据或正文变化则不生效。它只是缺口已审阅声明，不能认证真人，更不是保存授权，Agent不得自行填入冒充同意。

重新prepare返回新proposal_id和新目标路径；必须展示新预览，另获明确授权该提案后才commit。assessment_reviewed=true不会清除partial/conflict或将未知改成事实，不会覆写旧页。600秒期限、账号隔离、payload哈希、防目标覆盖、最多一次POST及读回回执保持。后台不得改写已授权正文。旧提案因阻断终结后，不能重试它来绕过审阅。

## 失败与回退

搜索/读取失败或超时返回partial，未知结果不称无重复；评估中账号/epoch变化拒绝生成提案。提交结果unknown只读receipt，不新建提案自动重发。原始预览、云端页及历史回执不被评估器合并、删除或自动更新。

本轮仅工作树与离线合成验证。部署须另行按当次新备份分段处理；不能部署后关闭评估保护来获得“通过”。回退只恢复本轮原文与哈希、移除本轮新增文件，不触碰既有proposal、receipt、登录状态或其它工作树改动。
