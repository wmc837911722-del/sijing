import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = resolve(root, 'artifacts', 'ux-onboarding');
await mkdir(artifacts, { recursive: true });
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: resolve(root, '.tmp', `onboarding-keyboard-${Date.now()}`), TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp') };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const prefix = packaged ? 'keyboard-packaged' : 'keyboard-source';
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true, timeout: 90_000 });
const result = { version: await desktop.evaluate(({ app }) => app.getVersion()), packaged: !!packaged, tested_at: new Date().toISOString(), checks: {}, geometry: {}, renderer_errors: [] };
let page;
const check = (name, passed) => { result.checks[name] = Boolean(passed); assert.ok(passed, name); };
const capture = async name => {
  await desktop.evaluate(async ({ BrowserWindow }) => { await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); });
  await page.waitForTimeout(200);
  const png = await desktop.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  await writeFile(resolve(artifacts, `${prefix}-${name}.png`), Buffer.from(png, 'base64'));
};
try {
  page = await desktop.firstWindow({ timeout: 90_000 });
  page.setDefaultTimeout(15_000);
  page.on('pageerror', error => result.renderer_errors.push(error.message));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await desktop.evaluate(({ BrowserWindow }) => { const window = BrowserWindow.getAllWindows()[0]; window.setSize(1080, 700); window.showInactive(); });
  const tour = page.getByRole('dialog', { name: '使用引导', exact: true });
  const body = tour.getByLabel('引导说明', { exact: true });
  const launch = page.getByRole('button', { name: '使用引导', exact: true });
  const steps = ['intro', 'knowledge', 'graph', 'models', 'repository', 'database', 'chat', 'mcp'];
  const geometry = () => tour.evaluate(dialog => {
    const rect = element => { const r = element.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom }; };
    const body = dialog.querySelector('.tour-body');
    const card = dialog.querySelector('.tour-card');
    const spot = dialog.querySelector('rect[data-tour-spotlight]');
    return { card: rect(card), spot: spot && rect(spot), header: rect(dialog.querySelector('.tour-topline')), controls: rect(dialog.querySelector('.tour-controls')), body: { ...rect(body), top: body.scrollTop, clientHeight: body.clientHeight, scrollHeight: body.scrollHeight }, innerWidth, innerHeight };
  });
  const ready = async step => {
    await page.locator(`dialog[data-tour-step="${step}"]`).waitFor();
    await tour.locator('rect[data-tour-spotlight]').waitFor();
    await page.waitForTimeout(150);
    await page.waitForFunction(() => {
      const card = document.querySelector('.tour-card')?.getBoundingClientRect();
      const controls = document.querySelector('.tour-controls')?.getBoundingClientRect();
      return card && controls && card.x >= 0 && card.y >= 0 && card.right <= innerWidth && card.bottom <= innerHeight && controls.bottom <= card.bottom;
    });
  };
  const focusBodyByTab = async step => {
    let reached = false, trapped = true;
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press('Tab');
      trapped &&= await tour.evaluate(dialog => dialog.contains(document.activeElement));
      if (await body.evaluate(element => element === document.activeElement)) { reached = true; break; }
    }
    check(`${step}: keyboard can reach scrollable explanation while staying in dialog`, reached && trapped);
  };
  const pauseAndCheckFocus = async (step, selector) => {
    await tour.getByRole('button', { name: '去操作', exact: true }).click();
    await tour.waitFor({ state: 'hidden' });
    await page.waitForFunction(selector => document.activeElement?.matches(selector), selector);
    check(`${step}: go operate puts keyboard focus on the real control`, true);
    await launch.click();
    await ready(step);
  };
  for (let index = 0; index < steps.length; index++) {
    const step = steps[index];
    await ready(step);
    const before = await geometry();
    result.geometry[step] = before;
    console.log(`Keyboard tour: ${step} (${Math.round(before.card.width)}px card, ${before.body.clientHeight}/${before.body.scrollHeight}px body)`);
    check(`${step}: new step starts at the top of its explanation`, before.body.top === 0);
    check(`${step}: fixed controls remain inside the card and viewport`, before.controls.y >= before.card.y && before.controls.bottom <= before.card.bottom && before.card.bottom <= before.innerHeight);
    await focusBodyByTab(step);
    if (before.body.scrollHeight > before.body.clientHeight + 4) {
      await page.keyboard.press('PageDown');
      await page.waitForFunction(() => document.querySelector('.tour-body').scrollTop > 0);
      const after = await geometry();
      check(`${step}: PageDown scrolls explanation without moving header or controls`, after.body.top > 0 && JSON.stringify(after.header) === JSON.stringify(before.header) && JSON.stringify(after.controls) === JSON.stringify(before.controls));
      await page.keyboard.press('End');
      await page.waitForFunction(() => { const body = document.querySelector('.tour-body'); return body.scrollTop + body.clientHeight >= body.scrollHeight - 2; });
      check(`${step}: End exposes the final explanation text`, true);
    }
    if (['models', 'repository', 'database', 'chat', 'mcp'].includes(step)) {
      const g = await geometry();
      const overlap = Math.max(0, Math.min(g.card.right, g.spot.right) - Math.max(g.card.x, g.spot.x)) * Math.max(0, Math.min(g.card.bottom, g.spot.bottom) - Math.max(g.card.y, g.spot.y));
      check(`${step}: explanation does not cover its highlighted operation area`, overlap < 1);
    }
    if (step === 'graph') await pauseAndCheckFocus(step, '.graph-canvas');
    if (step === 'models') await pauseAndCheckFocus(step, '[data-tour="model-add"]');
    if (step === 'database') await capture('database-scrolled');
    if (step === 'chat') {
      await capture('chat-small-light');
      await pauseAndCheckFocus(step, '#bm-chat-input');
      await page.keyboard.press('Escape');
      await tour.waitFor({ state: 'hidden' });
      check('Escape returns keyboard focus to tutorial launcher', await launch.evaluate(element => element === document.activeElement));
      await page.getByRole('button', { name: '深色主题', exact: true }).click();
      await launch.click();
      await ready(step);
      await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1500, 940));
      await ready(step);
      await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1080, 700));
      await ready(step);
      await capture('chat-small-dark');
      let trapped = true;
      for (let i = 0; i < 12; i++) { await page.keyboard.press('Tab'); trapped &&= await tour.evaluate(dialog => dialog.contains(document.activeElement)); }
      await page.keyboard.press('Shift+Tab');
      trapped &&= await tour.evaluate(dialog => dialog.contains(document.activeElement));
      check('dark resized dialog traps repeated Tab and Shift+Tab', trapped);
    }
    if (index < steps.length - 1) await tour.getByRole('button', { name: /^(开始引导|下一步)$/ }).click();
  }
  await capture('mcp-small-dark');
  await tour.getByRole('button', { name: '完成引导', exact: true }).click();
  await tour.waitFor({ state: 'hidden' });
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
  await desktop.close();
}
