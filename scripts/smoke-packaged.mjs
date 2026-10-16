import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const metadata = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const build = metadata.build;
const productName = build.productName || metadata.productName || metadata.name;
const platformKey = { win32: 'win', darwin: 'mac', linux: 'linux' }[process.platform];
const binaryName = build[platformKey]?.executableName || build.executableName
  || (process.platform === 'linux' ? metadata.name.toLowerCase() : productName);
const release = resolve(root, 'release');
const folders = existsSync(release) ? readdirSync(release, { withFileTypes: true }).filter(item => item.isDirectory()).map(item => item.name) : [];
let candidates;
if (process.platform === 'win32') {
  candidates = folders.filter(name => /^win.*unpacked$/.test(name)).map(name => resolve(release, name, `${binaryName}.exe`));
} else if (process.platform === 'darwin') {
  candidates = folders.filter(name => /^mac(?:-|$)/.test(name)).map(name => resolve(release, name, `${binaryName}.app`, 'Contents/MacOS', binaryName));
} else {
  candidates = folders.filter(name => /^linux.*unpacked$/.test(name)).map(name => resolve(release, name, binaryName));
}
candidates = candidates.filter(existsSync);
const executable = process.env.BUSINESSMAP_TEST_EXECUTABLE || (candidates.length === 1 ? candidates[0] : null);
if (!executable) throw new Error('Expected one native unpacked desktop build. Set BUSINESSMAP_TEST_EXECUTABLE to select one.');
const artifacts = resolve(root, 'artifacts');
const temporary = resolve(root, '.tmp');
mkdirSync(artifacts, { recursive: true });
mkdirSync(temporary, { recursive: true });
const env = { ...process.env, BUSINESSMAP_TEST_EXECUTABLE: executable, TEMP: temporary, TMP: temporary, TMPDIR: temporary };
delete env.ELECTRON_RUN_AS_NODE;
delete env.BUSINESSMAP_DEV_URL;
const report = { platform: process.platform, arch: process.arch, executable, full_suite: process.argv.includes('--all'), started_at: new Date().toISOString(), stages: [], passed: false };
function run(name, command, args, extraEnv = {}) {
  console.log(`Verifying ${name} on ${process.platform}/${process.arch}`);
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit', windowsHide: true, timeout: 15 * 60_000, env: { ...env, ...extraEnv } });
  const passed = !result.error && result.status === 0;
  report.stages.push({ name, passed, exit_code: result.status, ...(result.error ? { error: result.error.message } : {}) });
  writeFileSync(resolve(artifacts, 'packaged-suite.json'), JSON.stringify(report, null, 2));
  if (!passed) process.exit(result.status || 1);
}
const script = name => resolve(root, 'scripts', name);
run('Desktop and business flows', process.execPath, [script('smoke-desktop.mjs')]);

const python = process.env.BUSINESSMAP_PYTHON || resolve(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const service = process.platform === 'darwin'
  ? resolve(dirname(executable), '../Resources/backend/businessmap-service')
  : resolve(dirname(executable), 'resources/backend', process.platform === 'win32' ? 'businessmap-service.exe' : 'businessmap-service');
run('Read-only MCP', python, [script('smoke-mcp.py'), '--executable', service]);
if (report.full_suite) {
  run('Native desktop lifecycle and shortcuts', process.execPath, [script('smoke-platform.mjs')]);
  run('Enterprise repository connections and model binding', process.execPath, [script('ux-repository-connections.mjs')]);
  run('Onboarding', process.execPath, [script('ux-onboarding.mjs')]);
  run('Onboarding keyboard', process.execPath, [script('ux-onboarding-keyboard.mjs')]);
  run('Workspace navigation', process.execPath, [script('ux-workflow-explore.mjs'), '--verify']);
  run('Tour placement and native save shortcut', process.execPath, [script('ux-tour-explore.mjs')], { BUSINESSMAP_TEST_ASSERT: '1', BUSINESSMAP_TEST_LABEL: 'packaged', BUSINESSMAP_TEST_ZOOM_SET: '', BUSINESSMAP_TEST_GEOMETRY_ONLY: '' });
  run('Tour at 125% and 150% zoom', process.execPath, [script('ux-tour-explore.mjs')], { BUSINESSMAP_TEST_ASSERT: '1', BUSINESSMAP_TEST_LABEL: 'packaged-zoom', BUSINESSMAP_TEST_ZOOM_SET: '1.25,1.5', BUSINESSMAP_TEST_GEOMETRY_ONLY: '1' });
}
report.passed = true;
report.completed_at = new Date().toISOString();
writeFileSync(resolve(artifacts, 'packaged-suite.json'), JSON.stringify(report, null, 2));
