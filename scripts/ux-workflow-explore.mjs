import { _electron as electron } from '@playwright/test';
import electronBinary from 'electron';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dismissOnboarding } from './dismiss-onboarding.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const data = resolve(root, '.tmp', `ux-workflow-${Date.now()}`);
const artifacts = resolve(root, 'artifacts', 'ux-workflow-review');
await mkdir(data, {recursive:true});
await mkdir(artifacts, {recursive:true});
const isolatedVault = resolve(data, 'isolated-vault');
await mkdir(isolatedVault);
await writeFile(resolve(isolatedVault, '新知识库笔记.md'), '# 新知识库笔记\n\n不包含原知识库标签。\n');
const verify = process.argv.includes('--verify');
const packaged = process.env.BUSINESSMAP_TEST_EXECUTABLE;
const output = verify ? packaged ? 'packaged' : 'verified' : 'baseline';
const result = { tested_at: new Date().toISOString(), platform: process.platform, arch: process.arch, mode: verify ? 'verification' : 'baseline', packaged: Boolean(packaged), data, checks: {}, observations: {}, errors: [] };
const env = {...process.env,BUSINESSMAP_DATA_DIR:data,BUSINESSMAP_HEADLESS:'1',PYTHONUTF8:'1',TEMP:data,TMP:data,TMPDIR:data};
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
let desktop;
try {
  desktop = await electron.launch({executablePath:packaged || electronBinary,args:packaged ? [] : [root],cwd:root,env,chromiumSandbox:true,timeout:60000});
  result.version = await desktop.evaluate(({app})=>app.getVersion());
  const page = await desktop.firstWindow();
  page.setDefaultTimeout(10000);
  page.on('pageerror', error=>result.errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  await desktop.evaluate(({BrowserWindow})=>{const win=BrowserWindow.getAllWindows()[0];win.setSize(1080,700);win.webContents.setBackgroundThrottling(false);});
  await dismissOnboarding(page);
  const nav = page.getByRole('navigation',{name:'应用导航'});
  const api=(method,path,body)=>page.evaluate(({method,path,body})=>window.businessmap.request(method,path,body),{method,path,body});
  const capture = async name => {
    const frame = ()=>desktop.evaluate(async({BrowserWindow})=>(await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64'));
    await frame();await page.waitForTimeout(150);
    await writeFile(resolve(artifacts,`${output}-${name}.png`),Buffer.from(await frame(),'base64'));
  };
  await page.locator('#bm-chat-input').fill('尚未发送的业务逻辑问题');
  await nav.getByRole('button',{name:'模型配置',exact:true}).click();
  await page.getByRole('button',{name:'添加模型',exact:true}).click();
  await page.getByRole('dialog').getByLabel('模型 ID',{exact:true}).fill('local-test-only');
  await page.getByRole('dialog').getByLabel('API 地址',{exact:true}).fill('http://127.0.0.1:1/v1');
  await page.keyboard.press('Escape');
  await nav.getByRole('button',{name:'仓库',exact:true}).click();
  await page.getByRole('button',{name:'接入仓库',exact:true}).first().click();
  const repositoryDraft = resolve(data, 'example-local-only');
  await page.getByRole('dialog').getByLabel('仓库地址或本地路径').fill(repositoryDraft);
  await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'接入仓库',exact:true}).first().click();
  result.checks.repositoryDraftRetainedAfterCancel=await page.getByRole('dialog').getByLabel('仓库地址或本地路径').inputValue()===repositoryDraft;
  await page.keyboard.press('Escape');
  await nav.getByRole('button',{name:'AI 问答',exact:true}).click();
  result.checks.chatDraftSurvivesNavigation=await page.locator('#bm-chat-input').inputValue()==='尚未发送的业务逻辑问题';
  await nav.getByRole('button',{name:'数据库',exact:true}).click();
  await page.getByLabel('SQL 查询编辑器').fill("SELECT channel, COUNT(*) AS total FROM orders GROUP BY channel");
  await page.getByRole('button',{name:'执行查询',exact:true}).click();
  await page.getByRole('columnheader',{name:'total',exact:true}).waitFor();
  await page.getByRole('button',{name:'生成报表',exact:true}).click();
  await page.getByRole('dialog').getByLabel('报表标题').fill('工作流探索报表');
  await page.getByRole('dialog').getByRole('button',{name:'生成报表',exact:true}).click();
  await page.getByRole('button',{name:/工作流探索报表/}).click();
  await page.getByRole('heading',{name:'工作流探索报表',exact:true}).waitFor();
  result.checks.queryReportPipeline=true;
  result.checks.reportFitsMinimumWidth=await page.locator('.reports-content').evaluate(el=>el.scrollWidth<=el.clientWidth+1);
  await page.getByRole('button',{name:'数据源与查询',exact:true}).click();
  result.checks.querySurvivesReport=await page.getByRole('columnheader',{name:'total',exact:true}).isVisible();

  // Real vault change through the native picker; picker chooses only our fixture.
  await nav.getByRole('button',{name:'业务地图',exact:true}).click();
  await page.locator('.note-article').waitFor();
  const oldWorkspace=await api('GET','/api/workspace');
  const tag = page.locator('.sidebar-tags > div:last-child button').first();
  result.observations.oldTag=await tag.innerText();
  await tag.click();
  await desktop.evaluate(({dialog},path)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path]});},isolatedVault);
  await page.getByRole('button',{name:'切换知识库',exact:true}).click();
  await page.locator('.note-article').filter({hasText:'新知识库笔记'}).waitFor();
  result.observations.newVaultNoteCount=(await api('GET','/api/notes')).notes.length;
  result.observations.newVaultVisibleTreeRows=await page.locator('.file-row').count();
  result.observations.newVaultEmptyMessage=await page.locator('.sidebar-empty').textContent().catch(()=>null);
  result.observations.newVaultTagClearAvailable=await page.getByRole('button',{name:'清除',exact:true}).count()>0;
  result.checks.vaultChangeClearsStaleTag=result.observations.newVaultVisibleTreeRows===1;
  await capture('vault-tag');

  // Closing the only note during a real load must clear the busy state.
  // The IPC hook holds the real response until after closing the tab; it does
  // not fabricate note content or depend on pointer-action timings.
  await page.locator('.tab-close').click();
  await desktop.evaluate(({ipcMain})=>{
    const original=ipcMain._invokeHandlers.get('businessmap:request');
    ipcMain.removeHandler('businessmap:request');
    ipcMain.handle('businessmap:request',async(event,payload)=>{
      const response=await original(event,payload);
      if(payload.method==='GET'&&payload.path.startsWith('/api/note?'))await new Promise(resolve=>{globalThis.workflowReleaseNote=resolve;});
      return response;
    });
  });
  // Restore old vault to remove the stale tag regardless of whether it is fixed.
  await desktop.evaluate(({dialog},path)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[path]});},oldWorkspace.vault_path);
  await page.getByRole('button',{name:'切换知识库',exact:true}).click();
  await page.locator('.tab-close').waitFor();
  await page.getByText('正在打开笔记…',{exact:true}).waitFor();
  await page.locator('.tab-close').click();
  result.observations.pendingNoteBeforeClose=await desktop.evaluate(()=>typeof globalThis.workflowReleaseNote==='function');
  await desktop.evaluate(()=>globalThis.workflowReleaseNote?.());
  await page.waitForTimeout(350);
  result.observations.tabsAfterClose=await page.locator('.workspace-tab').count();
  result.observations.stillLoadingAfterClose=await page.getByText('正在打开笔记…',{exact:true}).isVisible();
  result.checks.closingLoadingLastTabShowsEmpty=await page.getByText('从一条知识，连接整个业务',{exact:true}).isVisible();
  await capture('close-loading-tab');
  result.checks.noRendererErrors=result.errors.length===0;
} catch(error) {
  result.fatal=String(error.stack||error);
} finally {
  await desktop?.close();
  await writeFile(resolve(artifacts,`${output}.json`),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
  if(result.fatal||verify&&Object.values(result.checks).some(ok=>!ok))process.exitCode=1;
}
