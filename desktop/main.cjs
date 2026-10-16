const { app, BrowserWindow, dialog, ipcMain, Menu, net, protocol, session, shell } = require('electron');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const nodeNet = require('node:net');

const root = path.resolve(__dirname, '..');
app.setName('Sijing');
if (process.platform === 'win32') app.setAppUserModelId('io.businessmap.desktop');
const isDev = !app.isPackaged;
const devUrl = isDev && process.env.BUSINESSMAP_DEV_URL;
// Keep the established data location and storage identities across the rebrand.
const dataDir = path.resolve(process.env.BUSINESSMAP_DATA_DIR || (isDev
  ? path.join(root, '.local-data')
  : process.platform === 'win32' && fs.existsSync('D:\\')
    ? 'D:\\BusinessMapData'
    : path.join(app.getPath('appData'), 'BusinessMap')));
fs.mkdirSync(dataDir, { recursive: true });
fs.mkdirSync(path.join(dataDir, 'logs'), { recursive: true });
fs.mkdirSync(path.join(dataDir, 'tmp'), { recursive: true });
app.setPath('userData', path.join(dataDir, 'desktop'));
app.setPath('sessionData', path.join(dataDir, 'desktop', 'session'));
app.setPath('crashDumps', path.join(dataDir, 'crashes'));
app.setAppLogsPath(path.join(dataDir, 'logs'));
app.commandLine.appendSwitch('disk-cache-dir', path.join(dataDir, 'desktop', 'cache'));
protocol.registerSchemesAsPrivileged([{ scheme: 'businessmap', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);

let window;
let backend;
let quitting = false;
let serviceUrl;
const token = crypto.randomBytes(32).toString('hex');
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function availablePort() {
  return new Promise((resolve, reject) => {
    const server = nodeNet.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function api(method, endpoint, body) {
  if (!serviceUrl) throw new Error('本地服务尚未就绪');
  const response = await fetch(`${serviceUrl}${endpoint}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    redirect: 'error',
    signal: AbortSignal.timeout(endpoint === '/health' ? 2000 : 180_000),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = result.detail || result.error || `请求失败 (${response.status})`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return result;
}

async function startBackend() {
  const port = await availablePort();
  serviceUrl = `http://127.0.0.1:${port}`;
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', process.platform === 'win32' ? 'businessmap-service.exe' : 'businessmap-service')
    : process.env.BUSINESSMAP_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  if (!fs.existsSync(executable)) {
    throw new Error(isDev ? '尚未安装后端环境。请先运行 node scripts/setup.mjs。' : '安装文件不完整：缺少本地服务程序，请重新安装。');
  }
  const args = [...(app.isPackaged ? [] : ['-m', 'businessmap']), '--data-dir', dataDir, '--port', String(port)];
  const logPath = path.join(dataDir, 'logs', 'backend.log');
  if (fs.existsSync(logPath) && fs.statSync(logPath).size > 2_000_000) fs.renameSync(logPath, `${logPath}.${Date.now()}`);
  const log = fs.createWriteStream(logPath, { flags: 'a' });
  backend = spawn(executable, args, {
    cwd: app.isPackaged ? dataDir : path.join(root, 'backend'),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env, BUSINESSMAP_TOKEN: token, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1',
      TEMP: path.join(dataDir, 'tmp'), TMP: path.join(dataDir, 'tmp'), TMPDIR: path.join(dataDir, 'tmp'),
    },
  });
  let startupError;
  backend.on('error', (error) => { startupError = error; });
  backend.stdout.pipe(log, { end: false });
  backend.stderr.pipe(log, { end: false });
  backend.on('exit', () => {
    log.end();
    if (!quitting && window) dialog.showErrorBox('本地服务已停止', '请重新启动思径。详细错误保存在应用数据目录的 logs/backend.log。');
  });
  for (let attempt = 0; attempt < 240; attempt++) {
    if (startupError) throw startupError;
    if (backend.exitCode !== null) throw new Error('本地服务启动失败，请查看应用数据目录中的 logs/backend.log。');
    try {
      await api('GET', '/health');
      return;
    } catch { await delay(250); }
  }
  throw new Error('本地服务启动超时，请查看 logs/backend.log。');
}

function checkSender(event) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) {
    throw new Error('未授权的桌面请求');
  }
  const url = event.senderFrame.url;
  if (!(url.startsWith('businessmap://desktop/') || (devUrl && new URL(url).origin === new URL(devUrl).origin))) {
    throw new Error('未授权的页面来源');
  }
}

function installHandlers() {
  ipcMain.handle('businessmap:open-external', async (event, value) => {
    checkSender(event);
    if (typeof value !== 'string' || value.length > 8192) throw new Error('无效链接');
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || !url.hostname || url.username || url.password) throw new Error('仅支持 HTTP 或 HTTPS 网页链接');
    await shell.openExternal(url.href);
  });
  ipcMain.handle('businessmap:request', async (event, payload) => {
    checkSender(event);
    if (!payload || !['GET', 'POST', 'PUT', 'DELETE'].includes(payload.method)
      || typeof payload.path !== 'string' || !payload.path.startsWith('/api/')
      || /[\\\r\n#]/.test(payload.path) || payload.path.length > 8192) throw new Error('无效请求');
    const normalized = new URL(payload.path, 'http://localhost');
    if (!normalized.pathname.startsWith('/api/') || normalized.origin !== 'http://localhost') throw new Error('无效路径');
    if (payload.body !== undefined && JSON.stringify(payload.body).length > 2_000_000) throw new Error('请求内容过大');
    return api(payload.method, payload.path, payload.body);
  });
  ipcMain.handle('businessmap:choose-directory', async (event) => {
    checkSender(event);
    const result = await dialog.showOpenDialog(window, { title: '选择文件夹', properties: ['openDirectory', 'createDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('businessmap:choose-file', async (event) => {
    checkSender(event);
    const result = await dialog.showOpenDialog(window, { title: '选择 SQLite 数据库', properties: ['openFile'], filters: [{ name: 'SQLite 数据库', extensions: ['db', 'sqlite', 'sqlite3'] }, { name: '所有文件', extensions: ['*'] }] });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('businessmap:window-control', (event, action) => {
    checkSender(event);
    if (action === 'minimize') window.minimize();
    else if (action === 'maximize') window.isMaximized() ? window.unmaximize() : window.maximize();
    else if (action === 'close') window.close();
  });
  ipcMain.handle('businessmap:open-vault', async (event) => {
    checkSender(event);
    const workspace = await api('GET', '/api/workspace');
    if (typeof workspace.vault_path !== 'string' || !path.isAbsolute(workspace.vault_path)) throw new Error('无效知识库路径');
    const error = await shell.openPath(workspace.vault_path);
    if (error) throw new Error(error);
  });
}

// Protocol and session policies belong to the application, not an individual
// window: macOS can close every window and later reopen from the Dock.
function installProtocol() {
  const dist = path.join(root, 'dist');
  protocol.handle('businessmap', (request) => {
    try {
      const url = new URL(request.url);
      const target = path.resolve(dist, `.${decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname)}`);
      const relative = path.relative(dist, target);
      if (url.hostname !== 'desktop' || relative.startsWith('..') || path.isAbsolute(relative) || !fs.existsSync(target) || !fs.statSync(target).isFile()) return new Response('Not found', { status: 404 });
      return net.fetch(pathToFileURL(target).toString());
    } catch { return new Response('Bad request', { status: 400 }); }
  });
  const devOrigin = devUrl ? new URL(devUrl).origin : null;
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const csp = ["default-src 'self'", `script-src 'self'${devUrl ? " 'unsafe-inline'" : ''}`, "style-src 'self' 'unsafe-inline'", "img-src 'self' data: blob:", "font-src 'self' data:", devUrl ? `connect-src 'self' ${devOrigin} ${devOrigin.replace('http:', 'ws:')}` : "connect-src 'none'", "worker-src 'self'", "object-src 'none'", "base-uri 'none'", "frame-src 'none'"].join('; ');
    callback({ responseHeaders: { ...details.responseHeaders, 'Content-Security-Policy': [csp] } });
  });
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
}

function installMacMenu() {
  if (process.platform !== 'darwin') return;
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu', label: '思径 Sijing' },
    { label: '文件', submenu: [{ role: 'close', label: '关闭窗口' }] },
    { role: 'editMenu', label: '编辑' },
    { label: '显示', submenu: [{ role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' }, { type: 'separator' }, { role: 'togglefullscreen', label: '切换全屏' }] },
    { role: 'windowMenu', label: '窗口' },
  ]));
}

async function createWindow() {
  const devOrigin = devUrl ? new URL(devUrl).origin : null;
  window = new BrowserWindow({
    width: 1500, height: 940, minWidth: 1080, minHeight: 700,
    title: '思径 Sijing · 业务地图', backgroundColor: '#fafafa', show: false,
    icon: path.join(root, 'build-resources', process.platform === 'win32' ? 'icon.ico' : 'icon.png'),
    frame: process.platform === 'darwin',
    ...(process.platform === 'darwin' ? { titleBarStyle: 'hiddenInset' } : {}),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, spellcheck: false },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-prevent-unload', (event) => {
    const choice = dialog.showMessageBoxSync(window, {
      type: 'question', title: '尚有未保存的修改',
      message: '当前笔记尚未保存。', detail: '继续编辑可保留本次修改；关闭将丢弃未保存的内容。',
      buttons: ['继续编辑', '放弃修改并关闭'], defaultId: 0, cancelId: 0, noLink: true,
    });
    if (choice === 1) event.preventDefault();
  });
  window.webContents.on('will-navigate', (event, url) => {
    if (!(url.startsWith('businessmap://desktop/') || (devOrigin && new URL(url).origin === devOrigin))) event.preventDefault();
  });
  window.once('ready-to-show', () => { if (process.env.BUSINESSMAP_HEADLESS !== '1') window.show(); });
  window.on('closed', () => { window = null; });
  await window.loadURL(devUrl || 'businessmap://desktop/index.html');
  if (isDev && process.env.BUSINESSMAP_DEVTOOLS === '1') window.webContents.openDevTools({ mode: 'detach' });
}

app.on('will-quit', () => { quitting = true; backend?.kill(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
process.on('exit', () => backend?.kill());
app.whenReady().then(async () => {
  installHandlers();
  installProtocol();
  installMacMenu();
  await startBackend();
  await createWindow();
  if (process.platform === 'darwin') app.on('activate', () => {
    if (quitting) return;
    if (!window) {
      void createWindow().catch((error) => dialog.showErrorBox('无法打开思径', error.message));
    } else {
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
    }
  });
  ipcMain.handle('businessmap:choose-repository-file', async (event, kind) => {
    checkSender(event);
    const titles = { ca: '选择公司 CA 证书', 'ssh-key': '选择 SSH 私钥文件', 'known-hosts': '选择 SSH 已知主机文件' };
    if (!Object.hasOwn(titles, kind)) throw new Error('无效的仓库文件类型');
    const filters = kind === 'ca'
      ? [{ name: 'CA 证书', extensions: ['pem', 'crt', 'cer'] }, { name: '所有文件', extensions: ['*'] }]
      : [{ name: '所有文件', extensions: ['*'] }];
    const result = await dialog.showOpenDialog(window, { title: titles[kind], properties: ['openFile'], filters });
    return result.canceled ? null : result.filePaths[0];
  });
}).catch((error) => {
  quitting = true;
  backend?.kill();
  dialog.showErrorBox('思径启动失败', error.message);
  app.quit();
});
