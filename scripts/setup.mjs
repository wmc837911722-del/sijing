import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temp = resolve(root, '.tmp');
mkdirSync(temp, { recursive: true });
const env = { ...process.env, TEMP: temp, TMP: temp, TMPDIR: temp, PYTHONUTF8: '1', PIP_CACHE_DIR: resolve(root, '.cache/pip') };
const python = resolve(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
function run(cmd, args) {
  const result = spawnSync(cmd, args, { cwd: root, env, stdio: 'inherit', windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
if (!existsSync(python)) run(process.env.BUSINESSMAP_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['-m', 'venv', '.venv']);
run(python, ['-m', 'pip', 'install', '-r', 'backend/requirements-dev.txt']);
console.log('Backend ready. Run npm ci, then npm run dev.');
