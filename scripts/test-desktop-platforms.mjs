import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// Execute the real main process with Electron/OS boundaries replaced. These
// checks cover lifecycle branches; native desktop smoke runs separately.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(resolve(root, 'desktop/main.cjs'), 'utf8');
const require = createRequire(import.meta.url);

async function boot(platform, useDefaultData = false) {
  const state = { windows: [], spawns: [], kills: 0, quits: 0, protocols: 0, errors: [], menu: null, paths: {}, appName: 'BusinessMap', appId: null };
  const app = Object.assign(new EventEmitter(), {
    isPackaged: true, commandLine: { appendSwitch() {} },
    getPath: () => '/user/app-data', setPath: (key, value) => { state.paths[key] = value; }, setAppLogsPath() {},
    getName: () => state.appName, setName: name => { state.appName = name; }, setAppUserModelId: id => { state.appId = id; }, whenReady: () => Promise.resolve(),
    quit: () => { state.quits++; app.emit('will-quit'); },
  });
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.minimized = false;
      this.shown = false;
      this.focused = false;
      this.webContents = Object.assign(new EventEmitter(), {
        setWindowOpenHandler() {}, openDevTools() {}, mainFrame: {},
      });
      state.windows.push(this);
    }
    async loadURL(url) { this.url = url; this.emit('ready-to-show'); }
    isMinimized() { return this.minimized; }
    restore() { this.minimized = false; }
    show() { this.shown = true; }
    focus() { this.focused = true; }
    close() { this.emit('closed'); app.emit('window-all-closed'); }
  }
  const electron = {
    app, BrowserWindow,
    Menu: { buildFromTemplate: template => template, setApplicationMenu: menu => { state.menu = menu; } },
    dialog: { showErrorBox: (...error) => state.errors.push(error), showMessageBoxSync: () => state.discard ? 1 : 0 },
    ipcMain: { handle() {} }, net: {}, shell: {},
    protocol: {
      registerSchemesAsPrivileged() {},
      handle: () => { if (++state.protocols > 1) throw new Error('Protocol already registered'); },
    },
    session: { defaultSession: { webRequest: { onHeadersReceived() {} }, setPermissionRequestHandler() {}, setPermissionCheckHandler() {} } },
  };
  const fakeFs = { mkdirSync() {}, existsSync: () => true, statSync: () => ({ size: 0 }), createWriteStream: () => ({ end() {} }) };
  const spawn = (executable, args) => {
    state.spawns.push({ executable, args });
    return Object.assign(new EventEmitter(), {
      exitCode: null, stdout: { pipe() {} }, stderr: { pipe() {} }, kill: () => { state.kills++; },
    });
  };
  const nodeNet = { createServer: () => ({ once() {}, listen(_port, _host, callback) { callback(); }, address: () => ({ port: 12345 }), close(callback) { callback(); } }) };
  const fakeProcess = Object.assign(new EventEmitter(), { platform, resourcesPath: '/resources', env: { BUSINESSMAP_DATA_DIR: resolve(root, '.tmp/platform-contract'), BUSINESSMAP_HEADLESS: '1' } });
  if (useDefaultData) delete fakeProcess.env.BUSINESSMAP_DATA_DIR;
  const context = vm.createContext({
    require: name => name === 'electron' ? electron : name === 'node:fs' ? fakeFs : name === 'node:child_process' ? { spawn } : name === 'node:net' ? nodeNet : require(name),
    __dirname: resolve(root, 'desktop'), process: fakeProcess,
    URL, Response, AbortSignal, setTimeout,
    fetch: async () => ({ ok: true, json: async () => ({ status: 'ok' }) }),
  });
  new vm.Script(source, { filename: 'desktop/main.cjs' }).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.windows.length, 1, 'one initial window');
  assert.deepEqual(state.errors, [], 'boot succeeds');
  return { app, state };
}

test('macOS keeps the backend after red-button close and reopens from Dock without duplicate protocol registration', async () => {
  const { app, state } = await boot('darwin');
  const first = state.windows[0];
  first.close();
  assert.equal(state.quits, 0, 'closing a Mac window does not quit the app');
  assert.equal(state.kills, 0, 'backend remains available without a window');
  app.emit('activate');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.windows.length, 2, 'Dock activation recreates the window');
  assert.equal(state.spawns.length, 1, 'Dock activation reuses the same backend');
  assert.equal(state.protocols, 1, 'secure protocol registered once across windows');
  assert.deepEqual(state.errors, []);
  state.windows[1].minimized = true;
  app.emit('activate');
  assert.equal(state.windows.length, 2, 'activation of an existing window does not duplicate it');
  assert.equal(state.windows[1].minimized, false);
  assert.equal(state.windows[1].focused, true);
  app.quit();
  assert.equal(state.kills, 1, 'explicit quit stops the backend');
});

test('macOS has native traffic lights and native editing/application/window menus', async () => {
  const { state } = await boot('darwin');
  assert.equal(state.windows[0].options.frame, true);
  assert.equal(state.windows[0].options.titleBarStyle, 'hiddenInset');
  assert.ok(state.menu.some(item => item.role === 'appMenu'));
  assert.ok(state.menu.some(item => item.role === 'editMenu'), 'Edit role supplies Cmd+C/V/X/A/Z');
  assert.ok(state.menu.some(item => item.role === 'windowMenu'));
  assert.ok(state.menu.some(item => item.submenu?.some(child => child.role === 'close')), 'Cmd+W uses normal window close');
});

for (const platform of ['win32', 'linux']) {
  test(`${platform} closes the app and backend on last window close`, async () => {
    const { state } = await boot(platform);
    assert.equal(state.windows[0].options.frame, false, 'custom controls remain enabled');
    state.windows[0].close();
    assert.equal(state.quits, 1);
    assert.equal(state.kills, 1);
    assert.ok(state.spawns[0].executable.endsWith(platform === 'win32' ? 'businessmap-service.exe' : 'businessmap-service'));
  });
}

test('unsaved content can cancel closing on every platform', async () => {
  for (const platform of ['darwin', 'win32', 'linux']) {
    const { state } = await boot(platform);
    let unloadAllowed = false;
    state.windows[0].webContents.emit('will-prevent-unload', { preventDefault: () => { unloadAllowed = true; } });
    assert.equal(unloadAllowed, false, `${platform}: continue editing preserves note`);
    assert.equal(state.kills, 0);
    state.discard = true;
    state.windows[0].webContents.emit('will-prevent-unload', { preventDefault: () => { unloadAllowed = true; } });
    assert.equal(unloadAllowed, true, `${platform}: explicit discard permits close`);
  }
});

test('Sijing keeps existing data, desktop storage and application identity after renaming', async () => {
  for (const platform of ['win32', 'darwin', 'linux']) {
    const { state } = await boot(platform, true);
    const expectedData = resolve(platform === 'win32' ? 'D:\\BusinessMapData' : '/user/app-data/BusinessMap');
    assert.equal(state.appName, 'Sijing');
    assert.equal(state.paths.userData, resolve(expectedData, 'desktop'));
    assert.equal(state.paths.sessionData, resolve(expectedData, 'desktop/session'));
    assert.equal(state.spawns[0].args[state.spawns[0].args.indexOf('--data-dir') + 1], expectedData);
    assert.equal(state.windows[0].url, 'businessmap://desktop/index.html', 'existing theme and onboarding storage origin is preserved');
    assert.equal(state.windows[0].options.title, '思径 Sijing · 业务地图');
    if (platform === 'win32') assert.equal(state.appId, 'io.businessmap.desktop');
  }
});
