import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const testDir = resolve(root, '.tmp', `graph-lifecycle-${Date.now()}`);
const artifacts = resolve(root, 'artifacts', 'ux-graph');
await mkdir(artifacts, { recursive: true });
const secondVault = resolve(testDir, 'second-vault');
await mkdir(secondVault, { recursive: true });
await writeFile(resolve(secondVault, '第二知识库.md'), '# 第二知识库\n\n[[独立节点]]');
await writeFile(resolve(secondVault, '独立节点.md'), '# 独立节点');
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp') };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const fallback = process.argv.includes('--no-gpu');
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true });
const result = { version: await desktop.evaluate(({ app }) => app.getVersion()), packaged: !!packaged, fallback, fallback_setup: fallback ? 'Controlled WebGL2 context creation failure; real Canvas renderer, not a hardware-disable test.' : undefined, checks: {}, renderer_errors: [] };
const page = await desktop.firstWindow();
const graph = page.locator('.graph-view:not(.local) .graph-canvas');
const check = (name, value) => { result.checks[name] = value; assert.ok(value, name); };
const snapshot = () => graph.evaluate(el => { const cy = el._cyreg.cy; return { zoom: cy.zoom(), pan: cy.pan(), nodes: cy.nodes().map(n => ({ id: n.id(), ...n.position() })) }; });
const settle = () => graph.evaluate(el => new Promise((resolve, reject) => { const started = Date.now(); const timer = setInterval(() => { if (el.dataset.layoutState === 'settled') { clearInterval(timer); resolve(); } else if (Date.now() - started > 10000) { clearInterval(timer); reject(new Error(`Layout did not settle: ${el.dataset.layoutState}`)); } }, 50); }));
const capture = async name => {
  await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(); });
  await page.waitForTimeout(200);
  const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG().toString('base64'));
  await writeFile(resolve(artifacts, `lifecycle-${name}.png`), Buffer.from(png, 'base64'));
};
try {
  page.on('pageerror', error => result.renderer_errors.push(error.message));
  await dismissOnboarding(page);
  await page.locator('#bm-chat-input').waitFor();
  if (fallback) await page.evaluate(() => {
    // Exercise the real fallback with a reproducible capability failure. Electron
    // on this host ignores disable-WebGL flags, so do not claim absent hardware.
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) { return type === 'webgl2' ? null : original.call(this, type, ...args); };
  });
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].showInactive());
  await page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '业务地图', exact: true }).click();
  await page.getByRole('button', { name: '关系图谱', exact: true }).click();
  await settle();
  if (fallback) check('graph uses Canvas after WebGL2 capability failure', await graph.getAttribute('data-renderer') === 'canvas');
  check('worker layout is actually active in production scheme', await graph.getAttribute('data-layout-state') === 'settled');
  const before = await snapshot();
  await graph.evaluate(el => { window.__originalGraph = el._cyreg.cy; });
  await page.getByLabel('筛选图谱节点').fill('不存在的节点');
  await page.getByText('没有匹配的节点', { exact: true }).waitFor();
  check('empty filter has no stale visible nodes', await graph.evaluate(el => el._cyreg.cy.nodes(':visible').length === 0));
  await page.getByRole('button', { name: '清除图谱筛选', exact: true }).click();
  check('filter clearing preserves graph instance and positions', await graph.evaluate(el => el._cyreg.cy === window.__originalGraph) && JSON.stringify((await snapshot()).nodes) === JSON.stringify(before.nodes));
  await capture('light');
  await page.getByRole('button', { name: '深色主题', exact: true }).click();
  check('theme switching keeps world coordinates and viewport', JSON.stringify(await snapshot()) === JSON.stringify(before));
  await capture('dark');

  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.waitForTimeout(80);
  const box = await graph.boundingBox();
  const target = await graph.evaluate(el => { const n = el._cyreg.cy.nodes()[0]; return { id: n.id(), ...n.renderedPosition() }; });
  await page.mouse.move(box.x + target.x, box.y + target.y);
  await page.mouse.down();
  await page.mouse.move(box.x + target.x + 45, box.y + target.y + 30, { steps: 12 });
  await page.mouse.up();
  await settle();
  const after = await snapshot();
  check('reduced motion keeps other nodes still while dragging', after.nodes.filter(n => n.id !== target.id).every(n => { const old = before.nodes.find(p => p.id === n.id); return Math.hypot(n.x - old.x, n.y - old.y) < .01; }));
  check('drag opens no document', await graph.isVisible());
  const zoomBefore = after.zoom;
  await page.getByRole('button', { name: '放大图谱', exact: true }).click();
  check('reduced motion zoom changes immediately', (await snapshot()).zoom > zoomBefore);

  const originalVault = await page.evaluate(async () => (await window.businessmap.request('GET', '/api/workspace')).vault_path);
  const swap = async path => {
    await desktop.evaluate(({ dialog }, path) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] }); }, path);
    await page.getByRole('button', { name: '切换知识库', exact: true }).click();
  };
  await swap(secondVault);
  await page.waitForFunction(() => document.querySelector('.graph-view:not(.local) .graph-canvas')?._cyreg?.cy?.nodes().length === 2);
  await settle();
  check('vault switch uses only the new graph', (await snapshot()).nodes.every(n => ['第二知识库.md', '独立节点.md'].includes(n.id)));
  await swap(originalVault);
  await page.waitForFunction(() => document.querySelector('.graph-view:not(.local) .graph-canvas')?._cyreg?.cy?.nodes().length === 6);
  await settle();
  check('returning to the original vault restores its graph cache', JSON.stringify((await snapshot()).nodes) === JSON.stringify(after.nodes));
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1080, 700));
  await capture('minimum-dark');
  check('minimum window has no horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  check('no renderer errors', result.renderer_errors.length === 0);
} finally {
  await writeFile(resolve(artifacts, `lifecycle${packaged ? '-packaged' : ''}${fallback ? '-fallback' : ''}.json`), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await desktop.close();
}
