import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { createServer } from 'vite';
import { execFileSync } from 'node:child_process';
import { copyFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const testDir = resolve(root, '.tmp', `ux-data-${Date.now()}`);
const artifacts = resolve(testDir, 'captures');
await mkdir(artifacts, { recursive: true });
const fixture = resolve(testDir, 'business.sqlite');
execFileSync(resolve(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'), ['-c',
  "import sqlite3,sys\nc=sqlite3.connect(sys.argv[1])\nc.execute('CREATE TABLE orders (id INTEGER, channel TEXT, amount REAL, description TEXT)')\nc.executemany('INSERT INTO orders VALUES (?,?,?,?)', [(i,'fixture-store',i*2.5,'业务详情'*150) for i in range(1,1206)])\nc.commit()\nc.close()", fixture], { windowsHide: true });
const server = await createServer({ root, server: { host: '127.0.0.1', port: 5197, strictPort: true, watch: { ignored: ['**/.tmp/**'] } } });
await server.listen();
const env = { ...process.env, BUSINESSMAP_DATA_DIR: testDir, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DEV_URL: 'http://127.0.0.1:5197', PYTHONUTF8: '1' };
delete env.ELECTRON_RUN_AS_NODE;
const desktop = await electron.launch({ executablePath: electronBinary, args: [root], cwd: root, env, timeout: 90_000 });
const findings = { testDir, mode: process.env.UX_VERIFY ? 'verification' : 'baseline', passed: false, errors: [], checks: {} };
const verify = Boolean(process.env.UX_VERIFY);
try {
  const page = await desktop.firstWindow({ timeout: 90_000 });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => findings.errors.push(error.message));
  await dismissOnboarding(page);
  await page.waitForFunction(() => !!window.businessmap);
  await desktop.evaluate(({ BrowserWindow, ipcMain }) => {
    BrowserWindow.getAllWindows()[0].setSize(1080, 700);
    BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false);
    const original = ipcMain._invokeHandlers.get('businessmap:request');
    globalThis.uxDataControl = { delayQuery: 0, failReportOnce: false };
    ipcMain.removeHandler('businessmap:request');
    ipcMain.handle('businessmap:request', async (event, payload) => {
      // Keep real service execution and database results. Inject transport latency
      // and one invalid receipt request to exercise asynchronous/error UI paths.
      if (payload.path === '/api/reports' && payload.method === 'POST' && globalThis.uxDataControl.failReportOnce) {
        globalThis.uxDataControl.failReportOnce = false;
        return original(event, { ...payload, body: { ...payload.body, result_id: 'missing-ux-receipt' } });
      }
      const value = await original(event, payload);
      if (payload.path === '/api/query' && globalThis.uxDataControl.delayQuery) await new Promise(resolve => setTimeout(resolve, globalThis.uxDataControl.delayQuery));
      return value;
    });
  });
  const capture = async name => {
    await page.waitForTimeout(250);
    // The first capture wakes a hidden compositor and may contain its previous
    // frame. Warm it, then capture the state asserted by the test after painting.
    await desktop.evaluate(async ({ BrowserWindow }) => {
      await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
    });
    await page.waitForTimeout(250);
    const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    await writeFile(resolve(artifacts, `${name}.png`), Buffer.from(png, 'base64'));
  };
  const api = (method, path, body) => page.evaluate(({ method, path, body }) => window.businessmap.request(method, path, body), { method, path, body });
  const navigation = page.getByRole('navigation', { name: '应用导航' });
  await navigation.getByRole('button', { name: '数据库', exact: true }).click();
  const editor = page.getByLabel('SQL 查询编辑器');
  await editor.waitFor();
  await page.getByRole('button', { name: '添加数据源', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('名称', { exact: true }).fill('UX 独立测试数据库');
  await dialog.getByRole('textbox', { name: /数据库文件/ }).fill(resolve(testDir, 'missing.sqlite'));
  await dialog.getByRole('button', { name: '添加数据源', exact: true }).click();
  await dialog.getByRole('alert').waitFor();
  findings.checks.datasourceErrorVisible = await dialog.getByRole('alert').innerText();
  if (verify) {
    assert.ok(!findings.checks.datasourceErrorVisible.includes('Error invoking remote method'), 'datasource error hides the Electron IPC wrapper');
    assert.ok(findings.checks.datasourceErrorVisible.includes('SQLite 文件不存在'), 'datasource error preserves the actual failure reason');
    findings.checks.datasourceErrorHasNoIpcWrapper = true;
  }
  await capture('datasource-error');
  await dialog.getByRole('textbox', { name: /数据库文件/ }).fill(fixture);
  await dialog.getByRole('button', { name: '添加数据源', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  const { datasources } = await api('GET', '/api/datasources');
  const fixtureSource = datasources.find(source => source.name === 'UX 独立测试数据库');
  assert.ok(fixtureSource);
  const select = page.getByLabel('当前数据源');
  await select.selectOption('demo');
  const draftA = 'SELECT channel, COUNT(*) AS total FROM orders GROUP BY channel';
  const draftB = 'SELECT id, channel, amount, description FROM orders ORDER BY id';
  await editor.fill(draftA);
  await select.selectOption(fixtureSource.id);
  await editor.fill(draftB);
  await select.selectOption('demo');
  findings.checks.draftRestoredAfterSourceSwitch = (await editor.inputValue()) === draftA;
  if (verify) assert.ok(findings.checks.draftRestoredAfterSourceSwitch, 'each source retains its SQL draft');
  await editor.fill(draftA);
  await navigation.getByRole('button', { name: '模型配置', exact: true }).click();
  await navigation.getByRole('button', { name: '数据库', exact: true }).click();
  findings.checks.draftRestoredAfterNavigation = (await editor.inputValue()) === draftA;
  if (verify) assert.ok(findings.checks.draftRestoredAfterNavigation, 'navigation retains SQL draft');
  await select.selectOption('demo');
  await editor.fill('SELECT COUNT(*) AS demo_total FROM orders');
  await desktop.evaluate(() => { globalThis.uxDataControl.delayQuery = 1500; });
  await page.getByRole('button', { name: '执行查询', exact: true }).click();
  await select.selectOption(fixtureSource.id);
  await page.waitForTimeout(1800);
  findings.checks.oldResultShownUnderNewSource = await page.locator('.query-results').getByRole('columnheader', { name: 'demo_total', exact: true }).isVisible();
  if (verify) assert.equal(findings.checks.oldResultShownUnderNewSource, false, 'late result must belong to original source');
  await capture('source-race');
  if (verify) {
    await select.selectOption('demo');
    await page.getByRole('columnheader', { name: 'demo_total', exact: true }).waitFor();
    assert.equal(await page.locator('.query-results tbody tr').first().innerText(), '1\t8', 'late query result is retained under original datasource');
    await select.selectOption(fixtureSource.id);
    assert.equal(await editor.inputValue(), draftB);
    findings.checks.originalSourceRetainsCompletedResult = true;
  }
  await desktop.evaluate(() => { globalThis.uxDataControl.delayQuery = 0; });
  await editor.fill(draftB);
  await page.getByRole('button', { name: '执行查询', exact: true }).click();
  await page.getByRole('columnheader', { name: 'description', exact: true }).waitFor();
  assert.equal(await page.locator('.query-results tbody tr').count(), 1000, 'real database result respects service limit');
  findings.checks.truncationVisible = await page.locator('.query-results').innerText().then(text => text.includes('截断') || text.includes('上限'));
  findings.checks.queryHorizontalOverflow = await page.locator('.query-main').evaluate(element => element.scrollWidth - element.clientWidth);
  if (verify) {
    assert.equal(await page.getByText('SQL 草稿已修改', { exact: false }).count(), 0, 'normalization is not mistaken for an edited draft');
    await editor.fill(draftB + '\n-- 尚未执行的草稿补充');
    await page.getByText('SQL 草稿已修改', { exact: false }).waitFor();
    await editor.fill(draftB);
    findings.checks.editedDraftDoesNotRelabelPriorResult = true;
  }
  await page.getByRole('button', { name: '浅色主题', exact: true }).click();
  await page.locator('.query-results').scrollIntoViewIfNeeded();
  await capture('long-result-light');
  await page.getByRole('button', { name: '深色主题', exact: true }).click();
  await capture('long-result-dark');
  await page.getByRole('button', { name: '生成报表', exact: true }).click();
  await dialog.getByLabel('报表标题').fill('长数据报表与失败重试');
  await desktop.evaluate(() => { globalThis.uxDataControl.failReportOnce = true; });
  await dialog.getByRole('button', { name: '生成报表', exact: true }).click();
  await page.waitForTimeout(500);
  findings.checks.reportErrorVisibleInDialog = await dialog.getByRole('alert').count() > 0;
  findings.checks.reportTitleRetainedAfterFailure = await dialog.getByLabel('报表标题').inputValue();
  if (verify) {
    assert.ok(findings.checks.reportErrorVisibleInDialog, 'report failure appears inside the modal');
    findings.checks.reportErrorVisible = await dialog.getByRole('alert').innerText();
    assert.ok(!findings.checks.reportErrorVisible.includes('Error invoking remote method'), 'report error hides the Electron IPC wrapper');
    assert.ok(findings.checks.reportErrorVisible.includes('查询结果不存在'), 'report error preserves the actual failure reason');
    findings.checks.reportErrorHasNoIpcWrapper = true;
  }
  await capture('report-save-error');
  await dialog.getByRole('button', { name: /生成报表|重试保存/, exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: /长数据报表与失败重试/ }).click();
  await page.getByRole('heading', { name: '长数据报表与失败重试', exact: true }).waitFor();
  findings.checks.reportHorizontalOverflow = await page.locator('.reports-content').evaluate(element => element.scrollWidth - element.clientWidth);
  await capture('report-dark');
  await page.getByRole('button', { name: '浅色主题', exact: true }).click();
  await capture('report-light');
  if (verify) {
    await page.getByRole('button', { name: '数据源与查询', exact: true }).click();
    assert.equal(await editor.inputValue(), draftB, 'return from report retains SQL');
    assert.equal(await page.locator('.query-results tbody tr').count(), 1000, 'return from report retains result');
    findings.checks.reportReturnRetainsDraftAndResult = true;
  }
  assert.deepEqual(findings.errors, []);
  findings.passed = true;
} finally {
  await writeFile(resolve(testDir, 'findings.json'), JSON.stringify(findings, null, 2));
  if (verify && findings.passed) {
    await mkdir(resolve(root, 'artifacts'), { recursive: true });
    await copyFile(resolve(testDir, 'findings.json'), resolve(root, 'artifacts', 'ux-data-checks.json'));
  }
  console.log(JSON.stringify(findings, null, 2));
  await desktop.close();
  await server.close();
}
