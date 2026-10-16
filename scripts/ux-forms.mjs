import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { createServer as createVite } from 'vite';
import { createServer } from 'node:http';
import { spawn, execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const data = resolve(root, '.tmp', `ux-forms-${Date.now()}`);
await mkdir(data, { recursive: true });
const checks = [];
const check = (name, ok, detail) => { checks.push({ name, ok, ...(detail === undefined ? {} : { detail }) }); };
const provider = createServer((_request, response) => setTimeout(() => {
  response.writeHead(503, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ error: { message: 'Local test service unavailable' } }));
}, 600));
await new Promise(resolve => provider.listen(0, '127.0.0.1', resolve));
const providerUrl = `http://127.0.0.1:${provider.address().port}/v1`;
const vite = await createVite({ root, server: { host: '127.0.0.1', port: 0, watch: { ignored: ['**/.tmp/**', '**/.cache/**', '**/release/**', '**/build/**'] } }, cacheDir: resolve(data, 'vite'), logLevel: 'error' });
await vite.listen();
const env = { ...process.env, BUSINESSMAP_DATA_DIR: data, BUSINESSMAP_HEADLESS: '1',
  BUSINESSMAP_DEV_URL: vite.resolvedUrls.local[0], PYTHONUTF8: '1', TEMP: data, TMP: data, TMPDIR: data };
delete env.ELECTRON_RUN_AS_NODE;
let desktop;
let databaseLock;
try {
  desktop = await electron.launch({ executablePath: electronBinary, args: [root], cwd: root, env, chromiumSandbox: true, timeout: 60000 });
  const page = await desktop.firstWindow();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1080, 700));
  const screenshot = async (filename) => {
    const capture = () => desktop.evaluate(async ({BrowserWindow}) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, {stayHidden:true,stayAwake:true})).toPNG().toString('base64'));
    // Wake the hidden compositor, then capture the frame after its CSS updates.
    await capture();
    await page.waitForTimeout(250);
    const png = Buffer.from(await capture(), 'base64');
    await writeFile(resolve(data, filename), png);
    return png;
  };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await dismissOnboarding(page);
  await page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '模型配置', exact: true }).click();
  const addModel = page.getByRole('button', { name: '添加模型', exact: true });
  await addModel.click();
  await page.getByRole('dialog').waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  check('Escape restores focus to model trigger', await addModel.evaluate(element => document.activeElement === element));
  await addModel.click();
  let modal = page.getByRole('dialog');
  await modal.getByRole('button', { name: '保存配置', exact: true }).click();
  check('required model ID blocks empty submission', await modal.getByLabel('模型 ID', { exact: true }).evaluate(element => !element.validity.valid));
  await modal.getByLabel('显示名称', { exact: true }).fill('本地失败连接测试');
  await modal.getByLabel('API 地址', { exact: true }).fill(providerUrl);
  await modal.getByLabel('模型 ID', { exact: true }).fill('ux-test-model');

  databaseLock = spawn(resolve(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
    ['-c', 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=10); db.execute("BEGIN IMMEDIATE"); print("locked",flush=True); sys.stdin.read(); db.rollback(); db.close()', resolve(data, 'workspace.sqlite')],
    { env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  await new Promise((resolve, reject) => { databaseLock.stdout.once('data', resolve); databaseLock.once('error', reject); });
  await modal.getByRole('button', { name: '保存配置', exact: true }).click();
  await modal.getByRole('button', { name: '保存中…', exact: true }).waitFor();
  check('saving model disables editable fields', await modal.getByLabel('模型 ID', { exact: true }).isDisabled());
  check('saving model visibly disables close', await modal.getByRole('button', { name: '关闭对话框' }).isDisabled());
  await page.keyboard.press('Escape');
  check('Escape cannot dismiss in-flight model save', await modal.isVisible());
  databaseLock.stdin.end();
  await new Promise(resolve => databaseLock.once('exit', resolve));
  databaseLock = null;
  await modal.waitFor({ state: 'hidden' });
  const model = page.locator('.provider-card').filter({ hasText: '本地失败连接测试' });
  await model.waitFor();
  await model.getByRole('button', { name: '测试', exact: true }).click();
  check('connection request shows pending state', await model.getByRole('button', { name: /请求中|测试中/ }).isVisible());
  await page.getByRole('alert').filter({ hasText: /503|模型.*失败|请求失败/ }).first().waitFor();
  check('failed connection has visible explanation', true);
  await model.getByRole('button', { name: '编辑 本地失败连接测试', exact: true }).click();
  check('edit form keeps model ID', await page.getByRole('dialog').getByLabel('模型 ID', { exact: true }).inputValue() === 'ux-test-model');
  await page.keyboard.press('Escape');
  check('Escape restores focus to edit trigger', await model.getByRole('button', { name: '编辑 本地失败连接测试', exact: true }).evaluate(element => document.activeElement === element));
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: theme === 'light' ? '浅色主题' : '深色主题', exact: true }).click();
    await model.getByRole('button', { name: '编辑 本地失败连接测试', exact: true }).click();
    await page.waitForTimeout(250);
    await screenshot(`model-form-${theme}.png`);
    check(`model dialog fits small ${theme} window`, await page.getByRole('dialog').evaluate(element => {
      const bounds = element.getBoundingClientRect();
      return bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight && element.scrollWidth <= element.clientWidth;
    }));
    await page.keyboard.press('Escape');
  }

  const source = resolve(data, 'source');
  await mkdir(source);
  const branch = 'feature/' + 'businessdomain'.repeat(9);
  const git = (...args) => execFileSync('git', ['-c', 'user.name=UX Test', '-c', 'user.email=ux@example.invalid', ...args], { cwd: source, windowsHide: true, stdio: 'pipe' });
  git('init', '-b', branch);
  await writeFile(resolve(source, 'orders.py'), 'def total():\n    return 100\n');
  git('add', '.'); git('commit', '-m', 'fixture');
  await page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '仓库', exact: true }).click();
  await page.getByRole('button', { name: '接入仓库', exact: true }).first().click();
  modal = page.getByRole('dialog');
  await modal.getByLabel('仓库地址或本地路径').fill(source);
  await modal.getByLabel('显示名称').fill('长分支业务服务');
  await modal.getByRole('button', { name: '接入仓库', exact: true }).click();
  await modal.waitFor({ state: 'hidden' });
  await page.locator('.repo-card').filter({ hasText: '长分支业务服务' }).getByText(branch, { exact: true }).waitFor({ timeout: 30000 });
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1080, 700));
  await page.waitForTimeout(300);
  const geometry = await page.locator('.repo-card').first().evaluate(element => {
    const card = element.getBoundingClientRect();
    const meta = element.querySelector('.repo-meta > span:first-child').getBoundingClientRect();
    const container = element.closest('.page-scroll');
    return { cardRight: card.right, metaRight: meta.right, width: innerWidth, overflow: container.scrollWidth - container.clientWidth };
  });
  check('long branch remains within 1080px workspace', geometry.overflow <= 1 && geometry.cardRight <= 1081 && geometry.metaRight <= geometry.cardRight, geometry);
  await page.getByRole('button', { name: '接入仓库', exact: true }).first().click();
  modal = page.getByRole('dialog');
  await modal.getByLabel('仓库地址或本地路径').fill('https://127.0.0.1:1/' + 'repository'.repeat(30) + '.git');
  await modal.getByLabel('显示名称').fill('失败仓库');
  await modal.getByRole('button', { name: '接入仓库', exact: true }).click();
  await modal.waitFor({state:'hidden'});
  await page.locator('.repo-card').filter({hasText:'失败仓库'}).locator('.badge').filter({hasText:/error|同步失败/}).waitFor({timeout:30000});
  const taskError = page.locator('.task-row .danger').first();
  await taskError.waitFor();
  check('failed repository exposes task failure details', (await taskError.innerText()).length > 10);
  check('long failed URL does not cause workspace overflow', await page.locator('.page-scroll').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
  for (const theme of ['light', 'dark']) {
    await page.getByRole('button', { name: theme === 'light' ? '浅色主题' : '深色主题', exact: true }).click();
    await page.waitForFunction(theme => document.documentElement.dataset.theme === theme, theme, {polling:100});
    const png = await screenshot(`repositories-${theme}.png`);
    const pixel = await sharp(png).extract({left:5,top:200,width:1,height:1}).removeAlpha().raw().toBuffer();
    check(`${theme} screenshot reflects current palette`, theme === 'dark' ? pixel[0] < 60 : pixel[0] > 220, [...pixel]);
  }
  check('renderer has no errors', errors.length === 0, errors);
} finally {
  databaseLock?.stdin.end();
  await desktop?.close();
  await vite.close();
  await new Promise(resolve => provider.close(resolve));
  await writeFile(resolve(data, 'result.json'), JSON.stringify({data, checks}, null, 2));
  console.log(JSON.stringify({data, checks}, null, 2));
  if (checks.some(check => !check.ok)) process.exitCode = 1;
}
