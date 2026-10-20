import type { CodexStatus, Provider } from './types';
import type { ProviderPreset } from './provider-presets';

export type ProviderForm = Pick<Provider, 'name' | 'kind' | 'base_url' | 'model'> & { id?: string; api_key: string };

export const newProviderForm = (preset: ProviderPreset): ProviderForm => ({
  name: preset.name, kind: preset.kind, base_url: preset.base_url,
  model: preset.kind === 'codex-local' ? 'default' : '', api_key: '',
});

export function changeProviderKind(form: ProviderForm, kind: Provider['kind']): ProviderForm {
  if (kind === form.kind) return form;
  if (kind === 'codex-local') return { ...form, kind, base_url: 'codex://local', model: 'default', api_key: '' };
  if (form.kind === 'codex-local') return { ...form, kind, base_url: '', model: '', api_key: '' };
  return { ...form, kind };
}

export function providerFormPayload(form: ProviderForm) {
  const { api_key, ...configuration } = form;
  if (form.kind === 'codex-local') return { ...configuration, base_url: 'codex://local', model: form.model.trim() || 'default' };
  return { ...configuration, api_key: api_key || undefined };
}

export function codexStatusLabel(status?: CodexStatus): string {
  if (!status) return '登录状态待检查';
  if (!status.installed || status.status === 'missing') return '未找到 Codex CLI';
  if (status.status === 'unsupported') return '当前 Codex CLI 版本不受支持';
  if (status.status === 'error') return 'Codex 状态检查失败';
  if (status.logged_in === false || status.status === 'login_required') return 'Codex 尚未登录';
  if (status.logged_in === true) return 'Codex 已安装 · 账号已登录';
  return 'Codex 已安装 · 登录状态待检查';
}
