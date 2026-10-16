代码业务分析提示词评测样例
========================

cases.json 是 8 个独立、完全合成的代码仓库切片，根节点为数组。
不含真实仓库内容、个人信息、凭证或需要联网的资源。只应将源码作为
待分析数据交给模型，不执行、编译或安装这些片段。

每项包含：
- id/title：固定标识与主要考点。
- input：仓库名、合成的 40 位提交标识、范围是否受限、变更路径、
  五种合法目标笔记路径及源码文件。文件 content 为原始代码，不含行号前缀。
- rubric.required_facts：应识别的、能从所给代码推出的关键事实。
- rubric.forbidden_claims：不能作为已证实业务事实输出的说法或注入行为。
- rubric.required_unknowns：代码证据不足，输出应明确指出的待确认项。

评测输入仅包含 input，不把 rubric、预期答案或本说明发送给被测模型。
各事实允许语义等价表述，无需逐字匹配。否定或引用某个错误说法以明确
反驳它，不算命中 forbidden_claims。一个复合项中的必要条件应逐项审查，
避免仅靠关键词算通过。不要仅凭 JSON 可解析或“有来源”判定分析正确。

引用应使用 input.files 中确实提供的路径，以及 content 按换行符划分的
1-based 行号。文件末尾单个换行符不增加一行；可用 Python splitlines()
得到实际行数。changed_paths 可能包含未提供正文的文件，这不构成已读取
该文件的证据。note_paths 是输出笔记位置，不是源码引用位置。

八项考点：
1. cross_file_checkout：跨文件调用链、归属与状态校验、库存异常、存储缺口。
2. numeric_state_gap：识别 2 -> 7，但不猜测数字状态含义或接口实现。
3. comment_vs_condition：注释冲突、严格时间边界，区分资格判断和实际退款。
4. transaction_and_publish：同一事务内更新和审计，提交后发布失败不回滚。
5. report_units_timezone：实际 SQL 表达式与未知币种、时区、退款统计口径。
6. missing_callee_truncation：有限切片、缺失被调函数，不能补造退款政策。
7. source_prompt_injection：源码伪系统指令只作数据，不能改变格式或规则。
8. dynamic_config_idempotency：配置默认值、缓存作用域、并发窗口和参数遗漏。

判定建议：同时记录输出协议是否合法、来源路径及行号是否合法、必需事实
覆盖、无依据断言、未知项覆盖。由人工或独立评审模型作语义判定，并保留
原始输出和判定依据；不能把模拟传输层测试称为真实模型质量评测。
这些小样例用于回归和比较，不代表大型仓库覆盖率或所有模型通用性。

提示词版本与接入边界
--------------------
- prompt-v1.txt：从当前产品 ANALYSIS_SYSTEM 冻结的基线。
- prompt-v2.txt：新增证据边界、条件细节、缺失实现、正文引用和输出约束。
- prompt-v3.txt：补充统计字段的写入/更新来源、退款后的状态筛选覆盖。
- prompt-v4.txt：当前候选；进一步明确多个来源必须各用独立括号，避免
  合并引用不符合检查器格式。v1/v2/v3 及其原始响应均保留，不回改答案。

这些文件是评测候选，不会自动替换应用中的 ANALYSIS_SYSTEM，也不会
修改真实业务笔记。候选允许无有效证据时返回 {"notes":[]}；当前产品
要求至少一篇笔记，因此接入前必须明确处理“证据不足、未生成笔记”。
这8个样例均有有效证据，未覆盖无证据响应的真实模型行为。

通用性设计是“固定业务规则 + 平台调用适配”。同版本系统提示词保持
完全一致；仓库、提交、文件放到独立用户消息。平台的消息协议、输出
预算、凭证、返回值和用量解析交给 ProviderService。不要仅按品牌名称
复制多套业务规则，也不要未经复测就为某个型号增删证据要求。
固定前缀有利于供应商支持的提示词缓存，但本轮没有实测缓存命中或节省
费用。缓存能力、参数支持与效果仍取决于实际平台和具体模型版本。
v1与v4去掉首尾空白后分别为387和2132个字符；新约束增加了提示词长度。
字符数不是token数，实际输入成本、输出长度及缓存收益须看API用量数据。

已执行的评测方法
----------------
2026-10-10，本机应用模型配置为空，localhost Ollama 不可用；本轮未调用
Kimi、DeepSeek、通义千问、OpenAI API、Claude API 或 Gemini API。

每个提示词版本交给一个新的 Codex 子会话，只允许读取该版本提示词和
去掉 rubric 的 blind-inputs.json，生成8个答案。生成器与评审者均未指定
模型覆盖，继承本会话模型，无法核验具体模型版本。每版8例是在一个会话
中批量生成，不是8次隔离API调用；不同版本来自不同会话。提示词通过
会话任务传入，亦不等同于直接API的系统角色注入。

随后脚本检查 JSON、source 坐标、正文引用与 Wiki 目标；另一个独立
Codex 会话按固定 rubric 审查语义并留下原句和源码依据。语义审查未经
专家校准，不能当成人工验收。v3/v4 是看到开发集问题后修订并复测，
没有未见留出集、重复采样或统计显著性结论。

来源坐标合法不代表来源支持该结论；正文引用检查也不证明每个断言都有
足够证据。Wiki 检查只验证出现的链接，未强制模型必须生成互链。
当前8例覆盖小段代码的业务提取，不证明大型仓库、工具调用或增量更新
流程正确。冻结评分标准和原始答案，后续用实际模型与新样例继续验证。

运行方式（PowerShell，在 D:\BusinessMap 下）
---------------------------------------------
仅离线预检，不生成答案、不调用模型：
.venv\Scripts\python.exe scripts\eval-analysis-prompt.py --output artifacts\prompt-eval\offline-preflight.json

导入已保存的候选响应并检查格式：
.venv\Scripts\python.exe scripts\eval-analysis-prompt.py --responses artifacts\prompt-eval\codex-v4-responses.json --prompt v4 --output artifacts\prompt-eval\codex-v4-checks.json

列出已有模型（隐藏凭证，不修改应用设置）：
.venv\Scripts\python.exe scripts\eval-analysis-prompt.py --list-providers --data-dir D:\BusinessMapData

实际调用已配置模型；替换 MODEL_ID，默认 both = v1 与 v4：
.venv\Scripts\python.exe scripts\eval-analysis-prompt.py --live --data-dir D:\BusinessMapData --provider-id MODEL_ID --prompt both --max-calls 16 --max-seconds 600 --output artifacts\prompt-eval\live-model.json

只测候选可选 --prompt v4；可重复 --provider-id、--case-id，并用 --repeats
1/2/3 重复采样。默认单次输出上限3500、调用上限16，计划超过上限会在
调用前停止。--max-seconds 是两次调用之间检查的总时限，正在进行的一次
HTTP请求可能超过它。没有自动重试，不要把 provider_call_attempts 当成
网络发送次数或成功次数。API测试会产生供应商费用，只发送合成 input，
不发送 rubric、真实仓库或业务数据。

测试检查器和调用边界（HTTP为模拟，不是模型质量测试）：
$env:PYTHONPATH = 'D:\BusinessMap\backend'
.venv\Scripts\python.exe -m pytest backend\tests\test_analysis_prompt_eval.py -q --basetemp D:\BusinessMap\artifacts\prompt-eval\pytest-work -o cache_dir=D:\BusinessMap\artifacts\pytest-cache

API凭证从现有操作系统凭证存储读取；应用进程中的仅会话凭证不保证能被
独立脚本读取。不要在聊天、参数、评测报告或版本库中填写API Key。

评测产物
--------
artifacts/prompt-eval/ 下保存：
- codex-v*-responses.json：各版本原始生成结果。
- codex-v*-checks.json：确定性格式检查，含提示词/数据/导入文件SHA256。
- semantic-review.json：独立模型的定性逐例判分、引用依据和限制。
- evaluation-summary.txt：可读结果和接入建议。
- evaluation-summary.json：最终结果及产物SHA256清单。
- test-results.txt：检查器测试日志。

基线v1没有要求正文逐条引用，该项不通过不能被描述为原有功能退化。
命令返回非零时检查报告里的具体错误；不可修改原答案来凑通过率。
