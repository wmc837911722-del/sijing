# 0.2 开发预览验收记录

验收日期：2026-10-10。此记录区分已执行的检查与尚未验证的环境。

## 0.3.0 Markdown 知识网络渐进查阅

内置问答改为模型自主选择工具的多轮查阅：目录/搜索 → 大纲、章节与分页读取 → 双链/反链 → 带实际阅读凭据的回答，顺序由问题和每轮工具结果决定。搜索摘要不直接成为已读来源；支持直接进入指定笔记，无强制根目录遍历。MCP 与内置 AI 共用导航实现，扩展为 9 个工具。Markdown 仍是权威内容，没有新增向量数据库或 Obsidian 运行依赖。

桌面增加实时探索记录、完成后的可折叠步骤、打开来源和停止回答；停止在当前模型调用或工具返回后生效。权限、知识库、文件 revision、生成笔记代码版本与数据源配置在后续调用前重新检查。生成笔记移动到普通目录或跨类别目录后继续校验归属及新鲜度；新的 AI 笔记记录 generated_key，旧笔记无法可靠追溯时保守排除。只读问数按需查阅 schema 和业务依据，每轮最多尝试一次 SQL，沿用 SQL 校验和数据库只读层；实际结果直接呈现，不回传模型。

- 最终全量后端 **195 passed**，仅既有 Starlette/httpx 弃用警告：[测试日志](../artifacts/backend-v030-final.txt)。覆盖多轮真实 HTTP 协议、长文章末尾章节、关联发现、引用来源、revision/权限变更、取消阻止 SQL、重复调用/预算、只读拒绝、三协议签名回放及缓存统计。此前集成范围 86 项与各分项不额外相加计数。
- TypeScript 与 Vite 构建通过：[构建日志](../artifacts/build-v030.txt)。既有 Markdown 编辑器 chunk 超过 500 kB 的构建提示保留，本轮未改编辑器拆包。
- 原聊天交互 **21 项通过**：[交互记录](../artifacts/ux-chat/checks.json)。渐进探索专项在源码与 Windows 0.3.0 包内程序各 **19 项通过**：[源码](../artifacts/exploration/source/checks.json)、[打包](../artifacts/exploration/packaged/checks.json)。涵盖真实本机模型 HTTP + 后端 + 界面、实时步骤、问句去重、草稿、源笔记打开、重载持久化及取消；浅深主题 1500×940 与 1080×700 检查无渲染异常。截图：[浅色](../artifacts/exploration/packaged/completed-1500x940-light.png)、[深色小窗口](../artifacts/exploration/packaged/completed-1080x700-dark.png)、[处理中](../artifacts/exploration/packaged/progress-light.png)。复现入口为 `node scripts/smoke-exploration.mjs`，打包时设置 BUSINESSMAP_TEST_EXECUTABLE。
- Windows 打包主流程 **17 项**与冻结 MCP 检查通过：[套件](../artifacts/packaged-suite.json)、[桌面](../artifacts/packaged-smoke.json)、[MCP](../artifacts/packaged-mcp-smoke.json)。MCP 已验证新增大纲、章节及关联工具，违规 SQL 被拒绝、示例数据库未变化。
- 重新冻结 Python 后端；六个变更模块的冻结字节码与当前源码编译结果一致，交付后端与构建后端 SHA256 一致：[核对记录](../artifacts/frozen-source-v030.json)。安装版与便携版已生成，文件校验和见 [SHA256SUMS.txt](../release/SHA256SUMS.txt)。

已使用原 `D:\BusinessMapData` 打开 0.3.0，原生窗口正常响应。6 篇 Markdown SHA256 和 5 个应用表记录数前后相同；记录数不等于完整数据库逐字段比较：[数据保留记录](../artifacts/user-data-preservation-v030.json)。

模型验证使用受控 HTTP 服务，没有使用真实供应商密钥。所选模型必须支持原生工具调用；连通测试不证明该能力。缓存记录实际返回指标，未测量或承诺真实厂商命中率。引用检查验证已读取来源，不能证明模型每一句业务解释正确。本轮没有重复图谱性能基准、九阶段完整桌面套件；未实机验证 macOS/Linux、Windows 安装向导/卸载及真实内网服务。

## 0.2.9 国内模型配置

新增 Kimi、DeepSeek、通义千问、智谱、豆包、硅基流动、腾讯混元 TokenHub、百度千帆、MiniMax、讯飞星火、阶跃星辰和百川，共 **12 个国内平台预设**。加上原有三类国际服务、自定义兼容服务和 Ollama，共 **17 个预设**。按国内、国际、自定义与本地分组；新增默认选中 Kimi，模型 ID 留空供用户选择或填写，不设型号白名单。提示区说明地域、模型/接入点 ID、平台专用密钥，已有配置不迁移。官方资料与范围见 [模型配置参考](MODEL-PROVIDERS.md)、[核实记录](../artifacts/provider-docs-v029.json)。

MiniMax 请求独立思考字段；星火官方兼容路径关闭默认联网搜索。连接测试保持 32-token 上限，对明确因额度耗尽而只有思考内容的有效响应提示「已连接、完整回答尚未验证」。正常业务问答和 SQL 生成仍拒绝同类空正文，保留实际用量统计，不执行 SQL，也不把思考文本当答案。

TypeScript 与生产构建通过。后端新增国内适配测试 **27 项**，连同既有模型、密钥和仓库目标隔离回归，选定范围 **41 passed、43 deselected**，仅一条已有 Starlette 弃用警告；强化 SQLite 查询结果记录断言后，对应集成测试单项再次通过。原始结果来自本轮工具控制台，未另存测试日志；不将单项复验额外计入独立测试数。可从 `test_domestic_providers.py`、`test_repository_policy.py`、`test_tools.py` 使用 `-k 'domestic or minimax or custom_compatible or spark or reasoning_limited or probe_never or provider or secrets or stable_evidence or wire or destination'` 重现该范围。

源码模型配置专项 **64 项通过、2 张截图**，覆盖 17 个选项、平台分组、自由模型 ID、切换清空密钥、编辑留空保留密钥、保存/重载、真实测试按钮和服务商实际缓存字段。请求仅发往独立 loopback 模拟服务；未使用真实供应商密钥。浅色 1440×940 与深色 1080×700 人工检查通过，小窗口标题保持可见、保存可滚动到达，无横向溢出或渲染异常。[源码结果](../artifacts/model-providers/source/result.json)。

最终 Windows 打包程序 `release/win-unpacked/Sijing.exe` 的相同模型专项 **64 项通过、2 张截图**，程序版本为 `0.2.9`，没有渲染异常。三次模型请求均为本机合成接口；未向真实供应商发起付费请求。[打包结果](../artifacts/model-providers/packaged/result.json)、[浅色 Kimi 表单](../artifacts/model-providers/packaged/light-kimi-form.png)、[深色小窗口](../artifacts/model-providers/packaged/small-dark-kimi-form.png)。运行入口为 `node scripts/smoke-model-providers.mjs`；设置 `BUSINESSMAP_TEST_EXECUTABLE` 可选择打包程序。

最终 `npm run test:packaged` 两阶段通过：桌面主流程 **17 项**和冻结 MCP 只读检查，数据库保持不变。[套件结果](../artifacts/v0.2.9/packaged-suite.json)、[桌面记录](../artifacts/v0.2.9/packaged-smoke.json)、[MCP 记录](../artifacts/v0.2.9/packaged-mcp-smoke.json)。所有自动化使用 D 盘独立测试数据；原用户窗口正常关闭后，已使用 `D:\BusinessMapData` 重新打开新版，窗口正常响应。只读比较重启前后的 6 篇 Markdown SHA256 与 5 个应用数据库表的记录数，全部一致；行数比较不代表逐字段校验。[数据保留记录](../artifacts/user-data-preservation-v029.json)。

Windows 安装版与便携版均已生成，校验和见 [SHA256SUMS.txt](../release/SHA256SUMS.txt)。后端由当前源码重新冻结，交付文件 SHA256 为 `00f62854ac9539ec64d3bf261a2248aebfb5e14a24d51f0fd179bdaa56d756c8`。构建曾等待远端 Electron 下载，最终通过 `--config.electronDist` 复用本地已测试的同版本 Electron 44.6.0 完成；未更改项目运行时版本。本轮未重新运行图谱基准、九阶段全套或全量后端测试；未验证 macOS/Linux 原生安装、Windows 安装向导/卸载及真实供应商账号权限。

## 0.2.8 思径 · Sijing 品牌应用

正式采用「思径 · Sijing」及「看懂业务，找到依据」标语。标题栏、侧栏、问答欢迎页/头像、引导、窗口标题、Mac 菜单、启动错误说明、服务显示名与安装包使用新品牌。界面采用可随主题变色的 S 路径矢量标识；安装图标生成 1024px PNG 与含 16–256px 七种尺寸的 ICO。

更名保留 `io.businessmap.desktop` 应用身份、`businessmap` URL/IPC、Python 包名、`BUSINESSMAP_*` 环境变量、现有数据目录、系统凭据库命名空间及 MCP 工具名。MCP 的服务显示名改为 `Sijing`。模型系统提示只更新产品署名，旧前缀缓存可能不再命中，新前缀按供应商规则建立；不迁移模型配置或累计用量。

TypeScript 与生产构建通过；平台分支测试 **6 项通过**，包含三平台改名后继续使用旧数据目录、桌面会话存储及原页面来源。后端既有 MCP 查询、HTTP 问答模式、凭据 session fallback、Git 增量分析与人工修改保护 **4 项通过**。后端重新冻结，SHA256 为 `4FA74FAA0C3458FD9018977CB1D765B7DF2D35275E0AE965982CC5744378BC2B`。本轮未重复 94 项后端全量测试及图谱性能基准。

Windows 0.2.8 安装版与便携版已生成，程序资源中的产品名为 `Sijing`、版本为 `0.2.8`；已提取并检查实际可执行文件的 [原生图标](../artifacts/sijing-executable-icon-v028.png)。安装产物为 `Sijing-0.2.8-win-x64-setup.exe` 与 `Sijing-0.2.8-win-x64-portable.exe`，校验和见 [SHA256SUMS.txt](../release/SHA256SUMS.txt)。

针对最终 `release/win-unpacked/Sijing.exe`，`npm run test:packaged` 的桌面主流程 **17 项通过**，冻结 MCP 只读检查通过，服务显示名为 `Sijing`，查询前后数据库不变；本轮该命令运行桌面和 MCP 两阶段，未重复九阶段全量套件。[两阶段结果](../artifacts/v0.2.8/packaged-suite.json)、[桌面记录](../artifacts/v0.2.8/packaged-smoke.json)、[MCP 记录](../artifacts/v0.2.8/packaged-mcp-smoke.json)。原生窗口、快捷键、未保存保护与后端退出 **9 项通过**，无渲染异常：[平台记录](../artifacts/v0.2.8/platform/packaged-win32.json)。

最终打包程序视觉专项 **47 项通过，27 张截图**，覆盖浅深主题及 1440×940、1080×700 两种尺寸，未调用真实模型服务，无渲染异常。另人工复查四张截图，名称、标语及 S 标识没有裁切或溢出。[视觉记录](../artifacts/ux-visual-design/packaged-v028/result.json)、[浅色问答](../artifacts/ux-visual-design/packaged-v028/light-chat-empty.png)、[深色业务文档](../artifacts/ux-visual-design/packaged-v028/dark-knowledge.png)、[小窗口欢迎页](../artifacts/ux-visual-design/packaged-v028/small-light-chat-empty.png)。

自动化使用 D 盘独立测试目录。原窗口正常关闭后，已使用原有 `D:\BusinessMapData` 打开新版，原生窗口标题为「思径 Sijing · 业务地图」，进程正常响应。只读比较重启前后快照，6 篇 Markdown 的 SHA256 全部一致、5 个应用数据库表的记录数一致；此检查不导出配置或凭据内容，也不将行数比较表述为逐字段校验。[数据保留记录](../artifacts/user-data-preservation-v028.json)。本轮未实测 macOS/Linux 原生安装，未验证 Windows 安装向导、卸载及签名。

## 0.2.7 桌面视觉与小窗口体验

五个主入口保持原顺序，统一采用银白/石墨两套中性主题、系统字体、细边框、圆角和轻阴影。调整桌面侧栏、分段导航、模型设置分组、仓库卡片、查询与报表、聊天欢迎页/输入区、Markdown 阅读面板、图谱控件及引导。没有引入外部字体、图片或新依赖；Windows/Linux 保留原有窗口操作，macOS 继续使用原生窗口按钮。

实测修正：长表单滚动时固定标题和关闭按钮；提高深色关闭态开关的对比；Markdown 表格按内容收束且保留溢出滚动；空白新对话从欢迎内容顶部开始。有消息的会话保持原有滚动策略。地图右侧面板在不足 1280 CSS 像素时默认收起，可手动打开；手动选择在当前窗口的后续调整尺寸中保持。

最终 TypeScript 检查、Vite 生产构建通过，桌面平台分支测试 **5 项通过**。本轮后端实现未改，复用 0.2.6 的冻结服务，没有重复执行 94 项后端基线或图谱性能基准。冻结服务 SHA256 为 `90A655822B4D65F922724277962184B5786F399505841BE72E477FAC789C54AF`。

视觉复验使用独立 D 盘测试数据，覆盖 1440×940 和 1080×700、浅深主题、真实本地 Git 导入、带引用的本地知识答复、SQLite 只读查询及结果报表。模型卡片使用本地合成配置，不请求真实模型服务。源码中间轮 [记录与 27 张截图](../artifacts/ux-visual-design/final-source-v027/result.json) 检查 39 项通过；该轮截图发现新对话欢迎图标被滚动位置裁切，随后修正滚动逻辑，最终状态以打包复验为准。

Windows 0.2.7 打包程序视觉专项 **47 项通过，27 张截图**，无渲染异常、未调用模型。新增断言验证小窗口欢迎标识完整可见、长表单标题/关闭可见、地图右栏的响应式默认行为与手动选择保持。[打包记录](../artifacts/ux-visual-design/packaged-v027/result.json)、[浅色问答](../artifacts/ux-visual-design/packaged-v027/light-chat-empty.png)、[深色业务文档](../artifacts/ux-visual-design/packaged-v027/dark-knowledge.png)、[小窗口欢迎页](../artifacts/ux-visual-design/packaged-v027/small-light-chat-empty.png)。复验入口为 `node scripts/ux-visual-design.mjs`，通过 `BUSINESSMAP_TEST_EXECUTABLE` 选择打包程序，`BUSINESSMAP_VISUAL_LABEL` 区分产物目录。

首次完整回归发现 1080px 图谱引导卡片遮住 5.43% 的目标：侧边剩余 318.8px 低于原先 320px 的布局门槛，错误回退到 400px 覆盖卡片。将可侧放宽度门槛调整到 300px 后，沿用独立正文滚动和固定按钮行，普通尺寸 16 个位置样本全部无遮挡；未放宽验收断言。[原始失败](../artifacts/ux-visual-design/packaged-v027/initial-tour-failure.json)、[源码修正复验](../artifacts/ux-tour-review/source-v027-final.json)。

修正后重新生成 Windows 安装版/便携版，并从头运行 `npm run test:packaged:all`，**9 个阶段全部通过**：主流程 17 项、只读 MCP、原生窗口与快捷键 9 项、企业仓库 32 项、引导 46 项、键盘 37 项、工作区 8 项、引导定位/保存 9 项、125%/150% 缩放 7 项。[最终整套结果](../artifacts/v0.2.7/packaged-suite.json)、[普通尺寸引导](../artifacts/v0.2.7/ux-tour-review/packaged.json)、[放大显示](../artifacts/v0.2.7/ux-tour-review/packaged-zoom.json)。冻结后端保持上述相同 SHA256，安装包校验和见 `release/SHA256SUMS.txt`。测试使用独立目录，交付窗口使用原有 `D:\BusinessMapData`。

没有 Mac/Linux 设备，本轮不声称原生安装和视觉已实机验证。系统字体在不同平台呈现会有差异；减少动态效果/透明度时使用对应样式回退。安装向导、卸载、签名和真实远程环境的范围仍以下文待验证表为准。

## 0.2.6 企业仓库接入与模型绑定

支持 HTTPS 用户名与 Token/密码、SSH Agent 或已有私钥、公司 CA、代理、分支测试与选择、离线修改连接和清除凭据。后续同步复用仓库配置；同步失败保留已发布地图。每个仓库独立绑定模型目标，应用内部分析、全库问答检索和持久会话历史遵守该边界。原有仓库需重新显式绑定模型，原有索引与 Markdown 保留。外部 MCP 客户端、用户输入、手工移除归属的内容及数据库数据的模型授权仍需独立管理。

最终源码全量后端测试 **94 项通过**，记录见 [后端测试](../artifacts/backend-v026.log)。使用本地测试 CA 和真实 Git smart HTTP 服务验证 HTTPS 认证、证书拒绝、分支克隆/拉取、凭据轮换、重定向拒绝及失败保留地图；使用本地 SSH 服务和系统 OpenSSH 客户端验证私钥认证、克隆、拉取、未知主机拒绝、带空格/引号的路径及 SCP 相对路径语义。恶意本地 Git 配置不能改写网络请求目的地，认证头不进入命令参数或临时配置，凭据只进入系统凭据库或会话内存；凭据删除失败保留明确的重试状态。模型专项覆盖默认模型切换、目标配置变化、全库知识过滤、历史隔离以及发送前暂停/重绑竞态。

生产前端构建与五项桌面平台分支测试通过。实际源码 Electron 企业仓库流程 **32 项通过**，包括真实账号认证 HTTP Git、分支选择、凭据保存/清除、无模型导入、明确模型绑定、问答阻止错误模型，以及 1080×700 浅深主题表单布局。实测发现并修复小窗口提交栏位置及取消后草稿丢失；普通输入保留，密码清空。[源码流程与截图](../artifacts/ux-repository-connections/source.json)。

Windows 0.2.6 安装版与便携版已生成，冻结后端由本轮最终源码重建。完整 `npm run test:packaged:all` **9 个阶段全部通过**：桌面主流程 17 项、只读 MCP、原生窗口/快捷键 9 项、企业仓库 32 项、引导 46 项、键盘 37 项、工作区 8 项、引导定位/保存 9 项以及缩放 7 项。[完整运行记录](../artifacts/v0.2.6/packaged-suite.json)、[打包企业仓库检查](../artifacts/v0.2.6/ux-repository-connections/packaged.json)。测试使用 D 盘独立数据目录和 Chromium 沙箱。原窗口正常关闭后，已用 `D:\BusinessMapData` 重新打开新版；用户数据未被测试替换。安装包 SHA256 见 `release/SHA256SUMS.txt`。

企业连接测试只使用本地合成仓库和测试凭据，未连接真实公司内网/VPN、代理或真实 SSH Agent。没有 Mac/Linux 实机，因此本轮也不宣称其原生安装包、系统凭据库或 SSH 行为已验收；四目标构建工作流继续保留。安装向导/卸载及签名环节仍按下文待验证范围记录。

## 0.2.5 三平台兼容与构建

首版目标明确为 Windows x64、macOS Intel x64、macOS Apple Silicon arm64、Linux x64。现有单一 macOS 构建扩展为两种架构的原生任务，增加 Linux 运行库、中文字体及 Xvfb 环境；四个目标统一执行完整打包验收入口。构建前检查 Node/Python/目标架构一致，macOS 的 PyInstaller 参数分别使用 `x86_64` 与 `arm64`，防止安装包外壳与后端架构不匹配。工作流没有发布权限，尚未在 GitHub 执行。

桌面端补齐 macOS 菜单、原生窗口按钮、最后关窗保留应用及 Dock 重新打开窗口；应用协议和安全设置移至启动时一次注册。Windows/Linux 最后关窗退出，真正退出时结束后端。平台分支测试 5 项通过，执行的是实际主进程代码配合模拟 Electron/OS 边界，不能替代 Mac/Linux 实机验证。Windows 源码运行 9 项通过，包括系统编辑、撤销、保存快捷键、原生关闭取消后保留未保存笔记、最后关窗与后端退出；[源码结果](../artifacts/platform/source-win32.json)。关闭确认的测试由驱动选择“继续编辑”，没有修改产品绕过确认。

本地还验证了构建脚本语法、工作流四目标配置、正常 Windows x64 环境检查与不匹配架构拒绝。当前设备未安装 WSL Linux 发行版，用户也没有 Mac/Linux 设备或 GitHub 仓库，因此 **macOS/Linux 原生安装包和运行验收仍未完成**。平台目标、原生构建步骤和可执行的 CI 入口见 [PLATFORMS.md](PLATFORMS.md)。

Windows 0.2.5 安装版和便携版已生成；完整 `npm run test:packaged:all` 在包内程序上执行通过，共 8 个阶段：桌面主流程 17 项、只读 MCP、原生窗口与快捷键 9 项、引导 46 项、键盘 37 项、工作区 8 项、引导定位/保存 9 项及缩放 7 项。[整套运行记录](../artifacts/v0.2.5/packaged-suite.json)、[平台原生检查](../artifacts/v0.2.5/platform/packaged-win32.json)。首次整套执行发现测试配置中的空缩放值被解析为 0，已修正测试参数解析并从头复验，最终全部通过。冻结后端与 0.2.4 相同，未重复 68 项后端基线测试；已有用户数据保留，0.2.5 窗口已重新打开。

## 0.2.4 实际操作复验

在独立知识库中重新操作完整流程后，复现并修正以下问题。失败基线保留在 [引导探索](../artifacts/ux-tour-review/baseline.json)、[放大显示探索](../artifacts/ux-tour-review/baseline-zoom.json) 和 [工作区探索](../artifacts/ux-workflow-review/baseline.json)，不使用修复后结果覆盖。

| 问题 | 修复后的行为 |
| --- | --- |
| 小窗口的引导卡遮挡数据源、聊天输入和图谱 | 按实际空间调整卡片宽度及位置，优先放在目标旁边；空间较小时只滚动说明正文 |
| 放大显示后，换步骤仍停在上一页滚动位置，标题或导航按钮消失 | 固定标题、关闭及导航按钮，每一步正文回到顶部；说明区支持键盘滚动 |
| 图谱「去操作」后，焦点回到引导入口，方向键不能直接操作图谱 | 识别可聚焦的画布并恢复焦点 |
| 引导打开时 Ctrl+S 保存了背后的未保存笔记 | 对话框打开时阻止背景保存；关闭引导后保留正常保存快捷键 |
| 切换知识库后，旧标签让新库笔记全部隐藏，且无清除入口 | 清空旧库标签、搜索结果及折叠状态 |
| 笔记加载中关闭最后一个标签，仍一直显示加载中 | 立即显示未选中笔记的空态，迟到响应不会恢复已关闭笔记 |

源码生产构建通过。工作区专项 8 项通过；引导位置与快捷键专项在 1280×700、1080×700 下验证全部 8 步目标与卡片无重叠，标题和操作按钮可见、焦点正确恢复、说明回顶。引导中 Ctrl+S 没有写入笔记，关闭后正常保存成功。上述受控笔记写入只发生在独立测试目录。

另在 1080×700 窗口下将 Electron 内容缩放到 125% 和 150%，所有步骤的标题、关闭和导航按钮可见，正文可滚动且换步复位；从图谱「去操作」后可立即用方向键平移。放大后空间不足时，卡片仍可能遮挡部分较大目标，暂停引导即可完整操作；本轮不声称所有缩放下都完全无重叠。

Windows 0.2.4 安装版与便携版已生成，SHA256 见 `release/SHA256SUMS.txt`。针对打包程序的 17 项桌面主流程检查及冻结 MCP 检查通过：[桌面结果](../artifacts/v0.2.4/packaged-smoke.json)、[MCP 结果](../artifacts/v0.2.4/packaged-mcp-smoke.json)。后端实现及冻结服务未变，未重复后端 68 项基线和图谱性能测试；macOS/Linux、实际远程模型和数据库的验收状态仍以下文限制为准。

最终 0.2.4 打包版专项：工作区 8 项通过（[结果](../artifacts/v0.2.4/ux-workflow-review/packaged.json)）；引导布局与快捷键 9 项通过（[结果](../artifacts/v0.2.4/ux-tour-review/packaged.json)）；放大显示 7 项通过（[结果](../artifacts/v0.2.4/ux-tour-review/packaged-zoom.json)）。普通尺寸与缩放场景各覆盖全部 8 步、14 次「去操作」；所有专项均无 renderer 错误。

原引导完整回归 46 项通过（[结果](../artifacts/v0.2.4/ux-onboarding/packaged.json)），观察到的 110 个接口请求全部为 GET，业务快照不变。新增键盘与布局专项 37 项通过（[结果](../artifacts/v0.2.4/ux-onboarding/keyboard-packaged.json)），涵盖 PageDown/End 滚动说明、固定头尾、换步回顶、Tab/Shift+Tab 焦点约束、浅深主题与缩窗；图谱、聊天步骤最窄卡宽分别为 334px、368px。所有专项使用独立 D 盘数据，用户窗口正常关闭后已用原数据目录重新打开 0.2.4。

复验脚本为 `scripts/ux-workflow-explore.mjs --verify` 和 `scripts/ux-tour-explore.mjs`；通过 `BUSINESSMAP_TEST_EXECUTABLE` 选择打包程序。引导探索设置 `BUSINESSMAP_TEST_ASSERT=1` 启用回归断言，设置 `BUSINESSMAP_TEST_ZOOM_SET=1.25,1.5` 检查 Electron 内容缩放。缩放检查不代表真实显示器 DPI 或所有辅助技术验收。

## 0.2.3 界面高亮引导

新增 8 步原生对话框引导，依次介绍主导航、业务笔记、关系图谱、模型、仓库、数据库与报表、AI 问答和可选 MCP。支持上一步、暂停去操作、跳过、继续和重看。引导只保存本地阅读进度，不将浏览教程当作已完成业务配置。

TypeScript、Vite 构建及 Windows 安装版/便携版打包通过。针对最终打包程序执行的引导专项 **46 项通过**：首次自动展示、真实目标高亮、暂停与重启恢复、完成状态、无目标时可退出、损坏偏好恢复、原生焦点约束、实际模型表单操作、未保存 Markdown 离开保护，以及聊天草稿保留。浅深主题和 1080×700 窗口均检查；窗口缩小后，位于滚动区域的目标按钮仍会滚动到可见位置并保持高亮。

专项观察到的 110 个接口请求均为 GET；操作前后 Markdown 与业务数据快照一致，没有自动执行 SQL、提交模型测试或触发仓库分析，renderer 错误为 0。[机器结果](../artifacts/v0.2.3/ux-onboarding/packaged.json)、[浅色截图](../artifacts/v0.2.3/ux-onboarding/packaged-light.png)、[深色小窗口截图](../artifacts/v0.2.3/ux-onboarding/packaged-small-dark.png)。测试使用独立 D 盘目录。

原有桌面 **17 项检查与冻结 MCP 检查通过**，覆盖图谱、查询、报表、问答、主题和只读约束：[桌面结果](../artifacts/v0.2.3/packaged-smoke.json)、[MCP 结果](../artifacts/v0.2.3/packaged-mcp-smoke.json)。旧功能测试通过真实「暂时跳过」按钮关闭首启引导；引导专项独立验证完整流程。后端未变更，未重复执行 68 项后端基线测试；本轮没有重复图谱性能测量。当前实机验收仍仅 Windows，未新增 macOS/Linux 实测结论。

教程内容与恢复说明见 [GETTING-STARTED.md](GETTING-STARTED.md)。可运行 `node scripts/ux-onboarding.mjs` 复验；指定 `BUSINESSMAP_TEST_EXECUTABLE` 则使用对应打包程序。

## 0.2.2 图谱交互复验

TypeScript 检查、Vite 生产构建与 Windows 安装版/便携版打包通过。以下检查针对最终 `release/win-unpacked/BusinessMap.exe`，不是开发服务器；测试数据位于 D 盘独立目录，未修改用户知识库。

- `npm run test:packaged`：17 项桌面检查通过，覆盖五入口、浅深主题、Markdown、只读查询、报表和问答会话；冻结服务的 MCP 检查通过，违规 SQL 被拒绝且数据库未变化。[桌面结果](../artifacts/v0.2.2/packaged-smoke.json)、[MCP 结果](../artifacts/v0.2.2/packaged-mcp-smoke.json)。
- 图谱日常操作：11 项通过，覆盖真实 Worker 布局、空筛选恢复、主题与坐标保留、减少动态效果、拖动与点击区分、知识库切换隔离和 1080 px 窗口无横向溢出。[结果](../artifacts/ux-graph/lifecycle-packaged.json)。
- Canvas 回退：12 项通过。在图谱模块加载前让 WebGL2 上下文创建返回 `null`，确认使用真实 Canvas 并通过同样的操作；这是受控能力失败测试，未宣称在无 GPU 硬件上测试。[结果](../artifacts/ux-graph/lifecycle-packaged-fallback.json)。
- 缓存与布局生命周期：300 篇真实 Markdown、14 项通过。未完成的缓存继续收敛，已稳定的缓存恢复不移动、不重新加热；按住节点时邻居连续运动；实际零尺寸画布暂停 Worker 输出，恢复后继续布局；标签确有像素输出。[结果](../artifacts/ux-graph/resume-packaged.json)。
- 300、600 节点交互各 18 项必需检查通过。首开最长主线程任务分别为 126 ms、201 ms；两组都未达到更严格的 100 ms 质量目标，报告保留警告。筛选、笔记返回与窗口调整检查中的视角和坐标偏移为零。[300 节点](../artifacts/ux-graph/packaged-300.json)、[600 节点](../artifacts/ux-graph/packaged-600.json)。

实际打开本机 Obsidian 1.13.4，使用独立配置和 150 篇合成笔记体验交互；参考记录见 [GRAPH-REFERENCE.md](GRAPH-REFERENCE.md)。最终 300、600 节点的性能数据、原版对照及截图见 [GRAPH-PERFORMANCE.md](GRAPH-PERFORMANCE.md)。首次绘制仍有短暂初始化，不承诺恒定帧率或完全等同 Obsidian 的表现。

原生最小化在自动化会话中没有使 `document.hidden` 变为 `true`，因此不声称覆盖原生 visibility 暂停分支；已验证真实 CSS 零尺寸触发的暂停与恢复。本轮没有修改后端实现，复用同一冻结服务并重新验证 MCP；68 项后端测试结论仍来自 0.2.0 基线。macOS 与 Linux 尚未实机验收。

## 0.2.1 界面修正复验

0.2.1 生产构建、类型检查及 `npm run test:packaged` 全部通过。打包程序验证五入口、主题、笔记、SQLite 查询、报表与会话，冻结 MCP 验证只读执行与数据不变；记录明确包含桌面版本 0.2.1。另对打包程序执行 7 项工作区回归，检查草稿及结果保留、保存中继续输入、最小窗口面板和外链接口。源码专项与实际发现见 [UX-REVIEW.md](UX-REVIEW.md)。

本轮只修改桌面与界面代码，未重复执行后端 68 项测试；下列后端测试结论来自 0.2.0 基线。0.2.1 仍重新运行了打包服务的 MCP 检查。

## 已执行

- Windows x64：TypeScript 检查和 Vite 生产构建通过。
- 后端 68 项自动化测试通过。覆盖 Markdown 持久化与冲突、Git 提交更新、自动分析状态、人工修改候选、模型适配器、数据库权限与结果边界，以及持久会话、模型/仓库作用域、跨进程并发与中断恢复。
- 桌面独立包：由 Playwright 操作 Electron，验证五入口顺序、浅深主题与主题持久化、笔记读写、冲突拒绝、双链图谱、真实 SQLite 查询、拒绝写入后数据不变、结果绑定报表、指标切换和模型/MCP 设置。测试显式开启 Chromium 沙箱。
- AI 问答：实际发送知识问题并验证来源、历史记录重载、跨页面和跨会话草稿保留。界面呈现真实请求等待状态，不模拟模型流式输出。
- 冻结 MCP 服务：使用真实 stdio 客户端完成初始化、列出六个工具、读取知识和 schema、执行聚合查询。示例订单数为 8，完成退款金额为 190。六类违规 SQL 被拒绝，数据库 SHA256 未变化，知识库路径穿越被拒绝。
- AI 适配器使用受控 HTTP 服务响应验证；Git 自动分析使用真实临时仓库提交与受控模型响应验证。

机器可读结果与界面截图位于 `artifacts/`；该目录不提交到源码仓库。`npm run test:packaged` 可重复执行桌面与 MCP 验收，测试使用独立的项目 `.tmp/` 数据目录。

隐藏的 Electron 测试窗口使用原生 `webContents.capturePage` 截图，预热绘制后再保存；最终打包截图启用减少动态效果设置以避免主题过渡中间帧。当前 Windows 环境中 Playwright 的截图偶有绘制等待超时；原生截图已与正常 Playwright 截图逐像素核对一致，所有 UI 操作与结果断言仍由 Playwright 执行。

## 待验证

| 环境 | 当前状态 |
| --- | --- |
| macOS / Linux | 已配置原生构建及桌面/MCP 测试工作流，尚未实际执行 |
| Windows 安装向导、卸载与便携自解压 | 已生成安装版与便携版；应用验证针对安装包包含的 unpacked 程序 |
| Apple 签名、公证、Gatekeeper；Linux DEB 安装和 AppImage 挂载 | 未验证 |
| 真实模型供应商 | 需要用户配置测试账号与模型；未使用任何真实 API 密钥 |
| PostgreSQL / MySQL | 适配器与权限测试通过，尚未连接真实远程实例验收 |
| 大型业务仓库语义准确率 | 尚未建立业务评估集；当前分析明确限制范围 |

三平台首版支持是产品要求；当前预览不能视作已经完成三平台发布验收。仓库尚未发布到 GitHub，原生 CI 尚未运行。Windows 预览未配置数字签名。

## 已知检查提示

后端测试有一个 Starlette TestClient/httpx 的弃用提示；没有测试失败。前端构建提示按需加载的 Markdown 编辑器代码块超过 500 kB，当前没有发现功能异常。
