import assert from 'node:assert/strict';
import test from 'node:test';
import { PROVIDER_PRESETS } from '../src/provider-presets.ts';
import { changeProviderKind, newProviderForm, providerFormPayload, codexStatusLabel } from '../src/provider-form.ts';
import { canContinueWithModel, confirmsModelContinuation } from '../src/project-model-binding.ts';

test('local Codex uses account login and cannot submit a copied API credential', () => {
  const preset = PROVIDER_PRESETS.find(item => item.id === 'codex-local');
  assert.ok(preset);
  const form = newProviderForm(preset);
  assert.equal(form.kind, 'codex-local');
  assert.equal(form.model, 'default');
  const payload = providerFormPayload({ ...form, api_key: 'credential-from-another-service', base_url: 'https://unused.example', model: ' ' });
  assert.equal(payload.base_url, 'codex://local');
  assert.equal(payload.model, 'default');
  assert.equal(Object.hasOwn(payload, 'api_key'), false);
});

test('switching between HTTP and account login clears incompatible connection details', () => {
  const form = { name: 'My service', kind: 'openai-compatible', base_url: 'https://example.com/v1', model: 'remote-model', api_key: 'old-secret' };
  const local = changeProviderKind(form, 'codex-local');
  assert.equal(local.api_key, '');
  assert.equal(local.base_url, 'codex://local');
  assert.equal(local.model, 'default');
  const remote = changeProviderKind(local, 'anthropic');
  assert.equal(remote.base_url, '');
  assert.equal(remote.model, '');
  assert.equal(remote.api_key, '');
});

test('HTTP provider edits preserve an existing key when the field stays empty', () => {
  const payload = providerFormPayload({ id: 'saved', name: 'DeepSeek', kind: 'openai-compatible', base_url: 'https://api.deepseek.com', model: 'existing-model', api_key: '' });
  assert.equal(payload.id, 'saved');
  assert.equal(payload.api_key, undefined);
  assert.equal(payload.model, 'existing-model');
});

test('local account status distinguishes unknown, missing and logged out states', () => {
  assert.equal(codexStatusLabel(undefined), '登录状态待检查');
  assert.equal(codexStatusLabel({ installed: false, logged_in: null, status: 'missing', version: null, detail: '' }), '未找到 Codex CLI');
  assert.equal(codexStatusLabel({ installed: true, logged_in: false, status: 'login_required', version: '1.0', detail: '' }), 'Codex 尚未登录');
  assert.equal(codexStatusLabel({ installed: true, logged_in: true, status: 'ready', version: '1.0', detail: '' }), 'Codex 已安装 · 账号已登录');
});

test('cross-model continuation is only offered for a current, stopped checkpoint with receipts', () => {
  const policy = { status: 'ready', provider_id: 'old-model' };
  const analysis = { run_id: 'saved-run', status: 'paused', stale: false, coverage: { completed_chunks: 915 } };
  assert.equal(canContinueWithModel(policy, analysis, 'codex'), true);
  assert.equal(canContinueWithModel(policy, analysis, 'old-model'), false);
  assert.equal(canContinueWithModel(policy, analysis, ''), false);
  assert.equal(canContinueWithModel(policy, { ...analysis, stale: true }, 'codex'), false);
  assert.equal(canContinueWithModel({ ...policy, status: 'changed' }, analysis, 'codex'), false);
  assert.equal(canContinueWithModel(policy, { ...analysis, coverage: { completed_chunks: 0 } }, 'codex'), false);
  for (const status of ['never', 'planning', 'running', 'completed']) {
    assert.equal(canContinueWithModel(policy, { ...analysis, status }, 'codex'), false, status);
  }
});

test('a timed-out continuation is confirmed only by the exact source and selected target', () => {
  const analysis = { run_id: 'new-run', status: 'paused', reused_chunks: 915, receipt_migration: { source_run_id: 'old-run', target_provider: { id: 'codex' } } };
  assert.equal(confirmsModelContinuation(analysis, 'old-run', 'codex'), true);
  assert.equal(confirmsModelContinuation(analysis, 'another-run', 'codex'), false);
  assert.equal(confirmsModelContinuation(analysis, 'old-run', 'another-provider'), false);
  assert.equal(confirmsModelContinuation({ run_id: 'old-run', status: 'paused' }, 'old-run', 'codex'), false);
  assert.equal(confirmsModelContinuation({ receipt_migration: { source_run_id: 'old-run' } }, 'old-run', 'codex'), false);
  assert.equal(confirmsModelContinuation(undefined, 'old-run', 'codex'), false);
  assert.equal(confirmsModelContinuation(analysis, undefined, 'codex'), false);
});
