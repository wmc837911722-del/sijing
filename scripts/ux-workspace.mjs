import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const observe = process.argv.includes('--observe');
const testDir = resolve(root, '.tmp', `ux-workspace-${Date.now()}`);
const artifacts = resolve(root, 'artifacts');
await mkdir(artifacts, { recursive: true });
// Use the production UI from npm run build, without a shared Vite/HMR cache.
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, PYTHONUTF8: '1' };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true });
const results = { mode: observe ? 'baseline' : 'verification', packaged: !!packaged, version: await desktop.evaluate(({ app }) => app.getVersion()), tested_at: new Date().toISOString(), checks: {}, renderer_errors: [] };
try {
  const page = await desktop.firstWindow();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  page.on('pageerror', error => results.renderer_errors.push(error.message));
  await dismissOnboarding(page);
  await page.locator('#bm-chat-input').waitFor();
  const nav = page.getByRole('navigation', { name: '应用导航' });
  const api = (method, path, body) => page.evaluate(({ method, path, body }) => window.businessmap.request(method, path, body), { method, path, body });
  const record = (key, passed) => { results.checks[key] = passed; if (!observe) assert.ok(passed, key); };
  const capture = async name => {
    await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
    await page.waitForTimeout(250);
    const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    await writeFile(resolve(artifacts, `ux-workspace-${name}.png`), Buffer.from(png, 'base64'));
  };

  await nav.getByRole('button', { name: '数据库', exact: true }).click();
  await page.getByLabel('SQL 查询编辑器').fill('SELECT 123 AS navigation_proof');
  await page.getByRole('button', { name: '执行查询', exact: true }).click();
  await page.getByRole('cell', { name: '123', exact: true }).waitFor();
  await nav.getByRole('button', { name: 'AI 问答', exact: true }).click();
  await nav.getByRole('button', { name: '数据库', exact: true }).click();
  record('SQL draft survives navigation', await page.getByLabel('SQL 查询编辑器').inputValue() === 'SELECT 123 AS navigation_proof');
  record('query result survives navigation', await page.getByRole('cell', { name: '123', exact: true }).isVisible());

  await nav.getByRole('button', { name: '业务地图', exact: true }).click();
  await page.locator('.note-article').waitFor();
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1080, 700));
  await page.waitForTimeout(200);
  await page.getByRole('button', { name: '切换右侧面板', exact: true }).click();
  await page.getByRole('button', { name: '切换右侧面板', exact: true }).click();
  record('context panel can open at minimum window size', await page.locator('.right-panel').isVisible());
  await capture('minimum-map');
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1500, 940));

  const path = await page.locator('.note-article').getAttribute('data-note-path');
  await page.getByRole('button', { name: '编辑 Markdown', exact: true }).click();
  const editor = page.locator('.cm-content');
  const firstDraft = '# 交互验收\n\n第一段已提交文字。\n\n[外部资料](https://example.org/businessmap-ux)\n';
  await editor.fill(firstDraft);
  await desktop.evaluate(() => {
    const original = globalThis.fetch;
    globalThis.__uxFetch = original;
    globalThis.fetch = async (url, init) => {
      const response = await original(url, init);
      if (String(url).includes('/api/note') && init?.method === 'PUT') await new Promise(resolve => setTimeout(resolve, 1100));
      return response;
    };
  });
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('button', { name: '保存中', exact: true }).waitFor();
  await editor.fill(`${firstDraft}\n请求期间继续输入，不应消失。`);
  await page.waitForTimeout(1500);
  record('typing during save is preserved', (await editor.textContent()).includes('请求期间继续输入'));
  const saved = await api('GET', `/api/note?path=${encodeURIComponent(path)}`);
  record('in-flight save writes exactly its submitted snapshot', saved.content === firstDraft);
  await desktop.evaluate(({ shell }) => {
    globalThis.fetch = globalThis.__uxFetch;
    globalThis.__uxOpenedUrls = [];
    shell.openExternal = async url => { globalThis.__uxOpenedUrls.push(url); };
  });
  await page.getByRole('button', { name: '阅读模式', exact: true }).click();
  await page.getByRole('link', { name: '外部资料', exact: true }).click();
  await page.waitForTimeout(100);
  record('external documentation link opens browser', await desktop.evaluate(() => globalThis.__uxOpenedUrls.includes('https://example.org/businessmap-ux')));
  if (!observe) record('external launcher rejects local executable links', await page.evaluate(async () => {
    try { await window.businessmap.openExternal('file:///C:/Windows/System32/cmd.exe'); return false; } catch { return true; }
  }));
  await capture('reading');
  if (!observe && await page.getByRole('button', { name: '保存', exact: true }).isEnabled()) {
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByRole('button', { name: '已保存', exact: true }).waitFor();
  }
  // Clear only this disposable test note's dirty state before closing.
  page.on('dialog', dialog => dialog.accept());
  await nav.getByRole('button', { name: '模型配置', exact: true }).click();
  assert.deepEqual(results.renderer_errors, []);
} finally {
  await writeFile(resolve(artifacts, `ux-workspace-${observe ? 'baseline' : packaged ? 'packaged' : 'verified'}.json`), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
  await desktop.close();
}
