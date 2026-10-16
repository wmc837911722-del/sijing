import { spawnSync } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync } from 'node:fs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temp = resolve(root, '.tmp');
mkdirSync(temp, { recursive: true });
const python = process.env.BUSINESSMAP_PYTHON || resolve(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const result = spawnSync(python, ['-m', 'pytest', 'backend/tests', '-q', '-o', `cache_dir=${resolve(root, '.cache/pytest')}`, `--basetemp=${resolve(temp, `pytest-${Date.now()}`)}`], {
  cwd: root,
  stdio: 'inherit',
  windowsHide: true,
  env: { ...process.env, PYTHONPATH: resolve(root, 'backend'), PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', TEMP: temp, TMP: temp, TMPDIR: temp },
});
if (result.error) throw result.error;
process.exitCode = result.status || 0;
