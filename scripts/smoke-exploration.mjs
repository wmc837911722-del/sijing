import { _electron as electron, expect as playwrightExpect } from '@playwright/test';
import electronBinary from 'electron';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dismissOnboarding } from './dismiss-onboarding.mjs';

// Actual desktop, persisted sessions and tool loop; only the remote model is a
// deterministic loopback HTTP fixture. Never uses the user's workspace or keys.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const expect = playwrightExpect.configure({ timeout: 30_000 });
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const label = packaged ? 'packaged' : 'source';
const artifacts = resolve(root, 'artifacts', 'exploration', label);
const testDir = resolve(root, '.tmp', `exploration-${label}-${Date.now()}`);
await mkdir(artifacts, { recursive: true });
await mkdir(testDir, { recursive: true });
const result = { label, platform: process.platform, tested_at: new Date().toISOString(), checks: [], renderer_errors: [] };
const check = (name, passed) => { assert.ok(passed, name); result.checks.push(name); console.log(`PASS ${name}`); };
const entryPath = '业务测试/退款入口.md';
const evidencePath = '业务测试/售后规则.md';
let releaseGate = () => {};
let gate;
const hold = () => { gate = new Promise(resolveGate => { releaseGate = resolveGate; }); };
const release = () => { releaseGate(); gate = undefined; };
const requests = [];
const server = createServer(async (request, response) => {
  try {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const receipts = body.messages.filter(message => message.role === 'tool').map(message => JSON.parse(message.content));
    requests.push({ model: body.model, steps: receipts.length });
    if (receipts.length === 1 && gate) await gate;
    let name, args;
    if (!receipts.length) {
      name = 'knowledge_search'; args = { query: '退款入口' };
    } else if (receipts.length === 1) {
      name = 'knowledge_outline'; args = { path: receipts[0].notes.find(note => note.path === entryPath).path };
    } else if (receipts.length === 2) {
      name = 'knowledge_links'; args = { path: receipts[1].path, direction: 'outgoing' };
    } else if (receipts.length === 3) {
      name = 'knowledge_read'; args = { path: receipts[2].edges[0].target, heading: '退款条件' };
    } else {
      assert.ok(receipts[3].content.includes('支付完成后七日内'));
      name = 'finish_answer'; args = { answer: '退款条件：支付完成后七日内，且商品尚未发货。已沿退款入口找到售后规则的对应章节。', citations: [receipts[3].path], kind: 'answer' };
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ finish_reason: 'tool_calls', message: { role: 'assistant', content: '',
      tool_calls: [{ id: `fixture-${receipts.length}`, type: 'function', function: { name, arguments: JSON.stringify(args) } }] } }],
      usage: { prompt_tokens: 100, completion_tokens: 20, prompt_cache_hit_tokens: 60 } }));
  } catch (error) {
    response.writeHead(500, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ error: String(error) }));
  }
});
await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done); });
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, PYTHONUTF8: '1',
  TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp'), PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring' };
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
  const navigateChat = () => page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: 'AI 问答', exact: true }).click();
  const input = page.locator('#bm-chat-input');
  const capture = async name => {
    await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
    await page.waitForTimeout(250);
    const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    await writeFile(resolve(artifacts, name), Buffer.from(png, 'base64'));
  };
  await api('PUT', '/api/note', { path: entryPath, content: '# 退款入口\n退款口径参见 [[业务测试/售后规则#退款条件]]。\n' });
  await api('PUT', '/api/note', { path: evidencePath, content: '# 售后规则\n\n## 背景资料\n' + '本段为其他背景资料。'.repeat(500) + '\n\n## 退款条件\n支付完成后七日内，且商品尚未发货。\n' });
  await api('POST', '/api/providers', { name: '渐进探索测试模型', kind: 'openai-compatible', base_url: `http://127.0.0.1:${server.address().port}/v1`, model: 'local-exploration-fixture' });
  await page.reload(); await input.waitFor();
  hold();
  await input.fill('沿退款入口查找退款条件'); await input.press('Enter');
  await expect(page.locator('.bm-chat-progress .bm-chat-exploration li')).toHaveCount(1);
  check('live receipts appear during the local in-flight request', await page.locator('.bm-chat-progress').getByText('搜索业务笔记').count() > 0);
  check('polling does not duplicate the submitted user message', await page.locator('.bm-chat-user').count() === 1);
  await expect(page.getByRole('button', { name: '停止回答', exact: true })).toBeEnabled();
  await input.fill('回答期间保留的草稿');
  await capture('progress-light.png');
  release();
  await expect(page.locator('.bm-chat-assistant')).toHaveCount(1);
  await expect(page.locator('.bm-chat-waiting')).toHaveCount(0);
  check('draft survives completion', await input.inputValue() === '回答期间保留的草稿');
  check('final answer uses the distant linked section', (await page.locator('.bm-chat-assistant').innerText()).includes('支付完成后七日内'));
  const finished = (await api('GET', '/api/chats')).chats[0];
  const saved = await api('GET', `/api/chats/${finished.id}`);
  const assistant = saved.messages.at(-1);
  check('five distinct executed steps persist', assistant.activities.map(item => item.tool).join(',') === 'knowledge_search,knowledge_outline,knowledge_links,knowledge_read,finish_answer');
  check('only actually read evidence is cited with its revision', assistant.sources.length === 1 && assistant.sources[0].path === evidencePath && !!assistant.sources[0].revision);
  check('completed exploration starts collapsed', !(await page.locator('.bm-chat-assistant details.bm-chat-exploration').evaluate(element => element.open)));
  const summary = page.locator('.bm-chat-assistant .bm-chat-exploration summary');
  await summary.focus(); await summary.press('Enter');
  check('keyboard opens the completed exploration record', await page.locator('.bm-chat-assistant details.bm-chat-exploration').evaluate(element => element.open));
  await page.getByRole('button', { name: evidencePath, exact: true }).click();
  await expect(input).not.toBeVisible();
  await expect(page.getByRole('tab', { name: '售后规则', exact: true })).toHaveAttribute('aria-selected', 'true');
  check('receipt opens its actual Markdown note', true);
  await navigateChat(); await input.waitFor();
  for (const theme of ['浅色', '深色']) {
    await page.getByRole('button', { name: `${theme}主题`, exact: true }).click();
    for (const size of [[1500, 940], [1080, 700]]) {
      await desktop.evaluate(({ BrowserWindow }, dimensions) => BrowserWindow.getAllWindows()[0].setSize(...dimensions), size);
      await page.locator('.bm-chat-conversation').evaluate(element => { element.scrollTop = 0; });
      const geometry = await page.evaluate(() => {
        const main = document.querySelector('.bm-chat-main');
        const send = document.querySelector('.bm-chat-send').getBoundingClientRect();
        return { overflow: main.scrollWidth > main.clientWidth + 1, visible: send.bottom <= innerHeight && send.right <= innerWidth };
      });
      check(`exploration fits ${size.join('x')} ${theme}`, !geometry.overflow && geometry.visible);
      await capture(`completed-${size.join('x')}-${theme === '浅色' ? 'light' : 'dark'}.png`);
    }
  }
  await page.reload(); await input.waitFor();
  await page.getByRole('navigation', { name: '选择对话' }).getByRole('button', { name: /沿退款入口查找退款条件/ }).click();
  await expect(page.locator('.bm-chat-assistant .bm-chat-exploration li')).toHaveCount(5);
  check('reopening preserves the exploration and citations', await page.locator('.bm-chat-assistant .bm-chat-exploration li').count() === 5 && await page.locator('.bm-chat-sources button').count() === 1);
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await page.getByLabel('问答模式', { exact: true }).selectOption('data');
  await page.getByLabel('问答使用的数据库', { exact: true }).selectOption('demo');
  hold();
  const beforeStopRequests = requests.length;
  await input.fill('停止前探索退款数据'); await input.press('Enter');
  await expect(page.locator('.bm-chat-progress .bm-chat-exploration li')).toHaveCount(1);
  await page.getByRole('button', { name: '停止回答', exact: true }).click();
  await expect(page.getByRole('button', { name: '正在停止', exact: true })).toBeDisabled();
  check('stop explains that the current call must finish', (await page.locator('.bm-chat-waiting').innerText()).includes('当前模型调用或工具执行结束后停止'));
  await capture('stopping-dark.png');
  release();
  await expect(page.locator('.bm-chat-waiting')).toHaveCount(0);
  await expect(page.locator('.bm-chat-message-error')).toHaveCount(1);
  const stoppedSummary = (await api('GET', '/api/chats')).chats.find(chat => chat.title === '停止前探索退款数据');
  const stopped = await api('GET', `/api/chats/${stoppedSummary.id}`);
  check('cancellation stops before another tool or SQL executes', requests.length - beforeStopRequests === 2 && stopped.messages.at(-1).activities.length === 1 && !stopped.messages.at(-1).result);
  check('cancelled steps persist while pending state is cleared', stopped.messages.at(-1).completion === 'limited' && !stopped.pending && stopped.pending_activity.length === 0);
  check('no duplicate question after cancellation', await page.locator('.bm-chat-user').count() === 1);
  check('no renderer exceptions', result.renderer_errors.length === 0);
  await writeFile(resolve(artifacts, 'checks.json'), JSON.stringify(result, null, 2));
} finally {
  release();
  await desktop?.close();
  await new Promise(done => server.close(done));
}
