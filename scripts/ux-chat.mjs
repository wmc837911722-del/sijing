import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron, expect } from '@playwright/test';
import electronBinary from 'electron';
import { createServer as createViteServer } from 'vite';
import { createServer } from 'node:http';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = resolve(root, 'artifacts', 'ux-chat');
await mkdir(artifacts, { recursive: true });
const checks = [];
const record = (name, passed, detail) => { checks.push({ name, passed, detail }); console.log(`${passed ? 'PASS' : 'FAIL'} ${name}${detail ? `: ${detail}` : ''}`); };
let fixtureFailure = false;
let fixtureGate;
let releaseFixture = () => {};
const longAnswer = ['# 订单处理说明', ...Array.from({ length: 28 }, (_, i) => `## 步骤 ${i + 1}\n\n这里说明订单处理的业务规则、状态变化和查询依据。\n\n这是一段用于检查实际窗口排版的长回答。`), '```sql\nSELECT ' + 'a_long_column_name, '.repeat(25) + 'final_column FROM orders;\n```', '| 字段 | 含义 |\n| --- | --- |\n| order_id | 订单标识 |', '回答结束标记。'].join('\n\n');
const fixtureNotePath = '测试/长回答依据.md';
const fixture = createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (fixtureGate) await fixtureGate;
  await new Promise(done => setTimeout(done, 500));
  response.writeHead(fixtureFailure ? 503 : 200, { 'content-type': 'application/json' });
  const read = body.messages.some(message => message.role === 'tool');
  const name = read ? 'finish_answer' : 'knowledge_read';
  const args = read ? { answer: longAnswer, citations: [fixtureNotePath], kind: 'answer' } : { path: fixtureNotePath };
  response.end(JSON.stringify(fixtureFailure ? { error: { message: 'UX fixture unavailable' } } : { choices: [{ message: { role: 'assistant', content: '', tool_calls: [{ id: `ux-${name}`, type: 'function', function: { name, arguments: JSON.stringify(args) } }] }, finish_reason: 'tool_calls' }], usage: { prompt_tokens: 20, completion_tokens: 40 } }));
});
await new Promise(done => fixture.listen(0, '127.0.0.1', done));
const vite = await createViteServer({ root, server: { host: '127.0.0.1', port: 0, hmr: false, watch: null } });
await vite.listen();
const env = { ...process.env, BUSINESSMAP_DEV_URL: `http://127.0.0.1:${vite.httpServer.address().port}`, BUSINESSMAP_DATA_DIR: resolve(root, '.tmp', `ux-chat-${Date.now()}`), BUSINESSMAP_HEADLESS: '1', PYTHONUTF8: '1' };
delete env.ELECTRON_RUN_AS_NODE;
let desktop;
try {
  desktop = await electron.launch({ executablePath: electronBinary, args: [root], cwd: root, env, chromiumSandbox: true, timeout: 90000 });
  const page = await desktop.firstWindow({ timeout: 90000 });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await dismissOnboarding(page);
  const input = page.locator('#bm-chat-input');
  const newButton = page.getByRole('button', { name: '新建对话', exact: true });
  const history = page.getByRole('navigation', { name: '选择对话' });
  const api = (method, path, body) => page.evaluate(({ method, path, body }) => window.businessmap.request(method, path, body), { method, path, body });
  const capture = async filename => {
    await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
    await page.waitForTimeout(250);
    const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    await writeFile(resolve(artifacts, filename), Buffer.from(png, 'base64'));
  };
  await input.waitFor({ state: 'visible' });
  await input.fill('初始草稿应保留');
  const before = (await api('GET', '/api/chats')).chats.length;
  await newButton.click(); await expect(newButton).toBeEnabled();
  await newButton.click(); await expect(newButton).toBeEnabled();
  const after = (await api('GET', '/api/chats')).chats.length;
  record('repeated new chat does not persist empty conversations', after === before, `${before} -> ${after}`);
  record('new chat keeps initial unsent draft accessible', await input.inputValue() === '初始草稿应保留');

  await input.fill('退款规则');
  await input.press('Enter');
  await page.locator('.bm-chat-assistant').first().waitFor();
  record('Enter sends and references open real knowledge', (await page.locator('.bm-chat-sources button').count()) > 0);
  record('first send restores keyboard focus to the composer', await input.evaluate(element => document.activeElement === element));
  await input.fill('已发会话的后续草稿');
  await newButton.click(); await expect(newButton).toBeEnabled();
  await input.fill('未发送的新会话草稿');
  await history.getByRole('button', { name: /退款规则/ }).click();
  record('history preserves existing session draft', await input.inputValue() === '已发会话的后续草稿', await input.inputValue());
  await newButton.click(); await expect(newButton).toBeEnabled();
  record('new draft survives an existing history visit', await input.inputValue() === '未发送的新会话草稿', await input.inputValue());
  await input.fill('第一行'); await input.press('Shift+Enter'); await input.pressSequentially('第二行');
  record('Shift+Enter inserts a newline', await input.inputValue() === '第一行\n第二行');
  await input.fill('中文输入法正在选字');
  const beforeComposition = (await api('GET', '/api/chats')).chats.length;
  await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true });
  record('IME confirmation Enter does not submit', await input.inputValue() === '中文输入法正在选字' && (await api('GET', '/api/chats')).chats.length === beforeComposition);

  await input.fill('限'.repeat(8001));
  const sendButton = page.getByRole('button', { name: '发送问题', exact: true });
  record('question longer than API limit is retained and blocked before sending', await sendButton.isDisabled() && (await input.inputValue()).length === 8001);
  await input.fill('连接失败时仍需保留的问题');
  await desktop.evaluate(() => {
    const original = globalThis.fetch;
    globalThis.fetch = (input, ...rest) => {
      if (/\/api\/chats\/[^/]+\/messages$/.test(String(input))) { globalThis.fetch = original; return Promise.reject(new Error('UX simulated connection interruption')); }
      return original(input, ...rest);
    };
  });
  await sendButton.click();
  await page.locator('.bm-chat-failure').waitFor();
  await page.getByRole('button', { name: '重新加载对话', exact: true }).click();
  await expect(sendButton).toBeVisible();
  await page.waitForTimeout(250);
  record('reload after transport failure preserves unsaved question', (await input.inputValue()).includes('连接失败时仍需保留的问题') || (await page.locator('.bm-chat-failure').count()) > 0);

  await api('PUT', '/api/note', { path: fixtureNotePath, content: '# 订单处理依据\n订单由创建到支付完成；本文用于本地界面验证。\n' });
  await api('POST', '/api/providers', { name: 'UX 长回答测试', kind: 'openai-compatible', base_url: `http://127.0.0.1:${fixture.address().port}/v1`, model: 'ux-test' });
  await page.reload(); await input.waitFor({ state: 'visible' });
  fixtureGate = new Promise(done => { releaseFixture = done; });
  await input.fill('长回答排版检查'); await input.press('Enter');
  await page.locator('.bm-chat-waiting').waitFor();
  await expect(newButton).toBeEnabled();
  await newButton.click(); await input.fill('后台答复时的新草稿');
  await history.locator('button').filter({ has: page.locator('.bm-chat-spin') }).click();
  record('switching back during a request shows its real pending state', await page.locator('.bm-chat-waiting').isVisible());
  await input.fill('等待回答时写下一个问题');
  releaseFixture(); fixtureGate = undefined;
  await page.getByText('回答结束标记。', { exact: true }).waitFor();
  await expect(page.locator('.bm-chat-waiting')).toHaveCount(0);
  record('answer completion preserves the follow-up draft', await input.inputValue() === '等待回答时写下一个问题');
  await newButton.click();
  record('background request preserves separate new draft', await input.inputValue() === '后台答复时的新草稿');
  await history.getByRole('button', { name: /长回答排版检查/ }).click();
  for (const [width, height] of [[1500, 940], [1080, 700]]) {
    await desktop.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setSize(...size), [width, height]);
    for (const theme of ['浅色', '深色']) {
      await page.getByRole('button', { name: `${theme}主题`, exact: true }).click();
      await page.waitForTimeout(250);
      const dimensions = await page.evaluate(() => {
        const main = document.querySelector('.bm-chat-main');
        const send = document.querySelector('.bm-chat-send').getBoundingClientRect();
        const conversation = document.querySelector('.bm-chat-conversation');
        return { overflow: main.scrollWidth > main.clientWidth + 1, buttonVisible: send.bottom <= innerHeight && send.right <= innerWidth, scrollable: conversation.scrollHeight > conversation.clientHeight };
      });
      record(`${width}x${height} ${theme} long answer layout`, !dimensions.overflow && dimensions.buttonVisible && dimensions.scrollable, JSON.stringify(dimensions));
      await capture(`chat-${width}-${theme === '浅色' ? 'light' : 'dark'}.png`);
    }
  }
  await page.locator('.bm-chat-conversation').evaluate(element => { element.scrollTop = 0; });
  await page.waitForTimeout(100);
  await input.fill('阅读历史时写草稿');
  record('typing does not move the reader away from earlier messages', await page.locator('.bm-chat-conversation').evaluate(element => element.scrollTop < 10));
  fixtureFailure = true;
  await input.fill('触发模型错误'); await input.press('Enter');
  await page.locator('.bm-chat-message-error').waitFor();
  record('provider failure remains visible and enables further input', await input.isEnabled() && !(await page.locator('.bm-chat-waiting').count()));
  fixtureFailure = false;
  await input.fill('错误后继续问答'); await input.press('Enter');
  await expect(page.getByText('回答结束标记。', { exact: true })).toHaveCount(2);
  record('session continues after provider recovers', true);
  record('no renderer exceptions', errors.length === 0, errors.join('; '));
  await writeFile(resolve(artifacts, 'checks.json'), JSON.stringify({ dataDirectory: env.BUSINESSMAP_DATA_DIR, checks }, null, 2));
  assert.equal(checks.filter(check => !check.passed).length, 0, 'Chat UX checks failed; see artifacts/ux-chat/checks.json');
} finally {
  releaseFixture();
  await desktop?.close();
  await vite.close();
  await new Promise(done => fixture.close(done));
}
