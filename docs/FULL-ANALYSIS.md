# 完整项目分析

项目分析以完整源码覆盖为目标。旧的单次取样只能生成局部预览；不能把少量预览主题当成项目只有这些业务。

## 完成条件

1. 固定业务项目成员、各仓提交、模型授权和知识库。
2. 清点所有受支持的已跟踪源码与必要契约，单独记录排除路径及原因。完整模式不沿用资料索引的500文件/128KB限制。
3. 大文件按完整行分段，小文件按模块合批。每批独立限制上下文，不能截掉文件后半部分后计为已分析。
4. 所有纳入文件的行区间必须恰好覆盖至末行；每批获得结构和来源编号有效的模型结果。
5. 将全部成功批次保留为私有事实账本，再按业务操作整合权限、输入、条件、结果与未知项。逐项记录每条输入的公开支持、保留未知、证据消解或内部说明去向；混合未知须分别处理。候选须通过结构、隐私及独立语义检查，才能生成业务知识、地图与总结。
6. 所需文档成功保存后才能显示完整完成。存在失败、未处理片段或未发布的人工冲突时保留进度与原文，明确尚未完成。

完整覆盖指纳入范围的源码文本已全部处理，不保证每条模型陈述语义正确，也不代替运行验收。测试、接口声明、DTO和生产实现必须区分，跨端调用关系需要两端来源；同名或位于同一项目不能证明连通。

## 状态与恢复

- 本地清点、分片计划和进度读取不调用模型。
- 模型批次状态、源文件与行段、输入摘要、调用用量及核验结果保存在本地SQLite；生成的业务知识正文以Markdown为准。
- 同一固定版本恢复时复用成功批次，只处理未完成部分。业务汇总也按输入摘要保存断点。
- 暂停阻止新的模型发送，在途调用可完成并保存回执。单个模型输出未通过结构或来源校验时，记录该批次失败，其他独立待处理批次继续各尝试一次；模型调用、授权、源码快照或输入错误会停止新发送。失败批次不自动付费重试，继续分析时仅处理未成功部分。任何失败仍会阻止完整完成和文档发布。
- 运行使用操作系统文件锁，防止桌面窗口与另一后台进程同时重复发送同一项目。
- 服务中断后，如果运行记录仍是进行中但操作系统锁已释放，界面会显示可继续的中断状态；读取状态不会启动模型调用。
- 项目成员、代码、知识库或授权变化后拒绝旧结果，界面不会把旧版本完成状态当成当前完成。
- 安全排除、文件类型不支持及无法完整处理的行必须分别可查。凭据字面量在发送前脱敏并保持行号；业务类名含Credential、Password等不应被误当凭据文件排除。

更换模型时，普通重新绑定仍不自动复用原供应商回执。若原断点与授权仍有效，可在项目绑定弹窗选择“核验并接续已完成结果”：重新读取固定 Git 内容并核对脱敏输入摘要、行段与引用，新建暂停的接续任务，保留旧任务及其原始结果。继续后仅发送未完成源码，内部回执保留混合模型来源，公开文档不展示模型迁移配置。核验及迁移不调用模型。

“本机 Codex”使用官方 CLI 现有 ChatGPT 登录，不读取或复制凭据，不向模型开放命令、仓库脚本或数据库工具。源码仍经过相同脱敏和引用校验。建议为长任务配置明确模型 ID；`default` 使用安装版本内置的默认型号。CLI 的输出预算不是服务端 token 硬限制，超过本地预算或未正常结束的结果不会计为成功。

当前本机 Codex 适配器在 macOS/Linux 启用；Windows 尚未实现完整子进程树终止，界面会明确拒绝此模式，HTTP 模型接入照常可用。

## 内容位置

- 内部源码索引、引用坐标、固定版本、覆盖与排除记录保存在私有元数据和检查点，不进入业务 Vault。
- 业务知识：`01-Wiki/<项目名称>/<实体|流程|规则|数据口径>/<主题名称>.md`。
- 业务地图：`02-Areas/业务地图/<项目名称>/业务地图.md`。
- 分析总结：`03-Projects/业务梳理/<项目名称>/分析总结.md`，说明业务结论及未确认事项。

公开正文、标题、路径、YAML 不包含源码、源码位置、提交、内部仓库或运行标识、类和方法名、接口路径及迁移配置。发布前校验不通过时保留内部断点并停止发布，不能把简单删词当作完整业务归纳。普通问答、来源、活动、本地摘录及历史回放同样受此边界限制。
业务原文和阅读范围在私有账本中完整保留。公开说明按业务操作组织，保留业务条件、数值、否定及真正未知；只有同一子问题、证据层次与范围一致的明确资料才能支持消解。纯阅读覆盖或注释不一致留作私有观察，不能变成整个项目的正确性疑问，也不能仅凭一条正面资料宣布观察已经消失。窄业务问题仍可列为待确认，不能只因来源范围有限而隐藏。没有实际匹配证据的跨端关系在私有账本继续待核实，静态实现也不能代替运行验收。
L0 v8 只生成私有导航：每条短事实编号唯一分配到业务主题或有来源依据的内部分类，不生成或接受改写正文。public 标签不是发布许可，internal 标签也不删除原事实；包括内部分类在内的全部原文、类型、条件及未知项继续进入现有 schema8 业务整合和独立评审。HTTP 与本机 Codex 共用该新契约；SOURCE1、后续 semantic HTTP 请求、完整事实支持证明和公开隐私门禁保持原样。
已接受的 v7 导航缓存只按精确旧 SYSTEM、wire、snapshot、成员和冻结校验器读取，归一化结果必须完全一致；旧行和时间戳不改，也不复制成 v8 成功行。批次保留旧 v7 边界（最多24事实、37000输入字节、6000输出预算）。v8 缓存同样完整校验；同批次两版本的实际导航不一致即停止。任何已有 semantic task（包括失败）都要求调度前全局验证全部 L0 缓存，缺失或损坏时不发出新模型调用。semantic_input_hash 不因导航协议升级改变，已有终态仍阻止重跑；缓存命中和事务提交后的成功事件仅记录私有 key/version/result hash，不含正文或来源坐标。
业务整合和独立评审都有明确预算与检查点，不自动循环修改或重试。未通过的候选保留待审并阻止发布；定向补充使用新的独立来源回执，不改写已成功的主批次结果。仅完成源码覆盖或输入去向计数，不能证明业务说明已通过语义验收。
每个业务包最多两稿。可信本机 Codex 的首稿结构有效、且独立首审完整退回时，修订请求携带完整原文、无损压紧的首稿、精确负项及服务端限定的修改范围。无关且已核验的业务陈述和私有处置不得增删改写；守卫只拒绝漂移，不自动补回旧内容。第二稿仍须完整独立核验，评审者不接收首审意见。新增上下文也受原请求硬上限约束，超限时不发送付费第二稿。HTTP 及结构无效首稿保持原有路径；模型连接、隐私、结果不明或第二稿失败仍终止，不增加第三稿。
业务知识、业务地图和总结过大时按完整章节、段落或整行分页。首页保留全部业务主题和每一部分的入口，原始事实、流程、待核验事项不因分页删减；所有部分统一参与写入前检查、版本核验和失败恢复。

实体、流程、规则、数据是知识的分类维度；不会再用固定四篇分类卡代表全部业务模块。旧技术预览不进入公开业务阅读或 AI 证据。人工修改始终采用版本检查与独立更新建议。

## 显式一次目录 transport 恢复

当前普通恢复仍拒绝既有模型调用终态。可信本机 Codex 的目录 leaf 在 attempt=1、needs_review、model_call/provider_error、严格整数 502 且 result=None 时，可由本地 operator 为精确 parent 创建一个独立的一次 child。generic 502 不代表已证实的上游 HTTP 或瞬态故障。SOURCE、L0、subdivide、partition_review、proposal/review、语义拒绝和 child 本身均不适用。

原父行全部字节和费用占用保留；原 system、模型 wire、schema、destination、profile、semantic_input_hash 与 generator_hash 不变。授权与 planned child 原子创建；消费回执与 planned→running 在一个 SQLite CAS 事务提交，之后才能发送。消费还要求当前项目 OS 锁、未暂停/取消、当前 scope 和旧 owner PID 核验。消费后即使发送前进程崩溃也不补发，running 未知和 child 失败永久终止。已完整 received 的合法历史消费可在当前守卫通过后无费接纳，授权过期只阻止首次消费。

全部 inventory 计入父失败和 child。完成证明必须精确包含 accepted child、原父、独立 DB 中的回执集合；孤立、删除、篡改、多余 planned 授权或内联伪回执均拒绝。恢复摘要只在存在恢复时加入投影哈希；普通已完成证明保持旧字节与零调用复用。私有 authorized/consumed/received/accepted 事件只在对应事务提交后产生，不记录正文或凭据。

该控制不增加公开 UI、HTTP 接口或模型工具。旧 smoke 的付费预算执行恢复仍不支持并拒绝；完整分析没有由此新增 global 32 次或费用硬上限，也不保证外部模型幂等。实际继续分析仍必须经过独立审查和当前授权检查，完成还需全部目录及后续两稿两审、canonical8 和发布守卫。

### Bounded catalogue navigation

Upper catalogue merge requests can use `bounded-operation-navigation-v1`:
complete operation titles and categories, exact fact counts, and ordered-alias
counts/digests form bounded navigation. All original private facts and aliases
remain intact; no first-N alias selection is used. Navigation compression does
not establish complete business relationships. Full original evidence still
feeds proposal and independent review, and unsafe/non-shrinking grouping fails
closed.

Paid legacy pages are adopted only after reconstructing original SOURCE/L0
routing and exact page order. A legacy failure remains a failure and retains its
billing occupancy. A separate root-authorized one-shot contract supersession
may bind that unchanged row to one normally hashed new-contract child. It is
not an automatic transport retry, and a fallback status 502 is not evidence of
an upstream provider error. Receipt authorization and dispatch consumption are
separate persisted transitions; accepted/received results replay without new
provider calls, while unknown running work requires reconciliation.

A consumed one-shot child remains running if provider setup raises before
admission. Its consumption receipt remains intact and replay requires
reconciliation; the runtime never resets it to planned or opens another send.
Final product reads validate the complete catalogue ancestry from the frozen
paid frontier, including every adopted page and single consumption of each
parent operation. An unrelated accepted page cannot be added to a rehashed
projection to make it part of the proof.

A fresh root grant may authorize one transport child for an ordinary bounded
catalogue merge classified model_call/provider_error with strict integer502.
That generic CLI classification does not establish an upstream HTTP error or
transient cause. This separate policy retains the exact original wire and
private context, preserves the failed parent and all paid results, and does not
apply to a recovery child, supersession child, legacy merge or other stage.
The grant has one use and a 3600-second window. Consumed running work requires
reconciliation; received results replay under guards after expiry without a
second dispatch. A later failure stops rather than authorizing another call.

## Explicit interrupted bounded merge reconciliation

An ordinary bounded catalogue request with a confirmed local dispatch but an
unknown outcome may use `operator-bounded-catalogue-interrupted-once-v1` only
through a fresh, precise operator authorization. Eligibility requires a trusted
running attempt-1 row, null result and SQL error, no failure/diagnostic and no
recovery or supersession metadata. Source, L0, leaves, legacy merges, proposal,
review and one-shot children remain excluded. No response or provider error is
manufactured from the dispatch evidence.

The original running row is immutable and remains a paid occupied slot. The
operator explicitly accepts possible duplicate upstream work for a distinct
single-use child. Fresh drained-process observation and hash-bound mode-0600
no-follow evidence must show the exact final adjacent reservation/start pair
with finite ordered `at` times, and no recovered result or later event.
Authorization inserts a receipt and planned child atomically while paused;
consumption rechecks current scope, controls and evidence and burns the receipt
before send. For a new authorization, the check must be current within 60 seconds
and must precede `authorized_at` by at most 60 seconds. Planned authentication and
consumption preserve that authorization-time freshness while rechecking current
PID absence, scope and immutable evidence throughout the original 3600-second
grant. A cached startup longer than 60 seconds does not require an evidence
rewrite or extend expiry. Failure before return leaves an unknown child that
cannot retry.

The full inventory exempts only the authenticated original interrupted parent;
an unpaired unknown sibling or unknown consumed child still blocks. Final
catalogue ancestry consumes the accepted physical child once, while the required
inventory retains its original running parent and all prior transport and
supersession proofs. Current, stored and support consumers require the complete
receipt closure. Consumed historical proofs do not depend on surviving evidence
files or grant freshness; durable received results can replay without another
model call.

The disposable regression topology proves four transport recoveries plus one
supersession over 64 real facts and 64 L0 rows. Its interruption is an actual
child-process exit during an ordinary runtime call after local admission.
Reconciliation then completes publication and current/stored/support validation
without changing prior raw rows, accepted receipts, Source or L0, or repaying
accepted pages. This is bounded fixture evidence, not acceptance of any live run.

## Complete paid catalogue frontier

Catalogue pages do not have to reduce the number of distinct business
operations. A complete source-backed paid forest now terminates directory
acquisition without forcing unrelated operations into one page or depending on
logarithmic semantic contraction. Accepted refinements consume exact physical
parent operations once; every paid page participates and untouched ancestor
operations remain in the terminal frontier. All original fact and alias
memberships are retained, including a frontier larger than 128 operations.

Valid received results first replay for free under current guards, then the
runtime captures a new snapshot before sealing complete-operation-frontier-v1.
The original unknown recovery parent and occupied fees remain unchanged. Four
recovery and supersession ancestry stays mandatory; no new authorization or
retry is created by the frontier. Already completed single-root projections
retain their old identity and replay path.

The frontier proof establishes exact evidence coverage and paid ancestry.
Complete original facts still enter ordinary semantic packets, reviewed
subdivision when needed, proposal/review, bounded correction and explicit
support validation. It does not assert global title deduplication or invent
cross-packet business relationships. Catalogue closure, downstream semantic
acceptance, stored/current/support/audit validation, publication and manual UI
acceptance remain separate outcomes.

A new complete frontier always requires one uniquely authenticated persisted
SOURCE run and both source reader callbacks. Deleted, empty, unavailable or
mismatched source metadata fails closed; inline projection inputs cannot supply
missing coverage or canonical ordering. The separately named checkpoint-only
preview is explicitly nonauthoritative and returns no terminal candidate; it
cannot seal a new proof or continue into semantic publication.

For a specifically selected semantic partition rejection, bounded partition
repair permits one feedback-guided regroup and one unchanged strict full-parent
review. Complete original facts and aliases remain in scope; feedback is not a
new source of facts. Only a strict accepted review can enter the final
projection. The original accepted subdivision and rejected review remain
immutable and counted, and the derived review identity depends on the actual
regrouped result. Unknown outcomes, privacy issues and a second rejection do
not authorize another draft. The default runtime selection is empty; stored
accepted and received receipts replay without new paid authorization.

An explicitly selected `atomic-parent-budget-v1` path can keep a rejected
partition's complete parent in a single semantic packet. It derives a smaller
per-packet candidate cap from the complete serialized request, preserving the
fixed provider profile, original facts and threefold review reserve with a
1,024-byte margin. It retains both prior rejected partition reviews and their
subdivisions as paid, immutable history. Ordinary semantic proposal and
independent review remain mandatory; a smaller budget does not prove that a
candidate fits or is semantically correct. Default admission is empty, and
unknown or unauthorized work is never retried implicitly.

For the fixed local profile, `whole-operation-exact-fit-v1` admits an otherwise oversized complete root only when its full first proposal fits 64,000 bytes and its candidate estimate fits 40,000 bytes. It retains every original fact, observation, matching receipt and unknown. Both raw and canonical candidate graphs remain limited to 40,000 bytes; an actual 160,000-byte review gate runs before reserving a review task. A saved proposal whose complete review cannot fit remains preserved; the run stops without reserving or sending a review. A semantic rejection follows the existing reviewed-prior preservation path. Revisions share the two-draft limit, and a second proposal must itself fit before reserving a slot. Existing partition and atomic histories remain immutable and occupied, and only an independently accepted review can support publication.

## 普通提案超时的显式一次替代

普通 D1 提案若以本机 Codex `model_call / 408 / timeout` 结束且结果为空，专用操作控制可在暂停状态下仅为明确选择的完整 parent 创建一次独立 child。完整 SOURCE、目录及分区、已选结构历史、原始父行和当前库存必须重新认证；授权文件和本地进程退出证据必须为私有 0600 文件。来源摘要绑定稳定的 run 身份和模块 ID 及完整源码批次、补充、匹配和 L0，合法状态与进度变更不会改变恢复身份。记录只确认本地进程已退出，上游结果和计费仍未知。普通恢复默认关闭，不将其他 408、502、评审、whole/atomic 或已恢复子项纳入此策略。

此精确包保留超时父行的物理占用，再额外授予一次替代：最多 5 次物理调用、2 次逻辑草稿。评审使用实际 child 结果，只有真实质量反馈才能启动 D2；最终证明同时保留父行、child、回执、实际评审和质量修改链。消费与开始运行原子绑定，消费后中断不自动重发；其他包和全量调用边界保持原约束。
