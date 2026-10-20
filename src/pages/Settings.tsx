import { useState } from 'react';
import { Bot, Check, Copy, KeyRound, Plug, Plus, ShieldCheck, Zap, Pencil, FlaskConical, Terminal } from 'lucide-react';
import type { Notify, Provider, Settings as SettingsData } from '../types';
import { errorMessage, post } from '../api';
import { Empty, Modal } from '../components/UI';
import { PROVIDER_GROUPS, PROVIDER_PRESETS } from '../provider-presets';
import { changeProviderKind, codexStatusLabel, newProviderForm, providerFormPayload, type ProviderForm } from '../provider-form';

export default function Settings({ settings, refresh, notify }: { settings: SettingsData | null; refresh: () => void; notify: Notify }) {
  const [form, setForm] = useState<ProviderForm | null>(null);
  const [presetId, setPresetId] = useState('kimi');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const formPreset = form && PROVIDER_PRESETS.find(preset => preset.kind === form.kind && preset.base_url === form.base_url.trim().replace(/\/+$/, ''));
  const isLocalCodex = form?.kind === 'codex-local';
  const selectPreset = (id: string) => {
    const preset = PROVIDER_PRESETS.find(item => item.id === id);
    if (!preset) return;
    setPresetId(id); setForm(newProviderForm(preset)); setError('');
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); if (!form || busy) return; setBusy('save'); setError('');
    try { await post<Provider>('/api/providers', providerFormPayload(form)); setForm(null); refresh(); notify('模型配置已保存'); }
    catch (err) { setError(errorMessage(err)); } finally { setBusy(''); }
  };
  const operation = async (id: string, action: 'test' | 'activate') => { setBusy(id); try { const result = await post<{ ok: boolean; message?: string }>(`/api/providers/${id}/${action}`); if (!result.ok) throw new Error(result.message || '连接失败'); notify(result.message || '已切换当前模型'); refresh(); } catch (err) { notify(errorMessage(err), 'error'); } finally { setBusy(''); } };
  const copyMcp = async () => { if (!settings) return; try { await navigator.clipboard.writeText(JSON.stringify({ mcpServers: { businessmap: { command: settings.mcp.command, args: settings.mcp.args } } }, null, 2)); notify('MCP 配置已复制'); } catch (err) { notify(errorMessage(err), 'error'); } };
  return <div className="page-scroll"><div className="page-content settings-content"><header className="page-heading"><div><div className="eyebrow">YOUR WORKSPACE, YOUR MODELS</div><h1>模型配置</h1><p>连接模型服务，用于代码分析、业务归纳与问答。</p></div></header>
    <section className="settings-section"><div className="section-heading"><h2><span className="settings-section-icon"><Bot size={17}/></span>模型服务</h2><button data-tour="model-add" className="button compact" disabled={Boolean(busy)} onClick={() => selectPreset('kimi')}><Plus size={14}/>添加模型</button></div>{settings?.providers.length ? <div className="provider-list">{settings.providers.map(provider => <div className={`provider-card ${provider.active ? 'is-active' : ''}`} key={provider.id}>
      <div className="provider-avatar">{provider.kind === 'codex-local' ? <Terminal size={22}/> : <Bot size={22}/>}</div>
      <div className="provider-info"><strong>{provider.name}{provider.active && <span className="badge green">当前使用</span>}</strong><span>{provider.kind === 'codex-local' && provider.model === 'default' ? 'Codex CLI 内置默认模型' : provider.model}</span>
        {provider.kind === 'codex-local' ? <>
          <small><Terminal size={11}/>{codexStatusLabel(provider.codex_status)}{provider.codex_status?.version ? ` · ${provider.codex_status.version}` : ''}</small>
          {provider.codex_status?.detail && <p className="form-hint">{provider.codex_status.detail}</p>}
          <p className="form-hint">支持完整代码分析与业务归纳；暂不支持数据或工具问答。测试会消耗 Codex 额度。</p>
        </> : <small><KeyRound size={11}/>{provider.has_key ? provider.secret_storage === 'keyring' ? '密钥保存在系统凭据库' : '密钥仅本次会话可用' : '尚未配置密钥'}</small>}
      </div>
      <div className="provider-buttons"><button className="icon-button" disabled={Boolean(busy)} title="编辑模型" aria-label={`编辑 ${provider.name}`} onClick={() => { setError(''); setForm({ id: provider.id, name: provider.name, kind: provider.kind, base_url: provider.base_url, model: provider.model, api_key: '' }); }}><Pencil size={15}/></button><button className="button compact" disabled={Boolean(busy)} onClick={() => void operation(provider.id, 'test')}><FlaskConical size={13}/>{busy === provider.id ? '请求中…' : '测试'}</button>{!provider.active && <button className="button compact" disabled={Boolean(busy)} onClick={() => void operation(provider.id, 'activate')}>启用</button>}</div>
    </div>)}</div> : <Empty title="连接一个模型，开始理解业务" detail="可使用本机 Codex 账号，或连接 DeepSeek、Kimi 等 API 服务。"/>}</section>
    <section className="settings-section"><div className="section-heading"><h2><span className="settings-section-icon"><Zap size={17}/></span>模型使用与缓存</h2><span className="badge">实际 API 统计</span></div><div className="usage-grid"><div><span>累计请求</span><strong>{(settings?.usage.requests || 0).toLocaleString()}</strong></div><div><span>输入 tokens</span><strong>{settings?.usage.input_tokens == null ? '—' : settings.usage.input_tokens.toLocaleString()}</strong></div><div><span>输出 tokens</span><strong>{settings?.usage.output_tokens == null ? '—' : settings.usage.output_tokens.toLocaleString()}</strong></div><div><span>缓存命中 tokens</span><strong className="accent-text">{settings?.usage.cached_tokens == null ? '—' : settings.usage.cached_tokens.toLocaleString()}</strong></div></div><p className="form-hint">缓存由各模型服务分别管理。这里显示服务商返回的实际数据；未返回的指标显示为「—」。</p></section>
    <section className="settings-section"><div className="section-heading"><h2><span className="settings-section-icon"><Plug size={17}/></span>连接其他 AI 工具</h2><button data-tour="mcp-copy" className="button compact" disabled={!settings} onClick={() => void copyMcp()}><Copy size={13}/>复制 MCP 配置</button></div><p className="muted">让支持 MCP 的工具读取业务知识、检查数据结构并执行只读查询。</p>{settings && <pre className="config-code">{JSON.stringify({ mcpServers: { businessmap: { command: settings.mcp.command, args: settings.mcp.args } } }, null, 2)}</pre>}<div className="form-hint"><ShieldCheck size={14}/>{settings?.mcp.description || '所有调用遵循统一的只读查询约束。'}</div></section>
    {form && <Modal busy={busy === 'save'} title={form.id ? '编辑模型服务' : '添加模型服务'} onClose={() => { if (busy !== 'save') setForm(null); }}>
      <form className="form-stack" onSubmit={save}>
        <fieldset className="form-fields" disabled={busy === 'save'}>
          {!form.id && <label>服务预设
            <select aria-label="服务预设" value={presetId} onChange={event => selectPreset(event.target.value)}>
              {PROVIDER_GROUPS.map(group => <optgroup key={group} label={group}>
                {PROVIDER_PRESETS.filter(preset => preset.group === group).map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
              </optgroup>)}
            </select>
          </label>}
          <div className="form-columns">
            <label>显示名称<input required value={form.name} onChange={event => setForm({ ...form, name: event.target.value })}/></label>
            <label>接口类型<select aria-label="接口类型" value={form.kind} onChange={event => setForm(changeProviderKind(form, event.target.value as Provider['kind']))}>
              <option value="openai-compatible">OpenAI 兼容</option><option value="anthropic">Anthropic</option><option value="gemini">Gemini</option><option value="codex-local">本机 Codex 账号</option>
            </select></label>
          </div>
          {!isLocalCodex && <><label>API 地址<input required type="url" value={form.base_url} onChange={event => setForm({ ...form, base_url: event.target.value })} placeholder="https://api.example.com/v1" aria-describedby="provider-address-hint"/></label>
          <p className="form-hint" id="provider-address-hint">填写接口基础地址，不含 /chat/completions；地区、企业网关或专用套餐地址可自行修改。</p>
          </>}
          <label>{isLocalCodex ? 'Codex 模型（可选）' : '模型 ID'}<input required={!isLocalCodex} value={form.model} onChange={event => setForm({ ...form, model: event.target.value })} list="provider-model-suggestions" placeholder={isLocalCodex ? 'default：CLI 内置默认模型' : formPreset?.models[0] ? `例如：${formPreset.models[0]}` : '填写控制台已开通的模型 ID'} aria-describedby="provider-model-hint"/></label>
          <datalist id="provider-model-suggestions">{formPreset?.models.map(model => <option key={model} value={model}/>)}</datalist>
          <p className="form-hint" id="provider-model-hint">{isLocalCodex ? '留空或填写 default 将使用当前 CLI 版本的内置默认模型；长任务建议填写明确模型 ID。' : '参考名称可直接修改，模型以你的控制台为准。当前支持文本对话接口，请选择支持非流式调用的模型。'}</p>
          {isLocalCodex ? <>
            <p className="form-hint"><Terminal size={14}/>请先在本机安装 Codex CLI，并使用 codex login 登录。保存后点击「测试」检查真实调用，测试会消耗账号额度。</p>
            <p className="form-hint">{formPreset?.hint || '使用本机 Codex CLI 已登录的账号与对应额度，无需 API 密钥。代码片段会通过 Codex 发送给 OpenAI。'}</p>
            <p className="form-hint">支持完整代码分析与业务归纳；暂不支持数据或工具问答。</p>
            <p className="form-hint">保存或启用后，请在项目中确认模型授权。既有分析结果是否能接续，以项目中的实际核验结果为准。</p>
          </> : <><label>API 密钥 <span className="muted">{form.id ? '（留空保留原密钥）' : ''}</span><input type="password" autoComplete="new-password" value={form.api_key} onChange={event => setForm({ ...form, api_key: event.target.value })} placeholder={form.id ? '保留现有密钥' : '输入 API 密钥'} aria-describedby="provider-key-hint"/></label>
          <p className="form-hint" id="provider-key-hint">{formPreset?.hint || '使用此接口对应的 API 凭据；聊天网站会员不等同于 API 服务。'}</p>
          <p className="form-hint"><ShieldCheck size={14}/>优先使用系统凭据库；不可用时仅在当前会话保存。</p>
          </>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="modal-actions"><button type="button" className="button" disabled={busy === 'save'} onClick={() => setForm(null)}>取消</button><button className="button primary" disabled={Boolean(busy)}><Check size={14}/>{busy === 'save' ? '保存中…' : '保存配置'}</button></div>
        </fieldset>
      </form>
    </Modal>}
  </div></div>;
}
