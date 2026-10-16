import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve, dirname } from 'node:path';
import electron from 'electron';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const vite = spawn(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1'], { cwd: root, stdio: 'inherit', windowsHide: true });
let desktop;
const stop = () => { desktop?.kill(); vite.kill(); };
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
process.on('exit', stop);
for (let attempt = 0; attempt < 120; attempt++) {
  try {
    const response = await fetch('http://127.0.0.1:5173');
    if (response.ok) break;
  } catch {}
  await new Promise((resolve) => setTimeout(resolve, 250));
  if (attempt === 119) { stop(); throw new Error('Vite did not start'); }
}
const env = { ...process.env, BUSINESSMAP_DEV_URL: 'http://127.0.0.1:5173' };
delete env.ELECTRON_RUN_AS_NODE;
desktop = spawn(electron, ['.'], { cwd: root, env, stdio: 'inherit', windowsHide: true });
desktop.on('exit', (code) => { vite.kill(); process.exitCode = code || 0; });
