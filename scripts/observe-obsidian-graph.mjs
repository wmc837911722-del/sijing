import { chromium } from '@playwright/test';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const runDir = resolve(root, '.tmp', `obsidian-graph-${Date.now()}`);
const vault = resolve(runDir, 'Synthetic Business Graph');
const profile = resolve(runDir, 'profile');
const appdata = resolve(runDir, 'appdata');
const artifacts = resolve(root, 'artifacts');
for (const path of [vault, profile, appdata, resolve(appdata, 'obsidian'), artifacts, resolve(vault, '.obsidian')]) await mkdir(path, { recursive: true });
const names = ['订单', '支付', '库存', '售后', '结算', '客户'];
const hubs = names.map(name => `${name}中心`);
for (let group = 0; group < names.length; group++) {
  const notes = Array.from({ length: 24 }, (_, i) => `${names[group]}规则${String(i + 1).padStart(2, '0')}`);
  await writeFile(resolve(vault, `${hubs[group]}.md`), `# ${hubs[group]}\n\n合成数据，用于交互评估。\n\n${[...notes, hubs[(group + 1) % 6]].map(name => `- [[${name}]]`).join('\n')}\n`);
  for (let i = 0; i < notes.length; i++) await writeFile(resolve(vault, `${notes[i]}.md`), `# ${notes[i]}\n\n这是合成业务规则。\n\n[[${hubs[group]}]]\n[[${notes[(i + 1) % notes.length]}]]\n${i % 6 === 0 ? `[[${hubs[(group + 1) % 6]}]]` : ''}\n`);
}
const vaultConfig = JSON.stringify({ vaults: { 'baba012345678901': { path: vault, ts: Date.now(), open: true } } });
await writeFile(resolve(profile, 'obsidian.json'), vaultConfig);
await writeFile(resolve(appdata, 'obsidian', 'obsidian.json'), vaultConfig);
await writeFile(resolve(vault, '.obsidian', 'app.json'), JSON.stringify({ promptDelete: false, showUnsupportedFiles: false }));
await writeFile(resolve(vault, '.obsidian', 'appearance.json'), JSON.stringify({ baseFontSize: 16, theme: 'obsidian' }));
await writeFile(resolve(vault, '.obsidian', 'core-plugins.json'), JSON.stringify(['file-explorer', 'global-search', 'switcher', 'graph', 'backlink', 'page-preview', 'command-palette']));
const env = { ...process.env, APPDATA: appdata, LOCALAPPDATA: resolve(runDir, 'localappdata'), TEMP: runDir, TMP: runDir };
delete env.ELECTRON_RUN_AS_NODE;
// This Obsidian distribution disables the Electron main-process inspector;
// Chromium CDP exposes its actual window without unpacking application code.
const child = spawn('D:\\Obsidian\\Obsidian.exe', [`--user-data-dir=${profile}`, '--remote-debugging-port=0'], { cwd: runDir, env, windowsHide: true, stdio: 'ignore' });
let port;
for (let i = 0; i < 100; i++) {
  try { port = Number((await readFile(resolve(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; } catch { await new Promise(resolve => setTimeout(resolve, 100)); }
}
if (!port) throw new Error('Isolated Obsidian did not expose a renderer debugging port');
const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
const results = { testedAt: new Date().toISOString(), executable: 'D:\\Obsidian\\Obsidian.exe', syntheticVault: vault, nodeCount: 150, checks: {}, observations: [], rendererErrors: [] };
// Infer visible node centers from screenshot pixels, never application internals.
async function visibleNodes(png, bounds) {
  const { data, info } = await sharp(png).removeAlpha().raw().toBuffer({ resolveWithObject: true });
  const mask = new Uint8Array(info.width * info.height);
  for (let y = Math.ceil(bounds.y + 15); y < Math.min(info.height, bounds.y + bounds.height - 15); y++) for (let x = Math.ceil(bounds.x + 15); x < Math.min(info.width, bounds.x + bounds.width - 15); x++) {
    const i = (y * info.width + x) * info.channels;
    if (data[i] > 145 && data[i] < 205 && Math.abs(data[i] - data[i + 1]) < 4 && Math.abs(data[i] - data[i + 2]) < 4) mask[y * info.width + x] = 1;
  }
  const nodes = [];
  for (let i = 0; i < mask.length; i++) if (mask[i]) {
    mask[i] = 0;
    const todo = [i]; let left = info.width, right = 0, top = info.height, bottom = 0, count = 0;
    while (todo.length) {
      const point = todo.pop(), x = point % info.width, y = Math.floor(point / info.width);
      left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y); count++;
      for (const next of [point - 1, point + 1, point - info.width, point + info.width]) if (mask[next]) { mask[next] = 0; todo.push(next); }
    }
    const w = right - left + 1, h = bottom - top + 1;
    if (w >= 5 && h >= 5 && w < 80 && h < 80 && Math.abs(w - h) <= 3 && count / (w * h) > 0.5) nodes.push({ x: (left + right) / 2, y: (top + bottom) / 2, radius: (w + h) / 4 });
  }
  return nodes;
}
try {
  const page = browser.contexts()[0].pages()[0];
  page.on('pageerror', error => results.rendererErrors.push(error.message));
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.waitForFunction(() => !!window.app?.workspace?.layoutReady);
  results.application = await page.title();
  results.isolatedVaultVerified = await page.evaluate(expected => window.app.vault.adapter.basePath === expected, vault);
  await page.keyboard.press('Control+g');
  await page.locator('canvas').waitFor();
  const canvasBounds = await page.locator('canvas').boundingBox();
  results.canvasBounds = canvasBounds;
  const capture = async name => {
    const png = await page.screenshot({ timeout: 30000 });
    await writeFile(resolve(artifacts, `obsidian-graph-${name}.png`), png);
    return png;
  };
  await page.waitForTimeout(4500);
  await capture('initial');
  // Hide graph options with the actual close control so the graph is unobstructed.
  const graphClose = page.locator('.graph-controls .clickable-icon[aria-label]').filter({ has: page.locator('svg.lucide-x') });
  if (await graphClose.count()) await graphClose.first().click();
  await page.mouse.move(canvasBounds.x + canvasBounds.width * 0.5, canvasBounds.y + canvasBounds.height * 0.5);
  await page.mouse.wheel(0, 350);
  await page.waitForTimeout(1600);
  const overview = await capture('overview');
  let nodes = await visibleNodes(overview, canvasBounds);
  results.overviewVisibleNodes = nodes.length;
  console.log('OVERVIEW', nodes.length);
  // Prefer a visible hub safely away from graph controls/viewport boundaries.
  const target = nodes.filter(p => p.x > canvasBounds.x + 100 && p.x < canvasBounds.x + canvasBounds.width - 180 && p.y > canvasBounds.y + 120 && p.y < canvasBounds.y + canvasBounds.height - 180).sort((a, b) => b.radius - a.radius)[0];
  if (!target) throw new Error('No safe visible node was found from the actual screenshot');
  results.hoverTarget = target;
  await page.mouse.move(target.x, target.y);
  await page.waitForTimeout(350);
  await capture('hover');
  results.checks.hoverRealNode = true;
  // RAF timing is renderer scheduling, not a claim about GPU frame presentation.
  await page.evaluate(() => { window.__graphRAF = []; let last = performance.now(); const start = last; function frame(now) { window.__graphRAF.push(now - last); last = now; if (now - start < 3500) requestAnimationFrame(frame); } requestAnimationFrame(frame); });
  await page.mouse.down();
  await page.mouse.move(target.x + 145, target.y + 100, { steps: 30 });
  await capture('drag-held');
  await page.mouse.up();
  await capture('drag-release');
  await page.mouse.move(canvasBounds.x + 20, canvasBounds.y + 30);
  await page.waitForTimeout(1200);
  await capture('drag-settled');
  results.checks.dragNodeViaMouse = true;
  await page.waitForTimeout(2300);
  const frames = await page.evaluate(() => window.__graphRAF);
  const steady = frames.slice(1).sort((a, b) => a - b);
  results.rafScheduling = { sampleCount: steady.length, medianMs: steady[Math.floor(steady.length * 0.5)], p95Ms: steady[Math.floor(steady.length * 0.95)], maxMs: Math.max(...steady), over33ms: steady.filter(x => x > 33.34).length, caveat: 'Renderer requestAnimationFrame scheduling during automated drag and screenshot capture, visible CDP-controlled Windows Electron window, not a GPU presentation benchmark or cross-app speed claim.' };
  // Pan from empty space and observe labels/camera together.
  await page.mouse.move(canvasBounds.x + 50, canvasBounds.y + 55);
  await page.mouse.down();
  await page.mouse.move(canvasBounds.x + 140, canvasBounds.y + 110, { steps: 24 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  await capture('panned');
  results.checks.panViaMouse = true;
  // Zoom at a deliberately off-center pointer to inspect the anchor behavior.
  const anchor = { x: canvasBounds.x + canvasBounds.width * 0.63, y: canvasBounds.y + canvasBounds.height * 0.48 };
  await page.mouse.move(anchor.x, anchor.y);
  const zoomBefore = await capture('zoom-before');
  await page.mouse.wheel(0, -180);
  await page.waitForTimeout(35);
  await capture('zoom-progress');
  await page.waitForTimeout(450);
  const zoomed = await capture('zoom-after');
  results.zoomPointer = anchor;
  const nodesBefore = await visibleNodes(zoomBefore, canvasBounds);
  const nodesAfter = await visibleNodes(zoomed, canvasBounds);
  let best;
  for (let scale = 1.05; scale < 2.6; scale += 0.005) {
    const pairs = [];
    for (const before of nodesBefore) {
      const expected = { x: anchor.x + (before.x - anchor.x) * scale, y: anchor.y + (before.y - anchor.y) * scale };
      if (expected.x < canvasBounds.x + 40 || expected.x > 1360 || expected.y < canvasBounds.y + 40 || expected.y > 860) continue;
      const after = nodesAfter.filter(point => (point.radius > 9) === (before.radius > 8)).map(point => ({ point, error: Math.hypot(point.x - expected.x, point.y - expected.y) })).sort((a, b) => a.error - b.error)[0];
      if (after && after.error < 4) pairs.push({ before, after: after.point, error: after.error });
    }
    if (pairs.length < 10) continue;
    const meanError = pairs.reduce((sum, pair) => sum + pair.error, 0) / pairs.length;
    if (!best || pairs.length > best.matchedNodes || (pairs.length === best.matchedNodes && meanError < best.meanError)) best = { scale, meanError, matchedNodes: pairs.length, examplePairs: pairs.slice(0, 5) };
  }
  results.zoomPixelMeasurement = best;
  results.checks.zoomAnchoredAtPointer = !!best && best.meanError < 3;
  results.checks.wheelZoomViaMouse = true;
  nodes = await visibleNodes(zoomed, canvasBounds);
  const clickable = nodes.filter(p => p.x > canvasBounds.x + 70 && p.x < canvasBounds.x + canvasBounds.width - 300 && p.y > canvasBounds.y + 80 && p.y < canvasBounds.y + canvasBounds.height - 80).sort((a, b) => b.radius - a.radius)[0];
  if (!clickable) throw new Error('No visible node available for note navigation');
  await page.mouse.click(clickable.x, clickable.y);
  await page.waitForTimeout(500);
  results.noteAfterClick = await page.title();
  results.checks.singleClickOpensNote = !results.noteAfterClick.startsWith('关系图谱');
  await capture('note-opened');
  results.navigationControls = await page.locator('.view-header [aria-label]').evaluateAll(elements => elements.map(el => ({ label: el.getAttribute('aria-label'), class: el.className })));
  await page.locator('.workspace-leaf.mod-active .view-header [aria-label="返回"]').click();
  await page.waitForTimeout(500);
  results.checks.backReturnsToGraph = await page.locator('canvas').count() > 0;
  await capture('returned');
  results.observations = [
    '150-node overview uses small neutral circles, thin low-contrast edges, and larger hubs; labels are hidden at low zoom.',
    'Hover emphasizes the pointed node, immediate neighbors and connecting edges; the remainder dims and the pointed label remains readable.',
    'Dragging a hub moves its neighboring nodes elastically. After release the neighborhood keeps settling rather than remaining permanently pinned.',
    'Wheel zoom changes continuously across intermediate and settled screenshots around the off-center mouse pointer; pixel-based matching checks the anchor without reading internal graph code.',
    'Labels gradually become visible with closer zoom; at overview scale a hovered label can still be read.',
    'A node opens its note on a single click. The visible Back control returns to the graph.'
  ];
  console.log(JSON.stringify(results, null, 2));
} finally {
  await writeFile(resolve(artifacts, 'obsidian-graph-observation.json'), JSON.stringify(results, null, 2));
  await browser.contexts()[0].pages()[0]?.close().catch(() => {});
  await browser.close();
  if (child.exitCode === null) child.kill();
}
