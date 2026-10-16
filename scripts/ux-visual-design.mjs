import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dismissOnboarding } from './dismiss-onboarding.mjs';

// Consistent visual specimens, using only isolated local fixtures and real UI.
// Run BUSINESSMAP_VISUAL_LABEL=baseline/final and optionally a packaged executable.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const label = process.env.BUSINESSMAP_VISUAL_LABEL || 'source';
assert.match(label, /^[a-z0-9-]+$/i);
const testDir = resolve(root, '.tmp', `visual-design-${label}-${Date.now()}`);
const artifacts = resolve(root, 'artifacts', 'ux-visual-design', label);
const source = resolve(testDir, 'commerce-core');
await mkdir(source, { recursive: true });
await mkdir(artifacts, { recursive: true });
const git = (...args) => execFileSync('git', args, { cwd: source, windowsHide: true, stdio: 'pipe' });
git('init', '-b', 'main');
git('config', 'user.name', 'BusinessMap Visual Test');
git('config', 'user.email', 'visual@example.invalid');
await writeFile(resolve(source, 'orders.py'), 'def can_refund(order):\n    return order.status == "paid" and not order.shipped\n');
git('add', '.'); git('commit', '-m', 'Define order refund eligibility');
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: resolve(testDir, 'app'), PYTHONUTF8: '1', PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring', TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp') };
delete env.ELECTRON_RUN_AS_NODE; delete env.BUSINESSMAP_DEV_URL;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true, timeout: 90_000 });
const result = { label, packaged: !!packaged, version: await desktop.evaluate(({ app }) => app.getVersion()), tested_at: new Date().toISOString(), screenshots: [], checks: [], renderer_errors: [] };
const check = (name, value) => { assert.ok(value, name); result.checks.push(name); console.log(`PASS ${name}`); };
let page;
try {
  page = await desktop.firstWindow(); page.setDefaultTimeout(20_000);
  page.on('pageerror', error => result.renderer_errors.push(error.message));
  await dismissOnboarding(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const api = (method, path, body) => page.evaluate(({ method, path, body }) => window.businessmap.request(method, path, body), { method, path, body });
  const nav = page.getByRole('navigation', { name: '应用导航' });
  const navigate = name => nav.getByRole('button', { name, exact: true }).click();
  const resize = async (width, height) => { await desktop.evaluate(({ BrowserWindow }, { width, height }) => BrowserWindow.getAllWindows()[0].setSize(width, height), { width, height }); await page.waitForTimeout(180); };
  const theme = async value => { const target = value === 'light' ? '浅色主题' : '深色主题'; await page.getByRole('button', { name: target, exact: true }).click(); };
  const capture = async name => {
    await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
    await page.waitForTimeout(300);
    const geometry = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, overflow: document.documentElement.scrollWidth - innerWidth, theme: document.documentElement.dataset.theme }));
    check(`${name}: viewport has no horizontal overflow`, geometry.overflow <= 1);
    const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    await writeFile(resolve(artifacts, `${name}.png`), Buffer.from(png, 'base64'));
    result.screenshots.push({ file: `${name}.png`, ...geometry });
  };

  await resize(1440, 940);
  await theme('light');
  await navigate('AI 问答');
  await capture('light-chat-empty');
  await page.locator('#bm-chat-input').fill('订单退款需要满足哪些条件？');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.bm-chat-message').length === 2 && !document.querySelector('.bm-chat-waiting'));
  check('local support produces cited answer without external model', (await api('GET', '/api/chats')).chats.length === 1);

  const repo = await api('POST', '/api/repositories', { url: source, name: 'Commerce · 订单服务', connection: { auth_type: 'none' } });
  const first = await api('POST', '/api/providers', { name: '企业模型服务', kind: 'openai-compatible', base_url: 'http://127.0.0.1:9/v1', model: 'business-reasoning' });
  await api('POST', '/api/providers', { name: '本地推理', kind: 'openai-compatible', base_url: 'http://127.0.0.1:10/v1', model: 'local-assistant' });
  await api('POST', `/api/repositories/${repo.id}/model-policy`, { provider_id: first.id });
  await navigate('仓库');
  await page.getByRole('article', { name: 'Commerce · 订单服务', exact: true }).getByText('已就绪', { exact: true }).waitFor({ timeout: 45_000 });
  await page.getByRole('article', { name: 'Commerce · 订单服务', exact: true }).getByText('企业模型服务 · business-reasoning', { exact: true }).waitFor();

  await navigate('数据库');
  const editor = page.getByLabel('SQL 查询编辑器');
  await editor.fill('SELECT\n  channel AS 渠道,\n  COUNT(*) AS 订单数量,\n  SUM(amount) AS 交易金额\nFROM orders\nGROUP BY channel\nORDER BY 交易金额 DESC;');
  await page.getByRole('button', { name: '执行查询', exact: true }).click();
  await page.getByRole('columnheader', { name: '交易金额', exact: true }).waitFor();
  check('actual read-only query renders result table', await page.locator('.query-results tbody tr').count() > 0);
  await page.getByRole('button', { name: '生成报表', exact: true }).click();
  await page.getByLabel('报表标题').fill('渠道经营概览');
  await page.getByRole('dialog').getByRole('button', { name: '生成报表', exact: true }).click();
  await page.getByRole('button', { name: /渠道经营概览/ }).click();
  await page.getByRole('heading', { name: '渠道经营概览', exact: true }).waitFor();
  check('report created from real query snapshot', (await api('GET', '/api/reports')).reports.length > 0);

  for (const value of ['light', 'dark']) {
    await theme(value);
    await navigate('模型配置'); await page.getByText('business-reasoning', { exact: true }).waitFor(); await capture(`${value}-models`);
    await navigate('仓库'); await capture(`${value}-repositories`);
    await navigate('数据库');
    if (await page.getByRole('button', { name: '数据源与查询', exact: true }).count()) await page.getByRole('button', { name: '数据源与查询', exact: true }).click();
    await editor.waitFor(); await capture(`${value}-database`);
    await page.getByRole('button', { name: '报表', exact: true }).click();
    await page.getByRole('button', { name: /渠道经营概览/ }).click();
    await page.getByLabel('图表数值指标').waitFor(); await capture(`${value}-report`);
    await navigate('AI 问答'); await capture(`${value}-chat-answer`);
    await navigate('业务地图'); await page.getByRole('button', { name: '文档', exact: true }).click(); await capture(`${value}-knowledge`);
    await page.getByRole('button', { name: '关系图谱', exact: true }).click(); await page.waitForTimeout(1200); await capture(`${value}-graph`);
  }

  await theme('light'); await navigate('模型配置');
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByLabel('模型 ID', { exact: true }).fill('your-model-id');
  await dialog.getByLabel('模型 ID', { exact: true }).focus();
  check('model dialog retains visible keyboard focus', await dialog.getByLabel('模型 ID', { exact: true }).evaluate(element => document.activeElement === element));
  await capture('light-model-dialog');
  await page.keyboard.press('Escape');
  check('model dialog restores trigger focus', await page.locator('[data-tour="model-add"]').evaluate(element => document.activeElement === element));

  await resize(1080, 700); await theme('dark'); await navigate('仓库');
  await page.locator('[data-tour="repository-add"]').click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('仓库地址或本地路径', { exact: true }).fill('https://git.company.example/platform/commerce.git');
  await dialog.getByLabel('认证方式', { exact: true }).selectOption('https');
  await dialog.getByLabel('Git 用户名', { exact: true }).fill('developer');
  await dialog.locator('summary').click();
  await dialog.getByLabel('公司 CA 证书文件', { exact: true }).scrollIntoViewIfNeeded();
  check('small repository dialog primary action visible', await dialog.getByRole('button', { name: '接入仓库', exact: true }).evaluate(element => { const r = element.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }));
  await capture('small-dark-repository-dialog'); await page.keyboard.press('Escape');

  await theme('light'); await navigate('数据库');
  await page.getByRole('button', { name: '数据源与查询', exact: true }).click();
  await page.getByRole('button', { name: '添加数据源', exact: true }).click();
  dialog = page.getByRole('dialog');
  await dialog.getByLabel('数据库类型', { exact: false }).selectOption('postgresql');
  await dialog.getByLabel('名称', { exact: true }).fill('生产业务 · 只读副本');
  await dialog.getByLabel('只读用户名', { exact: true }).fill('business_reader');
  await dialog.getByRole('button', { name: '添加数据源', exact: true }).scrollIntoViewIfNeeded();
  check('small database dialog primary action reachable', await dialog.getByRole('button', { name: '添加数据源', exact: true }).evaluate(element => { const r = element.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }));
  check('small database dialog title and close button stay visible after scroll', await dialog.evaluate(element => {
    const frame = element.getBoundingClientRect();
    return ['.modal-surface > header h2', '.modal-surface > header button'].every(selector => {
      const box = element.querySelector(selector)?.getBoundingClientRect();
      return box && box.top >= frame.top && box.bottom <= frame.bottom;
    });
  }));
  await capture('small-light-database-dialog'); await page.keyboard.press('Escape');
  for (const value of ['light', 'dark']) {
    await theme(value);
    await navigate('模型配置'); await capture(`small-${value}-models`);
    await navigate('仓库'); await capture(`small-${value}-repositories`);
    await navigate('数据库'); await page.getByRole('button', { name: '数据源与查询', exact: true }).click(); await capture(`small-${value}-database`);
  }
  await theme('light'); await navigate('AI 问答'); await capture('small-light-chat');
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.bm-chat-message').length === 0);
  const welcome = page.locator('.bm-chat-welcome');
  await welcome.waitFor();
  const welcomeGeometry = await page.evaluate(() => {
    const container = document.querySelector('.bm-chat-welcome');
    const subtitle = container?.querySelector(':scope > p');
    const brand = container?.querySelector('.bm-chat-welcome-brand');
    const composer = document.querySelector('.bm-chat-composer');
    const conversation = document.querySelector('.bm-chat-conversation');
    if (!subtitle || !brand || !composer || !conversation) return null;
    const text = subtitle.getBoundingClientRect(), icon = brand.getBoundingClientRect(), input = composer.getBoundingClientRect(), area = conversation.getBoundingClientRect();
    return { subtitleTop: text.top, subtitleBottom: text.bottom, brandTop: icon.top, brandBottom: icon.bottom, composerTop: input.top, conversationTop: area.top, conversationBottom: area.bottom, viewportHeight: innerHeight };
  });
  check('small welcome subtitle is visible above composer', welcomeGeometry && welcomeGeometry.subtitleTop >= welcomeGeometry.conversationTop && welcomeGeometry.subtitleBottom <= Math.min(welcomeGeometry.conversationBottom, welcomeGeometry.composerTop));
  check('new small conversation starts with complete welcome brand visible', welcomeGeometry && welcomeGeometry.brandTop >= welcomeGeometry.conversationTop && welcomeGeometry.brandBottom <= welcomeGeometry.conversationBottom);
  check('small welcome send control is inside viewport', await page.getByRole('button', { name: '发送问题', exact: true }).evaluate(element => { const r = element.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth; }));
  await capture('small-light-chat-empty');
  await theme('dark'); await navigate('业务地图'); await page.getByRole('button', { name: '文档', exact: true }).click(); await capture('small-dark-knowledge');
  const inspectorToggle = page.getByRole('button', { name: '切换右侧面板', exact: true });
  check('small knowledge inspector defaults to collapsed', await inspectorToggle.getAttribute('aria-pressed') === 'false' && await page.locator('.right-panel').count() === 0);
  await resize(1440, 940);
  check('large knowledge inspector defaults to expanded', await inspectorToggle.getAttribute('aria-pressed') === 'true' && await page.locator('.right-panel').isVisible());
  await resize(1080, 700);
  check('responsive inspector collapses again before explicit choice', await inspectorToggle.getAttribute('aria-pressed') === 'false');
  await inspectorToggle.click();
  check('small inspector can be explicitly reopened', await inspectorToggle.getAttribute('aria-pressed') === 'true' && await page.locator('.right-panel').isVisible());
  await resize(1440, 940); await resize(1080, 700);
  check('explicit open inspector survives window resize', await inspectorToggle.getAttribute('aria-pressed') === 'true' && await page.locator('.right-panel').isVisible());
  await inspectorToggle.click();
  check('explicit close inspector updates toggle state', await inspectorToggle.getAttribute('aria-pressed') === 'false' && await page.locator('.right-panel').count() === 0);
  await resize(1440, 940);
  check('explicit closed inspector stays closed on a large window', await inspectorToggle.getAttribute('aria-pressed') === 'false' && await page.locator('.right-panel').count() === 0);
  check('fixture never called a model provider', (await api('GET', '/api/settings')).usage.requests === 0);
  check('no renderer exceptions', result.renderer_errors.length === 0);
} catch (error) { result.failure = String(error); throw error; }
finally {
  await writeFile(resolve(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await desktop.close();
}
