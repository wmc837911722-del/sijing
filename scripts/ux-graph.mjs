import { dismissOnboarding } from './dismiss-onboarding.mjs';
import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const observe = process.argv.includes('--observe');
const count = Number(process.argv.find(value => value.startsWith('--nodes='))?.split('=')[1] || 300);
const profiling = process.argv.includes('--profile');
const mode = profiling ? 'profile' : observe ? 'baseline' : process.env.BUSINESSMAP_TEST_EXECUTABLE ? 'packaged' : 'verified';
const testDir = resolve(root, '.tmp', `ux-graph-${Date.now()}`);
const artifacts = resolve(root, 'artifacts', 'ux-graph');
await mkdir(resolve(testDir, 'vault'), { recursive: true });
await mkdir(artifacts, { recursive: true });
const title = index => `业务 ${String(index).padStart(4, '0')}`;
await Promise.all(Array.from({ length: count }, (_, index) => {
  const targets = [...new Set([(index + 1) % count, (index + 7) % count, Math.floor(index / 20) * 20])].filter(target => target !== index);
  return writeFile(resolve(testDir, 'vault', `${title(index)}.md`), `---\ntype: ${index % 5 === 0 ? 'overview' : 'business'}\ntags: [图谱验收]\n---\n# ${title(index)}\n\n真实 Markdown 图谱 fixture。\n\n${targets.map(target => `[[${title(target)}]]`).join('\n')}\n`);
}));
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: testDir, PYTHONUTF8: '1', TEMP: resolve(testDir, 'tmp'), TMP: resolve(testDir, 'tmp') };
await mkdir(env.TEMP, { recursive: true });
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true });
const results = { mode, nodes: count, version: await desktop.evaluate(({ app }) => app.getVersion()), packaged: !!packaged, tested_at: new Date().toISOString(), checks: {}, quality_checks: {}, quality_warnings: [], metrics: {}, renderer_errors: [] };
const page = await desktop.firstWindow();
page.setDefaultTimeout(15000);
page.on('pageerror', error => results.renderer_errors.push(error.message));
const record = (key, value) => { results.checks[key] = value; };
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const cameraDelta = (a, b) => ({ zoom: Math.abs(a.zoom - b.zoom), pan: distance(a.pan, b.pan) });
const stableCamera = (a, b) => Math.abs(a.zoom - b.zoom) < .005 && distance(a.pan, b.pan) < 3;
const graphSelector = '.graph-view:not(.local) .graph-canvas';
const state = () => page.locator(graphSelector).evaluate(element => {
  // Read the running renderer; never construct a synthetic graph or mutate positions.
  const cy = element._cyreg?.cy;
  if (cy) return { renderer: 'cytoscape', backend: element.dataset.renderer || 'canvas', layout_state: element.dataset.layoutState, zoom: cy.zoom(), pan: cy.pan(), width: cy.width(), height: cy.height(), nodes: cy.nodes().map(node => ({ id: node.id(), visible: node.visible(), x: node.position('x'), y: node.position('y'), sx: node.renderedPosition('x'), sy: node.renderedPosition('y'), neighbors: node.neighborhood('node').map(peer => peer.id()) })) };
  if (element.graphSnapshot) return element.graphSnapshot();
  throw new Error('Graph renderer snapshot is unavailable');
});
const settle = async () => {
  let previous = await state();
  let quiet = 0;
  for (let attempt = 0; attempt < 25 && quiet < 2; attempt++) {
    await page.waitForTimeout(200);
    const next = await state();
    const positions = new Map(next.nodes.map(node => [node.id, node]));
    const delta = Math.max(0, ...previous.nodes.filter((_, index) => index % Math.max(1, Math.floor(count / 16)) === 0).map(node => positions.has(node.id) ? distance(node, positions.get(node.id)) : Infinity));
    quiet = delta < .2 && (!next.layout_state || next.layout_state === 'settled') ? quiet + 1 : 0;
    previous = next;
  }
  return previous;
};
const capture = async name => {
  await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
  await page.waitForTimeout(150);
  const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  await writeFile(resolve(artifacts, `${mode}-${count}-${name}.png`), Buffer.from(png, 'base64'));
};
try {
  await dismissOnboarding(page);
  await desktop.evaluate(({ BrowserWindow }) => { const win = BrowserWindow.getAllWindows()[0]; win.setSize(1500, 940); win.showInactive(); });
  await page.getByRole('navigation', { name: '应用导航' }).getByRole('button', { name: '业务地图', exact: true }).click();
  await page.locator('.note-article').waitFor();
  await page.getByRole('button', { name: '切换右侧面板', exact: true }).click();
  await page.getByRole('button', { name: '切换知识目录', exact: true }).click();
  await page.evaluate(() => {
    window.__graphPerf = { tasks: [], frames: [], zoom: [], started: performance.now(), sampling: true };
    new PerformanceObserver(list => { window.__graphPerf.tasks.push(...list.getEntries().map(entry => ({ start: entry.startTime, duration: entry.duration }))); }).observe({ type: 'longtask', buffered: false });
    let last = performance.now();
    const tick = now => { if (!window.__graphPerf.sampling) return; window.__graphPerf.frames.push({ at: now, duration: now - last }); last = now; const element = document.querySelector('.graph-view:not(.local) .graph-canvas'); const cy = element?._cyreg?.cy; const snap = !cy && element?.graphSnapshot?.(); if (cy || snap) window.__graphPerf.zoom.push({ at: now, zoom: cy ? cy.zoom() : snap.zoom }); requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  });
  const profiler = profiling ? await page.context().newCDPSession(page) : null;
  if (profiler) { await profiler.send('Profiler.enable'); await profiler.send('Profiler.start'); }
  const openingStart = Date.now();
  await page.getByRole('button', { name: '关系图谱', exact: true }).click();
  await page.locator(graphSelector).waitFor();
  await page.waitForFunction(selector => { const el = document.querySelector(selector); return !!(el?._cyreg?.cy || el?.graphSnapshot); }, graphSelector);
  let initial = await state();
  results.graph_renderer = { library: initial.renderer, backend: initial.backend };
  results.renderer_asset = await page.evaluate(() => performance.getEntriesByType('resource').map(entry => entry.name).find(name => name.includes('/GraphView-')));
  assert.equal(initial.nodes.length, count, 'fixture must be indexed by the real backend');
  results.metrics.graph_instance_ready_ms = Date.now() - openingStart;
  await page.waitForTimeout(1200);
  results.metrics.opening_wall_ms = Date.now() - openingStart;
  const openingPerf = await page.evaluate(() => ({ ...window.__graphPerf, now: performance.now() }));
  results.metrics.opening_long_tasks_ms = openingPerf.tasks.map(task => task.duration);
  if (profiler) {
    const { profile } = await profiler.send('Profiler.stop');
    await writeFile(resolve(artifacts, `profile-${count}.cpuprofile`), JSON.stringify(profile));
    const times = new Map();
    profile.samples.forEach((id, index) => times.set(id, (times.get(id) || 0) + profile.timeDeltas[index] / 1000));
    results.metrics.profile_top_self_ms = profile.nodes.map(node => ({ function: node.callFrame.functionName, line: node.callFrame.lineNumber, column: node.callFrame.columnNumber, url: node.callFrame.url, ms: times.get(node.id) || 0 })).sort((a, b) => b.ms - a.ms).slice(0, 15);
    await profiler.detach();
  }
  record('cold graph creation avoids main-thread tasks over 500ms', !openingPerf.tasks.some(task => task.duration > 500));
  results.quality_checks['initial layout avoids tasks over 100ms'] = !openingPerf.tasks.some(task => task.duration > 100);
  if (!results.quality_checks['initial layout avoids tasks over 100ms']) results.quality_warnings.push(`Cold graph creation still has a ${Math.max(...results.metrics.opening_long_tasks_ms)}ms main-thread task; the stricter 100ms quality target is not met.`);
  await capture('opened');

  const box = await page.locator(graphSelector).boundingBox();
  let current = await settle();
  const chosen = [...current.nodes].filter(node => node.sx > 60 && node.sx < current.width - 170 && node.sy > 70 && node.sy < current.height - 150 && node.neighbors.length && node.neighbors.length < current.nodes.length - 1).sort((a, b) => Math.hypot(a.sx - current.width / 2, a.sy - current.height / 2) - Math.hypot(b.sx - current.width / 2, b.sy - current.height / 2))[0];
  assert.ok(chosen, 'a connected visible node must be available for real dragging');
  const neighborsBefore = current.nodes.filter(node => chosen.neighbors.includes(node.id));
  const dragProfiler = profiling ? await page.context().newCDPSession(page) : null;
  if (dragProfiler) { await dragProfiler.send('Profiler.enable'); await dragProfiler.send('Profiler.start'); }
  await page.mouse.move(box.x + chosen.sx, box.y + chosen.sy);
  await page.waitForTimeout(150);
  const hover = await page.locator(graphSelector).evaluate((element, id) => {
    const cy = element._cyreg?.cy;
    const node = cy.getElementById(id);
    const peerIds = new Set(node.closedNeighborhood().nodes().map(peer => peer.id()));
    const distant = cy.nodes().filter(peer => !peerIds.has(peer.id()));
    return { node_opacity: Number(node.style('opacity')), min_neighbor_opacity: Math.min(...node.neighborhood('node').map(peer => Number(peer.style('opacity')))), min_distant_opacity: Math.min(...distant.map(peer => Number(peer.style('opacity')))), classes: node.classes() };
  }, chosen.id);
  results.metrics.hover = hover;
  record('hover distinguishes connected from unrelated nodes', hover.min_distant_opacity < .5 && hover.min_neighbor_opacity > .5);
  if (!observe) {
    const label = await page.locator(graphSelector).evaluate((element, id) => {
      const canvas = element.querySelector('.graph-labels');
      if (!canvas) return { connected: false, painted_pixels: 0 };
      const cy = element._cyreg.cy;
      const node = cy.getElementById(id);
      const point = node.renderedPosition();
      const ratio = canvas.width / cy.width();
      const x = Math.max(0, Math.floor((point.x - 100) * ratio));
      const y = Math.max(0, Math.floor(point.y * ratio));
      const width = Math.min(Math.ceil(200 * ratio), canvas.width - x);
      const height = Math.min(Math.ceil(45 * ratio), canvas.height - y);
      const context = canvas.getContext('2d');
      const pixels = context.getImageData(x, y, width, height).data;
      let painted = 0;
      for (let i = 3; i < pixels.length; i += 4) if (pixels[i] > 10) painted++;
      return { connected: canvas.isConnected, painted_pixels: painted, font: context.font, width: canvas.width, height: canvas.height };
    }, chosen.id);
    results.metrics.hover.label_canvas = label;
    record('hovered node has a painted readable label', label.connected && label.painted_pixels > 10 && Number(label.font?.match(/([\d.]+)px/)?.[1]) >= 12);
  }
  if (!profiling) await capture('hover');
  await page.mouse.down();
  await page.mouse.move(box.x + chosen.sx + 120, box.y + chosen.sy + 55, { steps: 18 });
  await page.waitForTimeout(450);
  const dragged = await state();
  await page.mouse.up();
  const peerMotion = Math.max(...neighborsBefore.map(node => distance(node, dragged.nodes.find(peer => peer.id === node.id))));
  results.metrics.drag = { node: chosen.id, moved_world: distance(chosen, dragged.nodes.find(node => node.id === chosen.id)), neighbor_max_motion_world: peerMotion };
  record('connected neighbors react while dragging', peerMotion > 1);
  record('drag keeps graph open', await page.getByRole('heading', { name: '业务关系图谱', exact: true }).isVisible());
  if (dragProfiler) {
    await page.waitForTimeout(1200);
    const { profile } = await dragProfiler.send('Profiler.stop');
    await writeFile(resolve(artifacts, `profile-drag-${count}.cpuprofile`), JSON.stringify(profile));
    await dragProfiler.detach();
  } else {
  await page.mouse.move(box.x + box.width * .72, box.y + box.height * .42);
  const wheelBefore = await state();
  const anchor = { x: box.width * .72, y: box.height * .42 };
  const anchorWorld = { x: (anchor.x - wheelBefore.pan.x) / wheelBefore.zoom, y: (anchor.y - wheelBefore.pan.y) / wheelBefore.zoom };
  const zoomStart = await page.evaluate(() => performance.now());
  for (let index = 0; index < 12; index++) { await page.mouse.wheel(0, -35); await page.waitForTimeout(38); }
  await page.waitForTimeout(500);
  const zoomEnd = await page.evaluate(() => performance.now());
  const wheelPerf = await page.evaluate(({ start, end }) => ({ frames: window.__graphPerf.frames.filter(frame => frame.at >= start && frame.at <= end), zoom: window.__graphPerf.zoom.filter(frame => frame.at >= start && frame.at <= end) }), { start: zoomStart, end: zoomEnd });
  const sortedFrames = wheelPerf.frames.map(frame => frame.duration).sort((a, b) => a - b);
  const changes = wheelPerf.zoom.slice(1).map((frame, index) => Math.abs(Math.log(frame.zoom / wheelPerf.zoom[index].zoom))).filter(value => value > .00001);
  results.metrics.wheel = { frame_count: sortedFrames.length, frame_p95_ms: sortedFrames[Math.floor(sortedFrames.length * .95)] || null, frame_max_ms: sortedFrames.at(-1) || null, rendered_zoom_steps: changes.length, max_log_zoom_step: Math.max(0, ...changes), total_zoom_ratio: wheelPerf.zoom.at(-1)?.zoom / wheelPerf.zoom[0]?.zoom };
  const wheelAfter = await state();
  results.metrics.wheel.anchor_error_px = distance(anchor, { x: anchorWorld.x * wheelAfter.zoom + wheelAfter.pan.x, y: anchorWorld.y * wheelAfter.zoom + wheelAfter.pan.y });
  record('wheel zoom is interpolated between input events', changes.length > 12);
  record('wheel zoom stays anchored to the pointer', results.metrics.wheel.anchor_error_px < 2);
  record('wheel frames remain below 50ms at p95', results.metrics.wheel.frame_p95_ms < 50);
  await page.mouse.move(box.x + 10, box.y - 20);
  await page.waitForTimeout(50);
  record('hover highlight clears when pointer leaves a node', await page.locator(graphSelector).evaluate(element => !element._cyreg.cy.nodes('.is-hovered, .is-neighbor, .is-dimmed').length));

  // Select a genuinely empty part of the viewport, then pan using real mouse events.
  current = await state();
  const empty = [];
  for (let x = 50; x < current.width - 180; x += 60) for (let y = 50; y < current.height - 180; y += 60) { const clearance = Math.min(...current.nodes.map(node => Math.hypot(node.sx - x, node.sy - y))); empty.push({ x, y, clearance }); }
  const start = empty.sort((a, b) => b.clearance - a.clearance)[0];
  await page.mouse.move(box.x + start.x, box.y + start.y);
  await page.mouse.down();
  await page.mouse.move(box.x + start.x + 95, box.y + start.y + 65, { steps: 12 });
  await page.mouse.up();
  const panned = await state();
  record('empty-space drag pans the viewport', distance(current.pan, panned.pan) > 50);
  const beforeFilter = await settle();
  const filter = page.getByRole('textbox', { name: '筛选图谱节点', exact: true });
  await filter.fill('业务 000');
  await page.waitForTimeout(350);
  record('filter displays only matching nodes', (await state()).nodes.filter(node => node.visible).length === Math.min(count, 10));
  await filter.fill('');
  await page.waitForTimeout(400);
  const afterFilter = await state();
  results.metrics.filter_camera_delta = cameraDelta(beforeFilter, afterFilter);
  record('filter clear preserves the viewport', stableCamera(beforeFilter, afterFilter));
  await page.getByRole('button', { name: '放大图谱', exact: true }).click();
  await page.waitForTimeout(400);
  const beforeTab = await settle();
  await page.getByRole('button', { name: '文档', exact: true }).click();
  await page.locator('.note-article').waitFor();
  await page.getByRole('button', { name: '关系图谱', exact: true }).click();
  await page.waitForTimeout(450);
  const afterTab = await state();
  results.metrics.tab_camera_delta = cameraDelta(beforeTab, afterTab);
  record('returning from a note preserves the viewport', stableCamera(beforeTab, afterTab));
  const nodeDelta = Math.max(...beforeTab.nodes.map(node => distance(node, afterTab.nodes.find(peer => peer.id === node.id))));
  results.metrics.tab_max_node_movement_world = nodeDelta;
  record('returning from a note does not relayout all nodes', nodeDelta < 5);

  // Move well beyond fit/minZoom first, otherwise a forced fit can look unchanged.
  await page.getByRole('button', { name: '放大图谱', exact: true }).click({ clickCount: 6, delay: 60 });
  await page.waitForTimeout(450);
  const beforeResize = await state();
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1300, 850));
  await page.waitForTimeout(450);
  const afterResize = await state();
  results.metrics.resize_zoom_delta = Math.abs(beforeResize.zoom - afterResize.zoom);
  record('window resize preserves zoom', Math.abs(beforeResize.zoom - afterResize.zoom) < .005);
  await capture('interacted');
  if (!observe) {
    await page.locator(graphSelector).focus();
    const beforeKeyboard = await state();
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(200);
    record('focused graph supports keyboard panning', distance(beforeKeyboard.pan, (await state()).pan) > 10);
    await page.getByRole('button', { name: '适应图谱大小', exact: true }).click();
    await page.waitForTimeout(450);
    const fitted = await state();
    record('fit makes every node visible in the viewport', fitted.nodes.every(node => node.sx >= 0 && node.sy >= 0 && node.sx <= fitted.width && node.sy <= fitted.height));
  }
  await page.getByRole('button', { name: '深色主题', exact: true }).click();
  await page.waitForTimeout(250);
  await capture('dark');
  }
  record('renderer has no uncaught errors', results.renderer_errors.length === 0);
  await page.evaluate(() => { window.__graphPerf.sampling = false; });
} catch (error) {
  results.error = error.stack || String(error);
  await capture('failure').catch(() => {});
  throw error;
} finally {
  await writeFile(resolve(artifacts, `${mode}-${count}.json`), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
  await desktop.close();
}
if (!observe && !profiling) assert.ok(Object.values(results.checks).every(Boolean), 'Graph interaction regression failed; see artifacts/ux-graph/*.json');
