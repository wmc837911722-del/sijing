import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const out = resolve(root, 'artifacts', 'ux-tour-review');
await mkdir(out, { recursive: true });
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const label = process.env.BUSINESSMAP_TEST_LABEL || (packaged ? 'packaged' : 'fixed');
const zoomSetting = process.env.BUSINESSMAP_TEST_ZOOM_SET?.trim();
const zooms = zoomSetting ? zoomSetting.split(',').map(Number) : null;
if (zooms?.some(zoom => !Number.isFinite(zoom) || zoom <= 0)) throw new Error('BUSINESSMAP_TEST_ZOOM_SET must contain positive zoom factors.');
const sizes = zooms ? zooms.map(zoom => ({width:1080,zoom,name:`1080-zoom${Math.round(zoom*100)}`})) : [1280,1080].map(width => ({width,zoom:1,name:String(width)}));
const env = { ...process.env, BUSINESSMAP_HEADLESS: '1', BUSINESSMAP_DATA_DIR: resolve(root, '.tmp', `tour-review-${Date.now()}`), PYTHONUTF8: '1', TEMP: resolve(root, '.tmp'), TMP: resolve(root, '.tmp') };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const desktop = await electron.launch({ executablePath: packaged || electronBinary, args: packaged ? [] : [root], cwd: root, env, chromiumSandbox: true, timeout: 90_000 });
const result = { version: await desktop.evaluate(({app}) => app.getVersion()), platform: process.platform, arch: process.arch, packaged: !!packaged, geometry: [], focus: [], errors: [], mutations: [] };
const saveShortcut = process.platform === 'darwin' ? 'Meta+s' : 'Control+s';
const anchors = { intro: '.navigation-items', knowledge: '.file-tree, .map-main', graph: '.graph-view:not(.local) .graph-canvas', models: '[data-tour="model-add"]', repository: '[data-tour="repository-add"]', database: '[data-tour="database-source"]', chat: '.bm-chat-composer', mcp: '[data-tour="mcp-copy"]' };
let page;
const capture = async name => {
  await desktop.evaluate(async ({BrowserWindow}) => BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, {stayHidden:true, stayAwake:true}));
  await page.waitForTimeout(150);
  const base64 = await desktop.evaluate(async ({BrowserWindow}) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, {stayHidden:true, stayAwake:true})).toPNG().toString('base64'));
  await writeFile(resolve(out, `${label}-${name}.png`), Buffer.from(base64,'base64'));
};
try {
  await desktop.evaluate(() => {
    globalThis.__tourRequests = [];
    const original = globalThis.fetch;
    globalThis.fetch = (url, init) => { globalThis.__tourRequests.push({method:init?.method || 'GET',path:new URL(String(url)).pathname}); return original(url, init); };
  });
  page = await desktop.firstWindow({timeout:90_000});
  page.setDefaultTimeout(15_000);
  page.on('pageerror', e => result.errors.push(e.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  const tour = page.getByRole('dialog',{name:'使用引导',exact:true});
  const launch = page.getByRole('button',{name:'使用引导',exact:true});
  await tour.waitFor();
  for (const {width,zoom,name} of sizes) {
    await desktop.evaluate(({BrowserWindow}, width) => BrowserWindow.getAllWindows()[0].setSize(width,700),width);
    await desktop.evaluate(({BrowserWindow}, zoom) => BrowserWindow.getAllWindows()[0].webContents.setZoomFactor(zoom),zoom);
    if (!await tour.isVisible()) await launch.click();
    if (await tour.getAttribute('data-tour-step') !== 'intro') await tour.getByRole('button',{name:'从头开始',exact:true}).click();
    for (const step of Object.keys(anchors)) {
      await page.locator(`dialog[data-tour-step="${step}"]`).waitFor();
      await page.waitForFunction(selector => {
        const target=[...document.querySelectorAll(selector)].find(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height);
        const spot=document.querySelector('rect[data-tour-spotlight]');
        if(!target || !spot) return false;
        const t=target.getBoundingClientRect(), s=spot.getBoundingClientRect();
        return Math.abs(s.left-Math.max(8,t.left-6))<2 && Math.abs(s.top-Math.max(48,t.top-6))<2;
      },anchors[step]);
      await page.waitForTimeout(150);
      // Native capture gives the hidden Electron window a real presentation;
      // read layout only after the associated RAF positioning has completed.
      await capture(`${name}-${step}`);
      const geometry = await page.evaluate(({selector,width,step,zoom}) => {
        const rect = e => e?.getBoundingClientRect().toJSON();
        const target = [...document.querySelectorAll(selector)].find(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height);
        const t = target?.getBoundingClientRect(), c=document.querySelector('.tour-card').getBoundingClientRect();
        const intersection=t ? Math.max(0, Math.min(t.right,c.right)-Math.max(t.left,c.left))*Math.max(0,Math.min(t.bottom,c.bottom)-Math.max(t.top,c.top)) : 0;
        const card=document.querySelector('.tour-card');
        const visible=e=>{const r=e.getBoundingClientRect(); return r.bottom<=Math.min(c.bottom,innerHeight)+1 && r.top>=Math.max(c.top,0)-1;};
        return {width,zoom,step,viewport:{width:innerWidth,height:innerHeight},target:rect(target),card:c.toJSON(),intersection,occlusion_ratio:t?intersection/(t.width*t.height):0,card_scroll:{top:card.scrollTop,height:card.clientHeight,scrollHeight:card.scrollHeight},body_scroll_top:card.querySelector('.tour-body')?.scrollTop,title_visible:visible(card.querySelector('h2')),controls:[...card.querySelectorAll('button')].map(e=>({text:e.textContent,rect:rect(e),in_viewport:visible(e)})),card_text:card.innerText,target_text:target?.innerText};
      },{selector:anchors[step],width,step,zoom});
      result.geometry.push(geometry);
      if (step !== 'intro') {
        await tour.getByRole('button',{name:'去操作',exact:true}).click();
        await tour.waitFor({state:'hidden'});
        await page.waitForTimeout(150);
        result.focus.push(await page.evaluate(({selector,width,step,zoom}) => {
          const target=[...document.querySelectorAll(selector)].find(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height);
          const active=document.activeElement;
          return {width,zoom,step,active_tag:active?.tagName,active_class:active?.className,active_label:active?.getAttribute('aria-label'),active_text:active?.textContent?.slice(0,80),inside_target:target?.contains(active),target_tabindex:target?.getAttribute('tabindex')};
        },{selector:anchors[step],width,step,zoom}));
        if (step === 'graph') {
          const canvas=page.locator(anchors.graph);
          const before=await canvas.evaluate(e=>({...e._cyreg.cy.pan()}));
          await page.keyboard.press('ArrowRight');
          const after=await canvas.evaluate(e=>({...e._cyreg.cy.pan()}));
          result.focus.at(-1).keyboard_pan_works=before.x!==after.x || before.y!==after.y;
        }
        await launch.click();
        await page.locator(`dialog[data-tour-step="${step}"]`).waitFor();
      }
      if (step !== 'mcp') {
        await tour.locator('.tour-body').evaluateAll(elements=>elements.forEach(element=>element.scrollTop=element.scrollHeight));
        await tour.getByRole('button',{name:/^(下一步|开始引导)$/}).click();
      }
    }
    await page.keyboard.press('Escape');
    await tour.waitFor({state:'hidden'});
  }

  if (!process.env.BUSINESSMAP_TEST_GEOMETRY_ONLY) {
  // A modal tutorial should not save an unrelated dirty Markdown editor through
  // a bubbling application shortcut. Only this disposable fixture is mutated.
  await page.getByRole('navigation',{name:'应用导航'}).getByRole('button',{name:'业务地图',exact:true}).click();
  await page.locator('.note-article').waitFor();
  const path = await page.locator('.note-article').getAttribute('data-note-path');
  const api = path => page.evaluate(path => window.businessmap.request('GET',path),path);
  const original = await api(`/api/note?path=${encodeURIComponent(path)}`);
  await page.getByRole('button',{name:'编辑 Markdown',exact:true}).click();
  const marker = 'UX review modal keyboard save marker';
  await page.locator('.cm-content').fill(`${original.content}\n\n${marker}`);
  await page.getByText('有未保存的更改',{exact:false}).waitFor();
  // Progress still points at MCP. Dismiss the normal leave guard, then reach
  // the current page using the same tour controls available to a user.
  page.once('dialog', d => d.dismiss());
  await launch.click();
  result.dirty_launch_blocked = !await tour.isVisible();
  // Restore content temporarily so standard tour controls can reach knowledge.
  await page.locator('.cm-content').fill(original.content);
  await launch.click();
  await tour.getByRole('button',{name:'从头开始',exact:true}).click();
  await tour.getByRole('button',{name:'开始引导',exact:true}).click();
  await page.keyboard.press('Escape');
  await tour.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'编辑 Markdown',exact:true}).click();
  await page.locator('.cm-content').fill(`${original.content}\n\n${marker}`);
  await page.getByText('有未保存的更改',{exact:false}).waitFor();
  await launch.click();
  await page.locator('dialog[data-tour-step="knowledge"]').waitFor();
  const before = await api(`/api/note?path=${encodeURIComponent(path)}`);
  await page.keyboard.press(saveShortcut);
  await page.waitForTimeout(700);
  const after = await api(`/api/note?path=${encodeURIComponent(path)}`);
  result.shortcut_under_tour = {path,before_revision:before.revision,after_revision:after.revision,unexpected_saved:after.content.includes(marker),dialog_still_open:await tour.isVisible()};
  await capture('shortcut');
  result.mutations = (await desktop.evaluate(() => globalThis.__tourRequests)).filter(r => r.method !== 'GET');
  await page.keyboard.press('Escape');
  await tour.waitFor({state:'hidden'});
  const manualMarker = `${marker} after closing`;
  await page.locator('.cm-content').fill(`${original.content}\n\n${manualMarker}`);
  await page.keyboard.press(saveShortcut);
  await page.getByRole('button',{name:'已保存',exact:true}).waitFor();
  result.normal_save_still_works = (await api(`/api/note?path=${encodeURIComponent(path)}`)).content.includes(manualMarker);
  }
  result.checks = {
    every_title_visible:result.geometry.every(g=>g.title_visible),
    every_navigation_control_visible:result.geometry.every(g=>g.controls.every(c=>c.in_viewport)),
    normal_size_targets_unoccluded:result.geometry.filter(g=>g.zoom===1).every(g=>g.intersection<1),
    every_step_starts_at_top:result.geometry.every(g=>g.body_scroll_top===undefined || g.body_scroll_top===0),
    practice_focus_reaches_every_target:result.focus.every(f=>f.inside_target),
    graph_arrow_key_after_practice:result.focus.filter(f=>f.step==='graph').every(f=>f.keyboard_pan_works),
    no_renderer_errors:!result.errors.length,
    ...(result.shortcut_under_tour ? {no_background_save:!result.shortcut_under_tour.unexpected_saved,normal_save_still_works:result.normal_save_still_works} : {}),
  };
  if (process.env.BUSINESSMAP_TEST_ASSERT && Object.values(result.checks).some(check=>!check)) throw new Error(`Regression checks failed: ${Object.entries(result.checks).filter(([,pass])=>!pass).map(([name])=>name).join(', ')}`);
  result.completed = true;
} catch (error) {
  result.completed = false;
  result.failure = {message:error.message,stack:error.stack};
  if (page) await capture('failure').catch(()=>{});
  process.exitCode=1;
} finally {
  await writeFile(resolve(out,`${label}.json`),JSON.stringify(result,null,2));
  console.log(JSON.stringify({version:result.version,packaged:result.packaged,completed:result.completed,geometry_samples:result.geometry.length,focus_samples:result.focus.length,checks:result.checks,failure:result.failure,artifact:resolve(out,`${label}.json`)},null,2));
  await desktop.close();
}
