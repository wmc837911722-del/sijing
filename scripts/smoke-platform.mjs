import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dismissOnboarding } from './dismiss-onboarding.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = resolve(root, 'artifacts/platform');
const dataDir = resolve(root, '.tmp', `platform-${process.platform}-${Date.now()}`);
await mkdir(artifacts, { recursive: true });
const env = { ...process.env, BUSINESSMAP_DATA_DIR: dataDir, BUSINESSMAP_HEADLESS: '1', TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp'), TMPDIR: resolve(root, '.tmp') };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true, timeout: 90_000 });
const desktopProcess = desktop.process();
const result = { platform: process.platform, packaged: !!packaged, version: await desktop.evaluate(({ app }) => app.getVersion()), tested_at: new Date().toISOString(), checks: [], renderer_errors: [] };
const check = (name, passed) => { assert.ok(passed, name); result.checks.push(name); console.log(`PASS ${name}`); };
const alive = pid => { try { process.kill(pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; throw error; } };
const waitFor = async (predicate, description) => {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${description}`);
};
let exited = false;
try {
  let page = await desktop.firstWindow({ timeout: 90_000 });
  page.on('pageerror', error => result.renderer_errors.push(error.message));
  // Electron handles beforeunload in will-prevent-unload. Prevent Playwright
  // from racing its native confirmation with an automatic CDP dismissal.
  page.on('dialog', dialog => { if (dialog.type() !== 'beforeunload') void dialog.dismiss(); });
  await dismissOnboarding(page);
  check('preload reports the actual host platform', await page.evaluate(() => window.businessmap.platform) === process.platform);
  const backendLog = await readFile(resolve(dataDir, 'logs/backend.log'), 'utf8');
  const backendPid = Number(backendLog.match(/Started server process \[(\d+)\]/)?.[1]);
  check('local backend is running', backendPid > 0 && alive(backendPid));
  const before = await page.evaluate(() => window.businessmap.request('GET', '/api/workspace'));
  const input = page.locator('#bm-chat-input');
  await input.fill('native shortcut baseline');
  await input.press('ControlOrMeta+A');
  await input.press('Backspace');
  check('platform select-all shortcut edits the whole field', await input.inputValue() === '');
  await input.press('ControlOrMeta+Z');
  check('platform undo shortcut restores the edit', await input.inputValue() === 'native shortcut baseline');
  await input.fill('');

  if (process.platform === 'darwin') {
    const roles = await desktop.evaluate(({ Menu }) => {
      const collect = items => items.flatMap(item => [item.role?.toLowerCase(), ...(item.submenu ? collect(item.submenu.items) : [])]).filter(Boolean);
      return collect(Menu.getApplicationMenu().items);
    });
    check('native Mac menus provide edit, close and quit shortcuts', ['copy', 'paste', 'cut', 'undo', 'selectall', 'close', 'quit'].every(role => roles.includes(role)));
    check('Mac titlebar leaves room for native traffic lights', await page.locator('.mac-titlebar').count() === 1 && await page.locator('.window-buttons').count() === 0);
    await desktop.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].close(); });
    await waitFor(async () => (await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)) === 0, 'Mac window closes');
    check('Mac keeps the application and backend after the last window closes', desktopProcess.exitCode === null && alive(backendPid));
    const reopened = desktop.waitForEvent('window');
    await desktop.evaluate(({ app }) => { app.emit('activate'); });
    page = await reopened;
    page.on('pageerror', error => result.renderer_errors.push(error.message));
    page.on('dialog', dialog => { if (dialog.type() !== 'beforeunload') void dialog.dismiss(); });
    await page.locator('#bm-chat-input').waitFor();
    const after = await page.evaluate(() => window.businessmap.request('GET', '/api/workspace'));
    check('Dock activation restores the same usable workspace', before.vault_path === after.vault_path);
    check('Dock activation reuses the existing backend process', alive(backendPid) && (await readFile(resolve(dataDir, 'logs/backend.log'), 'utf8')).match(/Started server process/g)?.length === 1);
  } else {
    check('Windows/Linux expose custom minimize, maximize and close controls', await page.locator('.window-buttons button').count() === 3);
  }

  await page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '业务地图', exact: true }).click();
  await page.getByRole('button', { name: '编辑 Markdown', exact: true }).click();
  const editor = page.locator('.cm-content');
  await editor.press('ControlOrMeta+End');
  await editor.press('Enter');
  await editor.pressSequentially('platform-close-guard');
  await page.getByText('有未保存的更改', { exact: false }).waitFor();
  await desktop.evaluate(({ BrowserWindow, dialog }) => {
    globalThis.__platformClosePrompts = 0;
    globalThis.__originalCloseDialog = dialog.showMessageBoxSync;
    dialog.showMessageBoxSync = () => { globalThis.__platformClosePrompts++; return 0; };
    BrowserWindow.getAllWindows()[0].close();
  });
  await waitFor(async () => (await desktop.evaluate(() => globalThis.__platformClosePrompts)) === 1, 'unsaved close prompts');
  check('native close can be cancelled without losing the unsaved note', await editor.innerText().then(text => text.includes('platform-close-guard')) && alive(backendPid));
  await editor.press('ControlOrMeta+s');
  await page.getByRole('button', { name: '已保存', exact: true }).waitFor();
  check('platform save shortcut saves the edited Markdown', true);
  await desktop.evaluate(({ dialog }) => { dialog.showMessageBoxSync = globalThis.__originalCloseDialog; });
  check('no renderer exceptions', result.renderer_errors.length === 0);

  // Use the native lifecycle, not Playwright's forced application cleanup.
  await desktop.evaluate(({ app, BrowserWindow }) => {
    setTimeout(() => process.platform === 'darwin' ? app.quit() : BrowserWindow.getAllWindows()[0].close(), 50);
  });
  await waitFor(() => desktopProcess.exitCode !== null || desktopProcess.signalCode !== null, 'application exits');
  exited = true;
  await waitFor(() => !alive(backendPid), 'backend stops on application quit');
  check(process.platform === 'darwin' ? 'explicit Mac quit stops the local backend' : 'closing the last window exits and stops the local backend', true);
  await writeFile(resolve(artifacts, `${packaged ? 'packaged' : 'source'}-${process.platform}.json`), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
} finally {
  if (!exited) {
    // This process owns only the disposable test vault. Failed dirty-note
    // checks must not leave a hidden confirmation or backend running.
    await desktop.evaluate(({ dialog }) => { dialog.showMessageBoxSync = () => 1; }).catch(() => {});
    await desktop.close().catch(() => {});
  }
}
