import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Exercise the real production renderer and local backend in a disposable Vault.
// No real model credentials, repositories or external databases are required.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const testDir = resolve(root, '.tmp', `onboarding-${Date.now()}`);
const artifacts = resolve(root, 'artifacts', 'ux-onboarding');
await mkdir(artifacts, { recursive: true });
await mkdir(resolve(root, '.tmp'), { recursive: true });
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, PYTHONUTF8: '1', TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp') };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true, timeout: 90_000 });
const result = { version: await desktop.evaluate(({ app }) => app.getVersion()), packaged: !!packaged, tested_at: new Date().toISOString(), checks: {}, steps: [], screenshots: [], renderer_errors: [], capture_method: 'electron-native' };
const prefix = packaged ? 'packaged' : 'source';
const key = 'businessmap.onboarding.v1';
const steps = ['intro', 'knowledge', 'graph', 'models', 'repository', 'database', 'chat', 'mcp'];
const anchors = { intro: '.navigation-items', knowledge: '.file-tree, .map-main', graph: '.graph-view:not(.local) .graph-canvas', models: '[data-tour="model-add"]', repository: '[data-tour="repository-add"]', database: '[data-tour="database-source"]', chat: '.bm-chat-composer', mcp: '[data-tour="mcp-copy"]' };
let page;
const check = (name, value) => { result.checks[name] = Boolean(value); assert.ok(value, name); };
const capture = async name => {
  await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
  await page.waitForTimeout(250);
  const png = await desktop.evaluate(async ({ BrowserWindow }) => {
    const image = await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
    if (image.isEmpty()) throw new Error('Native desktop capture is empty');
    return image.toPNG().toString('base64');
  });
  const filename = `${prefix}-${name}.png`;
  await writeFile(resolve(artifacts, filename), Buffer.from(png, 'base64'));
  result.screenshots.push(filename);
};
try {
  // Observe request metadata only. Forward every request unchanged, including all
  // authentication and permission checks in the actual desktop/backend bridge.
  await desktop.evaluate(() => {
    const original = globalThis.fetch;
    globalThis.__onboardingRequests = [];
    globalThis.fetch = (url, init) => {
      globalThis.__onboardingRequests.push({ method: init?.method || 'GET', path: new URL(String(url)).pathname });
      return original(url, init);
    };
  });
  page = await desktop.firstWindow({ timeout: 90_000 });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.renderer_errors.push(error.message));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const tour = page.getByRole('dialog', { name: '使用引导', exact: true });
  const launch = page.getByRole('button', { name: '使用引导', exact: true });
  const nav = page.getByRole('navigation', { name: '应用导航' });
  const api = path => page.evaluate(path => window.businessmap.request('GET', path), path);
  const snapshot = async () => {
    const [workspace, settings, datasources, tasks, chats, reports, notes] = await Promise.all([
      api('/api/workspace'), api('/api/settings'), api('/api/datasources'), api('/api/tasks'), api('/api/chats'), api('/api/reports'), api('/api/notes'),
    ]);
    const documents = await Promise.all(notes.notes.map(note => api(`/api/note?path=${encodeURIComponent(note.path)}`)));
    return { repositories: workspace.repositories, providers: settings.providers, usage: settings.usage, datasources, tasks, chats, reports, notes: documents.map(({ path, content, revision }) => ({ path, content, revision })).sort((a, b) => a.path.localeCompare(b.path)) };
  };
  const expectStep = async (step, record = false) => {
    await tour.waitFor({ state: 'visible' });
    await page.locator(`dialog[aria-label="使用引导"][data-tour-step="${step}"], dialog[aria-label="使用引导"] [data-tour-step="${step}"]`).waitFor();
    check(`step ${step} is visible`, await tour.isVisible());
    if (record && !result.steps.includes(step)) { result.steps.push(step); console.log(`Onboarding: ${step}`); }
    {
      const spotlight = page.locator('rect[data-tour-spotlight]');
      await spotlight.waitFor({ state: 'visible' });
      await page.waitForFunction(selector => {
        const target = [...document.querySelectorAll(selector)].find(element => { const r = element.getBoundingClientRect(); return r.width && r.height; });
        const spot = document.querySelector('rect[data-tour-spotlight]');
        if (!target || !spot) return false;
        const r = target.getBoundingClientRect(), s = spot.getBoundingClientRect();
        const x = Math.max(8, r.left - 6), y = Math.max(48, r.top - 6);
        return Math.abs(s.left - x) < 2 && Math.abs(s.top - y) < 2
          && Math.abs(s.width - Math.max(0, Math.min(innerWidth - 8, r.right + 6) - x)) < 2
          && Math.abs(s.height - Math.max(0, Math.min(innerHeight - 32, r.bottom + 6) - y)) < 2;
      }, anchors[step]);
      check(`step ${step} highlights the intended real control`, await spotlight.evaluate(element => {
        const rect = element.getBoundingClientRect();
        return rect.width > 5 && rect.height > 5 && rect.right > 0 && rect.bottom > 0 && rect.left < innerWidth && rect.top < innerHeight;
      }));
    }
  };
  const next = async step => { await tour.getByRole('button', { name: /^(下一步|开始引导)$/ }).click(); await expectStep(step, true); };
  const storage = () => page.evaluate(key => JSON.parse(localStorage.getItem(key)), key);
  const closed = () => tour.waitFor({ state: 'hidden' });
  const fit = async name => {
    const geometry = await tour.evaluate(element => ({
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      buttons: [...element.querySelectorAll('button')].filter(button => button.getClientRects().length).map(button => { const rect = button.getBoundingClientRect(); return { text: button.getAttribute('aria-label') || button.textContent, fits: rect.left >= -1 && rect.top >= -1 && rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1 }; }),
    }));
    result[`${name}_geometry`] = geometry;
    result[`${name}_target`] = await page.evaluate(anchors => {
      const step = document.querySelector('dialog[aria-label="使用引导"]')?.getAttribute('data-tour-step');
      const target = [...document.querySelectorAll(anchors[step])].find(element => { const r = element.getBoundingClientRect(); return r.width && r.height; });
      const scroller = target?.closest('.page-scroll');
      return { step, target_rect: target?.getBoundingClientRect().toJSON(), spotlight_rect: document.querySelector('rect[data-tour-spotlight]')?.getBoundingClientRect().toJSON(), scroll: scroller && { top: scroller.scrollTop, client_height: scroller.clientHeight, scroll_height: scroller.scrollHeight } };
    }, anchors);
    check(`${name} has no clipped tutorial controls or horizontal overflow`, !geometry.horizontalOverflow && geometry.buttons.length > 0 && geometry.buttons.every(button => button.fits));
  };

  await expectStep('intro', true);
  check('first launch automatically opens tutorial after workspace is ready', !!await api('/api/workspace'));
  const before = await snapshot();
  await fit('light');
  await capture('light');
  let trapped = true;
  const count = await tour.getByRole('button').count();
  for (let i = 0; i < count + 3; i++) {
    await page.keyboard.press('Tab');
    trapped &&= await tour.evaluate(element => element.contains(document.activeElement));
  }
  await page.keyboard.press('Shift+Tab');
  trapped &&= await tour.evaluate(element => element.contains(document.activeElement));
  check('Tab and Shift+Tab stay inside the native tutorial dialog', trapped);

  await next('knowledge');
  await tour.getByRole('button', { name: '上一步', exact: true }).click();
  await expectStep('intro');
  check('previous step works', true);
  await next('knowledge');
  await next('graph');
  await tour.getByRole('button', { name: '关闭引导', exact: true }).click();
  await closed();
  assert.deepEqual(await storage(), { status: 'paused', step: 2 });
  check('close stores the current step with minimal local state', true);
  await page.reload();
  await page.locator('#bm-chat-input').waitFor();
  await page.waitForTimeout(350);
  check('paused tutorial does not reopen automatically after reload', !await tour.isVisible());
  await launch.click();
  await expectStep('graph');
  await next('models');
  await tour.getByRole('button', { name: '去操作', exact: true }).click();
  await closed();
  check('go operate pauses on the intended real page', await page.getByRole('heading', { name: '模型配置', exact: true }).isVisible());
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  const model = page.getByRole('dialog', { name: '添加模型服务', exact: true });
  await model.getByLabel('显示名称', { exact: true }).fill('引导测试，取消不保存');
  await model.getByLabel('模型 ID', { exact: true }).fill('tutorial-test-no-request');
  check('real model form is editable after tutorial pauses', await model.getByLabel('模型 ID', { exact: true }).inputValue() === 'tutorial-test-no-request');
  await model.getByRole('button', { name: '取消', exact: true }).click();
  await launch.click();
  await expectStep('models');
  await next('repository');
  await next('database');
  await next('chat');
  await next('mcp');
  await tour.getByRole('button', { name: '上一步', exact: true }).click();
  await expectStep('chat');
  await next('mcp');
  await tour.getByRole('button', { name: '暂时跳过', exact: true }).click();
  await closed();
  await page.getByRole('button', { name: '深色主题', exact: true }).click();
  await launch.click();
  await expectStep('mcp');
  await fit('dark');
  await capture('dark');
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1080, 700));
  await page.waitForTimeout(250);
  await expectStep('mcp');
  check('resizing the open tour keeps the real MCP control in its spotlight', !await tour.locator('.tour-missing').isVisible());
  await fit('small_dark');
  await capture('small-dark');
  await page.keyboard.press('Escape');
  await closed();
  check('Escape pauses without losing the current step', (await storage()).step === 7 && (await storage()).status === 'paused');
  await page.getByRole('button', { name: '浅色主题', exact: true }).click();
  await launch.click();
  await expectStep('mcp');
  await fit('small_light');
  await capture('small-light');
  await tour.getByRole('button', { name: '完成引导', exact: true }).click();
  await closed();
  check('all eight planned tutorial steps were visited', steps.every(step => result.steps.includes(step)));
  check('completion is persisted', (await storage()).status === 'done');
  await page.reload();
  await page.locator('#bm-chat-input').waitFor();
  await page.waitForTimeout(350);
  check('completed tutorial stays closed after reload', !await tour.isVisible());

  // Tutorial navigation is subject to the same draft protection as normal UI.
  const chatDraft = '未发送的业务问题：退款流程有哪些步骤？';
  await page.locator('#bm-chat-input').fill(chatDraft);
  await launch.click();
  await expectStep('intro');
  check('reopening completed tutorial starts at the beginning', true);
  await next('knowledge');
  await page.keyboard.press('Escape');
  await closed();
  await nav.getByRole('button', { name: 'AI 问答', exact: true }).click();
  check('chat draft survives tutorial navigation', await page.locator('#bm-chat-input').inputValue() === chatDraft);
  await nav.getByRole('button', { name: '业务地图', exact: true }).click();
  await page.locator('.note-article').waitFor();
  const notePath = await page.locator('.note-article').getAttribute('data-note-path');
  const original = await api(`/api/note?path=${encodeURIComponent(notePath)}`);
  await page.getByRole('button', { name: '编辑 Markdown', exact: true }).click();
  const editor = page.locator('.cm-content');
  const marker = '引导验收：拒绝离开时这段未保存内容必须保留。';
  await editor.fill(`${original.content}\n\n${marker}`);
  await page.getByText('有未保存的更改', { exact: false }).waitFor();
  await launch.click();
  await expectStep('knowledge');
  let guardMessage = '';
  page.once('dialog', async dialog => { guardMessage = dialog.message(); await dialog.dismiss(); });
  await tour.getByRole('button', { name: '下一步', exact: true }).click();
  await expectStep('knowledge');
  check('tutorial respects the unsaved Markdown leave guard', /未保存/.test(guardMessage) && (await editor.textContent()).includes(marker));
  await page.keyboard.press('Escape');
  await closed();
  check('closing blocked tutorial leaves the original editor available', await editor.isVisible() && (await editor.textContent()).includes(marker));
  await editor.fill(original.content);
  await page.getByRole('button', { name: '已保存', exact: true }).waitFor();
  await launch.click();
  await tour.getByRole('button', { name: '从头开始', exact: true }).click();
  await expectStep('intro');
  check('restart explicitly returns to intro', true);
  await next('knowledge');
  await next('graph');
  // Controlled missing-target setup uses CSS on the existing real control; it
  // does not add any product hook or change navigation/permission behavior.
  const hiddenTargetStyle = await page.addStyleTag({ content: '[data-tour="model-add"] { display: none !important; }' });
  await tour.getByRole('button', { name: '下一步', exact: true }).click();
  await page.locator('dialog[data-tour-step="models"] .tour-missing').waitFor();
  check('a missing target presents readable fallback guidance', await tour.getByRole('button', { name: '暂时跳过', exact: true }).isEnabled());
  await tour.getByRole('button', { name: '暂时跳过', exact: true }).click();
  await closed();
  await hiddenTargetStyle.evaluate(element => element.remove());
  check('missing target can still be skipped', !await tour.isVisible());

  // Corruption is limited to the test profile. A malformed preference must never
  // prevent entering, advancing through, or closing the tutorial.
  for (const [name, malformed] of [['invalid-json', '{broken'], ['invalid-step', JSON.stringify({ status: 'paused', step: 9999 })]]) {
    await page.evaluate(({ key, malformed }) => localStorage.setItem(key, malformed), { key, malformed });
    await page.reload();
    await page.locator('#bm-chat-input').waitFor();
    await page.waitForTimeout(350);
    if (!await tour.isVisible()) await launch.click();
    await tour.waitFor();
    const step = await tour.evaluate(element => element.getAttribute('data-tour-step') || element.querySelector('[data-tour-step]')?.getAttribute('data-tour-step'));
    check(`${name} preference recovers to a usable tutorial step`, steps.includes(step));
    await page.keyboard.press('Escape');
    await closed();
    const stored = await storage();
    check(`${name} is replaced with valid minimal state`, Object.keys(stored).sort().join(',') === 'status,step' && stored.status === 'paused' && Number.isInteger(stored.step) && stored.step >= 0 && stored.step < steps.length);
  }

  const after = await snapshot();
  check('tutorial and cancelled forms leave all Markdown and business data unchanged', JSON.stringify(before) === JSON.stringify(after));
  const requests = await desktop.evaluate(() => globalThis.__onboardingRequests);
  result.request_observation = { total: requests.length, mutations: requests.filter(request => request.method !== 'GET') };
  check('tutorial never automatically writes data, executes SQL or calls a model', result.request_observation.mutations.length === 0);
  check('renderer has no unhandled exceptions', result.renderer_errors.length === 0);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.failure = { message: error.message, stack: error.stack };
  if (page) await capture('failure').catch(() => {});
  process.exitCode = 1;
} finally {
  await writeFile(resolve(artifacts, `${prefix}.json`), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  // Only this disposable test instance is closed; user windows are untouched.
  await desktop.close();
}
