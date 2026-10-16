# Obsidian 图谱交互实测参考

2026-10-08，在本机实际启动 `D:\Obsidian\Obsidian.exe`（Obsidian 1.13.4），通过真实窗口体验图谱。使用 D 盘独立配置与合成 vault：6 个业务中心、每中心 24 个规则，共 **150 篇 Markdown**，包含组内链接及跨组链接。未读取或修改用户的 Obsidian 配置和真实笔记，未提取或复制其闭源实现。

## 已观察的行为

| 交互 | 实际效果 | 对 BusinessMap 的意义 |
| --- | --- | --- |
| 拖动节点 | 被抓住的节点跟随指针，邻居被逐步拉动；释放后局部继续松弛 | 拖动期间继续运行力导向布局，释放后逐渐停止，避免硬切坐标或永久钉住节点 |
| 滚轮缩放 | 以鼠标所在位置为锚点，经历中间帧后达到目标尺度 | 滚轮累计目标尺度，在动画帧内平滑逼近，保持指针对应的图内位置 |
| 拖动空白 | 整张图同步平移 | 平移过程不重跑全图初始化，不改变节点间相对位置 |
| 悬停节点 | 当前节点、直接邻居和连接边保持清晰，其余内容变暗 | 让关系成为视觉焦点；鼠标移开恢复原图 |
| 缩放标签 | 总览时隐藏标签；放大后逐渐出现；悬停节点的名称仍可读 | 按缩放尺度控制文字可见度，避免低倍率全屏文字重叠 |
| 打开笔记 | 单击节点打开对应 Markdown；点击“返回”可回到图谱 | 拖动与点击分开判断，并保存图谱视角与布局状态 |
| 总览视觉 | 节点为小圆点，连接多的中心更大，边较细且对比度低 | 用节点大小和悬停表达结构，无需持续展示粗边、箭头与长文本 |

BusinessMap 保留用户要求的黑白灰主题；参考交互反馈强弱，不复制 Obsidian 的紫色强调色。

## 验证与证据

运行 `node scripts/observe-obsidian-graph.mjs` 可创建独立合成 vault 并重复操作。安装版 Obsidian 的主进程 inspector 不可用，因此脚本通过 Chromium CDP 连接真实窗口，Ctrl+G 打开图谱，鼠标完成悬停、拖动、缩放及节点点击，点击实际“返回”按钮回图谱。测试结束关闭该实例。

- 总览中通过截图识别到全部 **150 个节点**。
- 在离中心的指针处放大，截图中 **83 个节点**符合围绕指针约 **1.805 倍**的坐标变换，平均拟合误差约 **1.36 px**。测量仅使用截图像素，不读取内部图谱实现。
- 拖动期间采样 **504 个 requestAnimationFrame 间隔**：中位数 7 ms，P95 7.3 ms，最大 8.9 ms；未出现超过 33.34 ms 的样本。这是当前 Windows/CDP 窗口的渲染线程调度采样，包含自动化与截图开销，不能等同 GPU 呈现帧率，也不构成与 BusinessMap 的性能比较。
- 全部 7 项操作记录通过，无 renderer JavaScript error。

数据见 [obsidian-graph-observation.json](../artifacts/obsidian-graph-observation.json)。主要截图：

- [150 节点总览](../artifacts/obsidian-graph-overview.png)
- [悬停与邻居突出](../artifacts/obsidian-graph-hover.png)
- [拖拽中](../artifacts/obsidian-graph-drag-held.png) / [释放后布局松弛](../artifacts/obsidian-graph-drag-settled.png)
- [缩放前](../artifacts/obsidian-graph-zoom-before.png) / [缩放中](../artifacts/obsidian-graph-zoom-progress.png) / [缩放后](../artifacts/obsidian-graph-zoom-after.png)
- [单击打开笔记](../artifacts/obsidian-graph-note-opened.png) / [返回图谱](../artifacts/obsidian-graph-returned.png)

这些是行为参考，不能据此承诺所有规模和设备都同样流畅。BusinessMap 改动仍需要使用其实际数据与运行窗口独立验收。
