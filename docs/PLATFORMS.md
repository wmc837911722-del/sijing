# 三平台构建与验收

Windows、macOS 和 Linux 都是首版目标。主功能、只读数据库边界、Markdown 知识库和引导流程保持一致；窗口和快捷键遵循各平台习惯。

| 平台 | 首版架构 | 安装包 | 原生构建环境 | 当前实际验证 |
| --- | --- | --- | --- | --- |
| Windows | x64 | 安装 EXE、便携 EXE | Windows 2022 runner | 已在本机运行与验证 |
| macOS / Intel | x64 | DMG、ZIP | macos-15-intel runner | 原生构建与运行待验证 |
| macOS / Apple Silicon | arm64 | DMG、ZIP | macos-15 runner | 原生构建与运行待验证 |
| Linux | x64 | AppImage、DEB | Ubuntu 22.04 runner | 原生构建与运行待验证 |

当前只有 Windows 开发环境，没有可用的 Mac、Linux 发行版或远程仓库。本地平台分支测试通过不等于这些系统已完成实机验收，也尚未生成它们的安装包。Linux 当前面向 Ubuntu/Debian 生态，不承诺已覆盖所有发行版、桌面环境或 ARM 设备。

## 已补齐的兼容处理

- macOS 使用原生窗口按钮和菜单，支持系统编辑快捷键。关闭最后一个窗口时保留应用及本地服务，从 Dock 激活后重新打开窗口；退出应用时结束服务。
- Windows/Linux 保留应用内窗口按钮，关闭最后一个窗口时退出并结束服务。三平台关闭未保存的 Markdown 时都保留离开确认。
- 应用协议、访问限制只在启动时注册一次，避免 macOS 重开窗口时重复注册。
- 数据、凭据和后端路径按系统处理。Windows 优先使用 D 盘；macOS/Linux 使用系统应用数据位置，也可通过 `BUSINESSMAP_DATA_DIR` 指定位置。
- 保存快捷键测试在 Mac 使用 Command，在 Windows/Linux 使用 Control。测试仓库路径由当前系统生成。
- Node、Python 与打包目标必须是同一系统和架构。PyInstaller 无法把 Windows 后端直接变成 Mac/Linux 程序，不能只修改安装包扩展名或只跨编译 Electron 外壳。

## 自动构建

[工作流](../.github/workflows/build.yml) 已配置四个独立原生任务。将项目放入获准使用的 GitHub 仓库后，向 `main` 推送、提交 PR、推送版本标签或手动执行 Desktop builds 均可触发。当前尚未上传项目或执行远程工作流。

每个任务执行：

1. 安装匹配架构的 Node 24、Python 3.12 及依赖；Linux 额外安装显示库、中文字体和虚拟显示服务。
2. 执行后端测试和桌面平台分支测试。
3. 在当前原生系统冻结 Python 服务，并生成对应架构的桌面安装包。
4. 对包内可执行程序执行 `npm run test:packaged:all`：主流程、只读 MCP、系统窗口与快捷键、企业仓库连接与模型绑定、引导、键盘、工作区以及内容缩放验收。Linux 通过 Xvfb 执行，保留 Chromium 沙箱。
5. 验证通过后上传安装包；无论通过或失败，都尽可能保留验收结果。

安装包和测试报告按系统、架构分别保存，避免一种平台的通过结果代表全部平台。`artifacts/packaged-suite.json` 记录本次实际系统、架构及逐项执行结果。

## 本机原生构建

在对应系统与架构上安装 Node 24、Python 3.12、Git 和 OpenSSH 客户端，然后执行：

```sh
node scripts/setup.mjs
npm ci
node scripts/build-backend.mjs --check
npm run test:platform
npm run package
npm run test:packaged:all
```

不应复用另一种系统或架构的 `.venv`、`node_modules` 或冻结后端产物。无显示的 Linux 环境需按工作流安装系统库，并使用 `xvfb-run --auto-servernum --server-args="-screen 0 1920x1200x24" npm run test:packaged:all`。

当前工作流生成开发预览包。macOS 的签名、公证与 Gatekeeper 验收，Windows 数字签名，以及各平台安装/卸载过程仍需正式发布前完成；包内程序的自动化通过不代表这些发布环节已经通过。
