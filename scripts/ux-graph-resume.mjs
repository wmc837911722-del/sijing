import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const testDir = resolve(root, '.tmp', `graph-resume-${Date.now()}`);
const artifacts = resolve(root, 'artifacts', 'ux-graph');
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const title = index => `恢复业务 ${String(index).padStart(4, '0')}`;
await mkdir(resolve(testDir, 'vault'), { recursive: true });
await mkdir(resolve(testDir, 'tmp'), { recursive: true });
await mkdir(artifacts, { recursive: true });
await Promise.all(Array.from({ length: 300 }, (_, index) => {
  const targets = [...new Set([(index + 1) % 300, (index + 7) % 300, Math.floor(index / 20) * 20])].filter(target => target !== index);
  return writeFile(resolve(testDir, 'vault', `${title(index)}.md`), `---\ntype: business\n---\n# ${title(index)}\n\n真实 Markdown 生命周期验收。\n\n${targets.map(target => `[[${title(target)}]]`).join('\n')}\n`);
}));
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, PYTHONUTF8: '1', TEMP: resolve(testDir, 'tmp'), TMP: resolve(testDir, 'tmp') };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true });
const page = await desktop.firstWindow();
page.setDefaultTimeout(15000);
const selector = '.graph-view:not(.local) .graph-canvas';
const graph = page.locator(selector);
const result = { version: await desktop.evaluate(({ app }) => app.getVersion()), packaged: !!packaged, tested_at: new Date().toISOString(), fixture_nodes: 300, checks: {}, observations: {}, limitations: [], renderer_errors: [] };
page.on('pageerror', error => result.renderer_errors.push(error.message));
const check = (name, passed) => { result.checks[name] = passed; assert.ok(passed, name); };
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const snapshot = () => graph.evaluate(element => {
  const cy = element._cyreg.cy;
  return {
    state: element.dataset.layoutState, zoom: cy.zoom(), pan: { ...cy.pan() }, width: cy.width(), height: cy.height(),
    nodes: cy.nodes().map(node => ({ id: node.id(), ...node.position(), sx: node.renderedPosition('x'), sy: node.renderedPosition('y'), grabbed: node.grabbed(), neighbors: node.neighborhood('node').map(peer => peer.id()) })),
  };
});
const movement = (before, after) => Math.max(0, ...before.nodes.map(node => {
  const next = after.nodes.find(candidate => candidate.id === node.id);
  return next ? distance(node, next) : Infinity;
}));
const settle = async () => {
  await page.waitForFunction(selector => document.querySelector(selector)?.dataset.layoutState === 'settled', selector, { timeout: 15000 });
  return snapshot();
};
const openGraph = async () => {
  await page.getByRole('button', { name: '关系图谱', exact: true }).click();
  await page.waitForFunction(selector => document.querySelector(selector)?._cyreg?.cy?.nodes().length === 300, selector);
};
const openDocument = async () => {
  await page.getByRole('button', { name: '文档', exact: true }).click();
  await page.locator('.note-article').waitFor();
};
const trace = () => page.evaluate(() => structuredClone(window.__graphResumeTrace));
const visible = async expected => page.waitForFunction(expected => document.hidden !== expected, expected);

try {
  await dismissOnboarding(page);
  await desktop.evaluate(({ BrowserWindow }) => { const win = BrowserWindow.getAllWindows()[0]; win.setSize(1500, 940); win.showInactive(); });
  await visible(true);
  // Observe the real Worker messages without delaying, replacing or altering them.
  // This distinguishes a paused worker from a merely suspended renderer RAF.
  await page.evaluate(() => {
    const original = Worker.prototype.postMessage;
    const known = new WeakMap();
    const trace = window.__graphResumeTrace = { serial: 0, sent: [], received: [] };
    Worker.prototype.postMessage = function(message, ...args) {
      if (message?.type === 'set-graph' && Array.isArray(message.nodes) && !known.has(this)) {
        const id = ++trace.serial;
        known.set(this, id);
        this.addEventListener('message', ({ data }) => {
          if (data?.type === 'positions') trace.received.push({ id, generation: data.generation, revision: data.revision, settled: data.settled, count: data.ids.length });
        });
      }
      if (known.has(this)) trace.sent.push({ id: known.get(this), type: message.type, generation: message.generation, revision: message.revision, phase: message.phase, count: message.nodes?.length, paused: message.paused });
      return original.call(this, message, ...args);
    };
  });
  await page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '业务地图', exact: true }).click();
  await page.locator('.note-article').waitFor();
  await page.getByRole('button', { name: '切换右侧面板', exact: true }).click();
  await page.getByRole('button', { name: '切换知识目录', exact: true }).click();

  await openGraph();
  const incomplete = await snapshot();
  check('early navigation actually interrupts an unfinished 300-node layout', incomplete.state === 'running');
  await openDocument();
  await openGraph();
  const resumed = await snapshot();
  check('unfinished cached layout resumes rather than being marked settled', resumed.state === 'running');
  const completed = await settle();
  result.observations.unfinished_cache_node_movement = movement(resumed, completed);
  check('unfinished cached layout continues calculating and eventually settles', completed.state === 'settled' && result.observations.unfinished_cache_node_movement > 1);

  await openDocument();
  await openGraph();
  const cached = await snapshot();
  result.observations.cached_zoom = cached.zoom;
  check('settled cache returns without changing world coordinates', cached.state === 'settled' && movement(completed, cached) < .001);
  const stableStart = await trace();
  await graph.evaluate(element => { element.style.display = 'none'; });
  await page.waitForFunction(sentCount => window.__graphResumeTrace.sent.slice(sentCount).some(message => message.type === 'pause'), stableStart.sent.length);
  await graph.evaluate(element => { element.style.removeProperty('display'); });
  await page.waitForTimeout(350);
  const stableEnd = await trace();
  const afterRestore = await snapshot();
  const undesiredResumes = stableEnd.sent.slice(stableStart.sent.length).filter(message => message.type === 'resume');
  result.observations.cached_restore = { resumes: undesiredResumes.length, max_node_movement: movement(cached, afterRestore) };
  check('restoring a zero-size canvas does not restart a settled cached layout', undesiredResumes.length === 0 && afterRestore.state === 'settled' && movement(cached, afterRestore) < .001);

  const box = await graph.boundingBox();
  const candidates = cached.nodes.filter(node => node.neighbors.length && node.sx > 70 && node.sx < cached.width - 180 && node.sy > 60 && node.sy < cached.height - 150);
  candidates.sort((a, b) => Math.hypot(a.sx - cached.width / 2, a.sy - cached.height / 2) - Math.hypot(b.sx - cached.width / 2, b.sy - cached.height / 2));
  assert.ok(candidates.length, 'fixture needs a visible connected node');
  const target = candidates[0];
  await page.mouse.move(box.x + target.sx, box.y + target.sy);
  await page.waitForTimeout(120);
  const labels = await graph.evaluate(element => {
    const canvas = element.querySelector('.graph-labels');
    if (!canvas) return { connected: false, nonzeroAlpha: 0 };
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let nonzeroAlpha = 0;
    for (let index = 3; index < pixels.length; index += 4) if (pixels[index]) nonzeroAlpha++;
    return { connected: canvas.isConnected, width: canvas.width, height: canvas.height, nonzeroAlpha, hovered: element._cyreg.cy.nodes('.is-hovered').map(node => node.id()) };
  });
  result.observations.hover_labels = labels;
  check('hover labels are drawn into a connected visible canvas', labels.connected && labels.nonzeroAlpha > 0 && labels.hovered?.length === 1);
  await graph.evaluate((element, id) => {
    const cy = element._cyreg.cy;
    const node = cy.getElementById(id);
    const peers = node.neighborhood('node');
    let previous = peers.map(peer => ({ id: peer.id(), ...peer.renderedPosition() }));
    const trace = window.__graphDragFrames = { frames: [], stopped: false };
    const started = performance.now();
    const sample = now => {
      if (trace.stopped) return;
      const next = peers.map(peer => ({ id: peer.id(), ...peer.renderedPosition() }));
      if (node.grabbed()) trace.frames.push({ elapsedMs: now - started, maxNeighborStepPx: Math.max(...next.map((position, index) => Math.hypot(position.x - previous[index].x, position.y - previous[index].y))) });
      previous = next;
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, target.id);
  await page.mouse.down();
  const grabbed = await snapshot();
  const active = grabbed.nodes.find(node => node.grabbed);
  assert.ok(active?.neighbors.length, 'real pointerdown must grab a connected node');
  const samples = [];
  let previous = grabbed;
  for (let index = 1; index <= 8; index++) {
    await page.mouse.move(box.x + target.sx + index * 13, box.y + target.sy + index * 5, { steps: 2 });
    await page.waitForTimeout(35);
    const next = await snapshot();
    const peerMovement = Math.max(...active.neighbors.map(id => distance(previous.nodes.find(node => node.id === id), next.nodes.find(node => node.id === id))));
    samples.push({ grabbed: next.nodes.find(node => node.id === active.id).grabbed, peerMovement, peerMovementPx: peerMovement * next.zoom });
    previous = next;
  }
  result.observations.grabbed_samples = samples;
  check('neighbors change in multiple frames before pointerup', samples.every(sample => sample.grabbed) && samples.filter(sample => sample.peerMovement > .1).length >= 3);
  result.observations.grabbed_frames = await page.evaluate(() => { window.__graphDragFrames.stopped = true; return window.__graphDragFrames.frames; });
  result.observations.max_neighbor_frame_step_px = Math.max(0, ...result.observations.grabbed_frames.map(frame => frame.maxNeighborStepPx));
  result.observations.max_grabbed_frame_gap_ms = Math.max(0, ...result.observations.grabbed_frames.slice(1).map((frame, index) => frame.elapsedMs - result.observations.grabbed_frames[index].elapsedMs));
  check('drag samples were rendered frames rather than background-throttled seconds', result.observations.max_grabbed_frame_gap_ms < 150);
  check('cached first drag avoids neighbor jumps over 40 screen pixels per frame', result.observations.grabbed_frames.length >= 3 && result.observations.max_neighbor_frame_step_px < 40);
  await page.mouse.up();
  check('real drag leaves the graph open', await graph.isVisible());
  const beforeHide = await snapshot();
  check('hide test begins during an active layout', beforeHide.state === 'running');
  const runningTrace = await trace();
  await graph.evaluate(element => { element.style.display = 'none'; });
  await page.waitForFunction(sentCount => window.__graphResumeTrace.sent.slice(sentCount).some(message => message.type === 'pause'), runningTrace.sent.length);
  await page.waitForTimeout(80);
  const pausedStart = await trace();
  await page.waitForTimeout(180);
  const pausedEnd = await trace();
  check('zero-size canvas pauses worker output instead of only hiding rendering', pausedStart.received.length === pausedEnd.received.length);
  await graph.evaluate(element => { element.style.removeProperty('display'); });
  const afterShow = await settle();
  const finalTrace = await trace();
  const resumes = finalTrace.sent.slice(pausedEnd.sent.length).filter(message => message.type === 'resume');
  result.observations.running_restore = { resumes: resumes.length, max_node_movement: movement(beforeHide, afterShow) };
  check('restoring a running canvas resumes the worker and reaches settled', resumes.length > 0 && afterShow.state === 'settled' && movement(beforeHide, afterShow) > .1);
  result.renderer_asset = await page.evaluate(() => performance.getEntriesByType('resource').map(entry => entry.name).find(name => name.includes('/GraphView-')));
  check('renderer has no uncaught errors', result.renderer_errors.length === 0);
  // Keep this probe LAST: minimizing can throttle automation RAF even after
  // restore, which would invalidate screen-step measurements above.
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].minimize());
  await page.waitForTimeout(180);
  result.observations.native_minimize = {
    ...await desktop.evaluate(({ BrowserWindow }) => { const win = BrowserWindow.getAllWindows()[0]; return { minimized: win.isMinimized(), visible: win.isVisible() }; }),
    documentHidden: await page.evaluate(() => document.hidden),
  };
  if (!result.observations.native_minimize.documentHidden) result.limitations.push('Windows native minimize succeeded, but this automated Electron session kept document.hidden=false. Native visibility pause/resume is not claimed; the real ResizeObserver visibility path was tested instead.');
} catch (error) {
  result.error = error.stack || String(error);
  process.exitCode = 1;
} finally {
  await page.mouse.up().catch(() => {});
  result.worker_trace = await trace().catch(() => null);
  await writeFile(resolve(artifacts, `resume${packaged ? '-packaged' : ''}.json`), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ...result, worker_trace: result.worker_trace ? { sent: result.worker_trace.sent.length, received: result.worker_trace.received.length } : null }, null, 2));
  await desktop.close();
}
