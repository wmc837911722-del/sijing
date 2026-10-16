import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dismissOnboarding } from './dismiss-onboarding.mjs';

// Uses an isolated workspace and a loopback model fixture. No external model
// calls, user credentials, or the user's existing workspace are involved.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const label = packaged ? 'packaged' : 'source';
const artifacts = resolve(root, 'artifacts', 'model-providers', label);
const testDir = resolve(root, '.tmp', `model-providers-${label}-${Date.now()}`);
await mkdir(artifacts, { recursive: true });
await mkdir(testDir, { recursive: true });
const result = { label, platform: process.platform, tested_at: new Date().toISOString(), checks: [], screenshots: [], presets: [], renderer_errors: [] };
const check = (name, value) => { assert.ok(value, name); result.checks.push(name); console.log(`PASS ${name}`); };
const requests = [];
const fakeKey = 'synthetic-model-provider-smoke-key';
const mock = createServer(async (request, response) => {
  try {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    requests.push({ method: request.method, path: request.url, authorization: request.headers.authorization, body });
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({
      id: 'local-provider-fixture', choices: [{ message: { role: 'assistant', content: '连接成功' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 100, completion_tokens: 5, total_tokens: 105, prompt_cache_hit_tokens: 64, prompt_cache_miss_tokens: 36 },
    }));
  } catch {
    response.writeHead(400, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: 'Invalid local fixture request' }));
  }
});
await new Promise((resolveServer, reject) => { mock.once('error', reject); mock.listen(0, '127.0.0.1', resolveServer); });
const mockBase = `http://127.0.0.1:${mock.address().port}/v1`;
const env = {
  ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, PYTHONUTF8: '1',
  PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring', TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp'),
};
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
let desktop;
try {
  desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true, timeout: 90_000 });
  result.version = await desktop.evaluate(({ app }) => app.getVersion());
  const page = await desktop.firstWindow({ timeout: 90_000 });
  page.setDefaultTimeout(20_000);
  page.on('pageerror', error => result.renderer_errors.push(error.message));
  await dismissOnboarding(page);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const api = (method, path, body) => page.evaluate(({ method, path, body }) => window.businessmap.request(method, path, body), { method, path, body });
  const navigate = () => page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '模型配置', exact: true }).click();
  const resize = async (width, height) => {
    await desktop.evaluate(({ BrowserWindow }, dimensions) => BrowserWindow.getAllWindows()[0].setSize(...dimensions), [width, height]);
    await page.waitForTimeout(200);
  };
  const capture = async name => {
    await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
    await page.waitForTimeout(250);
    const geometry = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, overflow: document.documentElement.scrollWidth - innerWidth, theme: document.documentElement.dataset.theme }));
    check(`${name}: no horizontal viewport overflow`, geometry.overflow <= 1);
    const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    await writeFile(resolve(artifacts, `${name}.png`), Buffer.from(png, 'base64'));
    result.screenshots.push({ file: `${name}.png`, ...geometry });
  };
  const openNew = async () => {
    await page.getByRole('button', { name: '添加模型', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '添加模型服务', exact: true });
    await dialog.waitFor();
    return dialog;
  };
  const save = async dialog => {
    await dialog.getByRole('button', { name: '保存配置', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
  };
  const card = name => page.locator('.provider-card').filter({ has: page.getByRole('button', { name: `编辑 ${name}`, exact: true }) });
  const testConnection = async (name, model, authorization) => {
    const before = requests.length;
    await card(name).getByRole('button', { name: '测试', exact: true }).click();
    await card(name).getByRole('button', { name: '测试', exact: true }).waitFor();
    check(`${name}: test button sends exactly one loopback request`, requests.length === before + 1);
    const sent = requests.at(-1);
    check(`${name}: adapter uses POST /v1/chat/completions`, sent.method === 'POST' && sent.path === '/v1/chat/completions');
    check(`${name}: custom model ID reaches adapter unchanged`, sent.body.model === model);
    check(`${name}: adapter uses the expected credential`, sent.authorization === authorization);
  };

  await resize(1440, 940);
  await page.getByRole('button', { name: '浅色主题', exact: true }).click();
  await navigate();
  let dialog = await openNew();
  const preset = dialog.getByLabel('服务预设', { exact: false });
  check('new model defaults to Kimi', await preset.inputValue() === 'kimi');
  check('new Kimi model requires a user-supplied model ID', await dialog.getByLabel('模型 ID', { exact: true }).inputValue() === '' && await dialog.getByLabel('模型 ID', { exact: true }).getAttribute('required') !== null);
  const groups = await preset.locator('optgroup').evaluateAll(elements => elements.map(element => element.label));
  assert.deepEqual(groups, ['国内平台', '国际平台', '自定义与本地']);
  check('preset picker groups domestic, international, and local services', true);
  const expectedHosts = {
    kimi: 'api.moonshot.cn', deepseek: 'api.deepseek.com', qwen: 'dashscope.aliyuncs.com',
    zhipu: 'open.bigmodel.cn', doubao: 'ark.cn-beijing.volces.com', siliconflow: 'api.siliconflow.cn',
    hunyuan: 'tokenhub.tencentmaas.com', qianfan: 'qianfan.baidubce.com',
    minimax: 'api.minimax.cn', spark: 'spark-api-open.xf-yun.com',
    stepfun: 'api.stepfun.com', baichuan: 'api.baichuan-ai.com',
    openai: 'api.openai.com', anthropic: 'api.anthropic.com', gemini: 'generativelanguage.googleapis.com',
    ollama: '127.0.0.1',
  };
  const keys = await preset.locator('option').evaluateAll(elements => elements.map(element => element.value));
  assert.deepEqual([...keys].sort(), [...Object.keys(expectedHosts), 'custom'].sort());
  check('all 12 domestic presets and existing international/local options are available', true);
  for (const [key, expectedHost] of Object.entries(expectedHosts)) {
    await preset.selectOption(key);
    const base = await dialog.getByLabel('API 地址', { exact: true }).inputValue();
    const parsed = new URL(base);
    check(`${key}: preset uses expected service endpoint`, parsed.hostname === expectedHost && parsed.protocol === (key === 'ollama' ? 'http:' : 'https:'));
    const kind = await dialog.getByLabel('接口类型', { exact: false }).inputValue();
    check(`${key}: adapter matches service API`, kind === (key === 'anthropic' ? 'anthropic' : key === 'gemini' ? 'gemini' : 'openai-compatible'));
    result.presets.push({ preset: key, base_url: base, kind });
  }
  await preset.selectOption('kimi');
  await dialog.getByLabel('API 密钥', { exact: false }).fill(fakeKey);
  await preset.selectOption('deepseek');
  check('switching a service preset clears the prior API key', await dialog.getByLabel('API 密钥', { exact: false }).inputValue() === '');
  await preset.selectOption('kimi');
  await capture('light-kimi-form');

  const kimiName = 'Kimi · 本地验证';
  const kimiModel = 'moonshot-future-private-model';
  await dialog.getByLabel('显示名称', { exact: true }).fill(kimiName);
  await dialog.getByLabel('模型 ID', { exact: true }).fill(kimiModel);
  await dialog.getByLabel('API 密钥', { exact: false }).fill(fakeKey);
  await save(dialog);
  let settings = await api('GET', '/api/settings');
  let provider = settings.providers.find(item => item.name === kimiName);
  check('Kimi saves arbitrary model IDs without a whitelist', provider?.model === kimiModel && provider?.base_url === 'https://api.moonshot.cn/v1');
  check('settings never return the API key', !JSON.stringify(settings).includes(fakeKey) && provider?.has_key === true);
  await page.getByRole('button', { name: `编辑 ${kimiName}`, exact: true }).click();
  dialog = page.getByRole('dialog', { name: '编辑模型服务', exact: true });
  check('editing Kimi preserves its model ID', await dialog.getByLabel('模型 ID', { exact: true }).inputValue() === kimiModel);
  check('editing keeps credentials out of the form', await dialog.getByLabel('API 密钥', { exact: false }).inputValue() === '');
  await dialog.getByLabel('API 地址', { exact: true }).fill(mockBase);
  await save(dialog);
  await testConnection(kimiName, kimiModel, `Bearer ${fakeKey}`);
  settings = await api('GET', '/api/settings');
  check('DeepSeek-compatible cache metrics use actual response usage', settings.usage.requests === 1 && settings.usage.input_tokens === 100 && settings.usage.output_tokens === 5 && settings.usage.cached_tokens === 64);

  dialog = await openNew();
  await dialog.getByLabel('服务预设', { exact: false }).selectOption('custom');
  const customName = '内网模型 · 本地验证';
  const customModel = 'company/team-model-2027';
  await dialog.getByLabel('显示名称', { exact: true }).fill(customName);
  await dialog.getByLabel('API 地址', { exact: true }).fill(mockBase);
  await dialog.getByLabel('模型 ID', { exact: true }).fill(customModel);
  await save(dialog);
  await page.reload();
  await navigate();
  settings = await api('GET', '/api/settings');
  provider = settings.providers.find(item => item.name === kimiName);
  check('Kimi custom endpoint and ID persist across renderer reload', provider?.base_url === mockBase && provider?.model === kimiModel);
  provider = settings.providers.find(item => item.name === customName);
  check('custom service and ID persist across renderer reload', provider?.base_url === mockBase && provider?.model === customModel && provider?.has_key === false);
  await testConnection(customName, customModel, undefined);
  await testConnection(kimiName, kimiModel, `Bearer ${fakeKey}`);

  await resize(1080, 700);
  await page.getByRole('button', { name: '深色主题', exact: true }).click();
  dialog = await openNew();
  await dialog.getByRole('button', { name: '保存配置', exact: true }).scrollIntoViewIfNeeded();
  check('small model form save action is reachable', await dialog.getByRole('button', { name: '保存配置', exact: true }).evaluate(element => { const r = element.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth; }));
  check('small model form header and close button stay visible after scrolling', await dialog.evaluate(element => ['.modal-surface > header h2', '.modal-surface > header button'].every(selector => {
    const r = element.querySelector(selector)?.getBoundingClientRect();
    return r && r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
  })));
  check('small model form has no horizontal content overflow', await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1 && [...element.querySelectorAll('input, select')].every(input => { const r = input.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })));
  await capture('small-dark-kimi-form');
  await page.keyboard.press('Escape');
  check('model dialog restores keyboard focus to its trigger', await page.locator('[data-tour="model-add"]').evaluate(element => element === document.activeElement));
  check('only three explicit loopback model tests were sent', requests.length === 3);
  check('no renderer exceptions', result.renderer_errors.length === 0);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.failure = String(error);
  throw error;
} finally {
  await writeFile(resolve(artifacts, 'result.json'), JSON.stringify(result, null, 2));
  if (desktop) await desktop.close();
  await new Promise(resolveServer => mock.close(resolveServer));
}
