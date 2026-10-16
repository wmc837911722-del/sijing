import { spawnSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temp = resolve(root, '.tmp');
mkdirSync(temp, { recursive: true });
const python = process.env.BUSINESSMAP_PYTHON || resolve(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const env = { ...process.env, TEMP: temp, TMP: temp, TMPDIR: temp, PYTHONUTF8: '1', PYINSTALLER_CONFIG_DIR: resolve(root, '.cache/pyinstaller') };
// PyInstaller freezes the interpreter on this machine; it cannot cross-compile
// an Intel backend into an Apple Silicon (or another OS) Electron package.
const targetArch = process.env.BUSINESSMAP_TARGET_ARCH || process.arch;
if (!['x64', 'arm64'].includes(targetArch) || targetArch !== process.arch) {
  throw new Error(`Build natively with Node ${targetArch}; this Node runtime is ${process.arch}.`);
}
const probe = spawnSync(python, ['-c', 'import json, platform, struct, sys; print(json.dumps({"platform": sys.platform, "machine": platform.machine(), "bits": struct.calcsize("P") * 8, "version": platform.python_version()}))'], { cwd: root, env, encoding: 'utf8', windowsHide: true });
if (probe.error) throw probe.error;
if (probe.status !== 0) throw new Error(`Cannot inspect build Python: ${probe.stderr.trim()}`);
const runtime = JSON.parse(probe.stdout.trim());
const pythonArch = { amd64: 'x64', x86_64: 'x64', arm64: 'arm64', aarch64: 'arm64' }[runtime.machine.toLowerCase()];
if (runtime.platform !== process.platform || runtime.bits !== 64 || pythonArch !== targetArch) {
  throw new Error(`Python ${runtime.platform}/${runtime.machine}/${runtime.bits}-bit does not match Electron ${process.platform}/${targetArch}. Recreate .venv with a matching native Python.`);
}
console.log(`Native backend build: ${process.platform}/${targetArch}, Python ${runtime.version}.`);
if (process.argv.includes('--check')) process.exit(0);
const args = ['-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'businessmap-service', '--distpath', 'build/backend', '--workpath', 'build/pyinstaller', '--specpath', 'build', '--paths', 'backend', '--collect-all', 'keyring', '--collect-submodules', 'mcp.server', '--collect-submodules', 'mcp.shared', '--copy-metadata', 'mcp', '--collect-all', 'sqlglot', '--collect-all', 'psycopg', '--collect-all', 'psycopg_binary', '--collect-submodules', 'uvicorn', 'backend/entry.py'];
if (process.platform === 'darwin') args.splice(-1, 0, '--target-architecture', targetArch === 'x64' ? 'x86_64' : targetArch);
const result = spawnSync(python, args, { cwd: root, env, stdio: 'inherit', windowsHide: true });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
