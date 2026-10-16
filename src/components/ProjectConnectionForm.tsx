import { useRef, useState, type FormEvent } from 'react';
import { CheckCircle2, FolderOpen, PlugZap } from 'lucide-react';
import { errorMessage, post } from '../api';
import type { BusinessProject, Notify, RepositoryConnectionInput } from '../types';
import { Modal } from './UI';
import './repository-connection.css';

type ConnectionDraft = { url: string; branch: string; connection: RepositoryConnectionInput };
type TestResult = { ok: boolean; branches: string[]; default_branch: string | null };
const emptyDraft = (): ConnectionDraft => ({ url: '', branch: '', connection: { auth_type: 'none' } });
function sourceOf(url: string): 'https' | 'ssh' | 'local' {
  url = url.trim();
  if (/^(ssh:\/\/|[^\s/@:]+@[^\s/:]+:)/i.test(url)) return 'ssh';
  if (/^https?:\/\//i.test(url) || !url) return 'https';
  return 'local';
}
function validate(draft: ConnectionDraft, label: string) {
  if (!draft.url.trim()) return `请填写${label}仓库地址或选择本地 Git 目录。`;
  const source = sourceOf(draft.url.trim());
  if (source === 'local' && !/^(\/|~\/|[A-Za-z]:[\\/])/.test(draft.url.trim())) return `${label}仓库需要完整的 HTTPS / SSH 地址或本地绝对路径。`;
  if (source === 'https' && draft.connection.auth_type === 'https') {
    if (!draft.connection.username?.trim()) return `请填写${label}仓库的 Git 用户名。`;
    if (!draft.connection.secret) return `请填写${label}仓库的 Token 或密码。`;
  }
  if (source === 'ssh' && draft.connection.auth_type === 'ssh-key' && !draft.connection.ssh_key_path?.trim()) return `请选择${label}仓库的 SSH 私钥文件。`;
  return '';
}
function RepositorySide({ label, draft, onChange, disabled, onError }: { label: string; draft: ConnectionDraft; onChange: (value: ConnectionDraft) => void; disabled: boolean; onError: (message: string) => void }) {
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const source = sourceOf(draft.url);
  const setConnection = (change: Partial<RepositoryConnectionInput>) => { onChange({ ...draft, connection: { ...draft.connection, ...change } }); setTest(null); };
  const choose = async (kind: 'directory' | 'ca' | 'ssh-key' | 'known-hosts') => {
    try {
      const value = kind === 'directory' ? await window.businessmap?.chooseDirectory() : await window.businessmap?.chooseRepositoryFile(kind);
      if (!value) return;
      if (kind === 'directory') onChange({ url: value, branch: '', connection: { auth_type: 'none' } });
      else setConnection({ [kind === 'ca' ? 'ca_bundle' : kind === 'ssh-key' ? 'ssh_key_path' : 'known_hosts_path']: value });
      setTest(null);
    } catch (err) { onError(errorMessage(err)); }
  };
  const runTest = async () => {
    const invalid = validate(draft, label); if (invalid) { onError(invalid); return; }
    setTesting(true); setTest(null); onError('');
    try { setTest(await post<TestResult>('/api/repositories/test', { url: draft.url.trim(), connection: draft.connection })); }
    catch (err) { onError(`${label}仓库：${errorMessage(err)}`); }
    finally { setTesting(false); }
  };
  return <fieldset className="project-import-side form-fields" disabled={disabled || testing}><legend>{label}仓库</legend>
    <label>{label}仓库地址<div className="input-action"><input required aria-label={`${label}仓库地址`} value={draft.url} placeholder="HTTPS / SSH 地址，或选择本地 Git 目录" onChange={event => { const url = event.target.value; const next = sourceOf(url); onChange({ ...draft, url, connection: next === source ? draft.connection : { auth_type: next === 'ssh' ? 'ssh-agent' : 'none' } }); setTest(null); }}/><button type="button" className="icon-button" title={`选择${label}本地仓库`} aria-label={`选择${label}本地仓库`} onClick={() => void choose('directory')}><FolderOpen size={16}/></button></div></label>
    <details className="repository-advanced"><summary>{label}分支与连接设置 <span className="muted">（可选）</span></summary><div className="form-fields">
      {source !== 'local' && <label>{label}同步分支{test ? <select value={draft.branch} onChange={event => onChange({ ...draft, branch: event.target.value })}><option value="">默认分支{test.default_branch ? `（${test.default_branch}）` : ''}</option>{draft.branch && !test.branches.includes(draft.branch) && <option value={draft.branch}>{draft.branch}</option>}{test.branches.map(branch => <option key={branch} value={branch}>{branch}</option>)}</select> : <input value={draft.branch} onChange={event => onChange({ ...draft, branch: event.target.value })} placeholder="留空使用默认分支"/>}</label>}
      {source !== 'local' && <label>{label}认证方式<select value={draft.connection.auth_type} onChange={event => setConnection({ auth_type: event.target.value as RepositoryConnectionInput['auth_type'], secret: '' })}>{source === 'https' ? <><option value="none">无需账号 · 公开仓库</option><option value="https">用户名 + Token / 密码</option></> : <><option value="ssh-agent">使用 SSH Agent</option><option value="ssh-key">指定 SSH 私钥文件</option></>}</select></label>}
      {source === 'https' && draft.connection.auth_type === 'https' && <><label>{label} Git 用户名<input autoComplete="username" value={draft.connection.username || ''} onChange={event => setConnection({ username: event.target.value })}/></label><label>{label} Token 或密码<input type="password" autoComplete="new-password" value={draft.connection.secret || ''} onChange={event => setConnection({ secret: event.target.value })}/></label><p className="form-hint">凭据优先保存在系统凭据库；不可用时仅本次运行有效。</p></>}
      {source === 'https' && <><label>{label}代理地址<input value={draft.connection.proxy_url || ''} onChange={event => setConnection({ proxy_url: event.target.value })} placeholder="http://127.0.0.1:7890"/></label><label>{label}公司 CA 证书<div className="input-action"><input value={draft.connection.ca_bundle || ''} onChange={event => setConnection({ ca_bundle: event.target.value })} placeholder="PEM 证书文件路径"/><button type="button" className="icon-button" aria-label={`选择${label}公司 CA 证书`} onClick={() => void choose('ca')}><FolderOpen size={16}/></button></div></label></>}
      {source === 'ssh' && <>{draft.connection.auth_type === 'ssh-key' && <label>{label} SSH 私钥<div className="input-action"><input value={draft.connection.ssh_key_path || ''} onChange={event => setConnection({ ssh_key_path: event.target.value })}/><button type="button" className="icon-button" aria-label={`选择${label} SSH 私钥`} onClick={() => void choose('ssh-key')}><FolderOpen size={16}/></button></div></label>}<label>{label}已信任主机文件<div className="input-action"><input value={draft.connection.known_hosts_path || ''} onChange={event => setConnection({ known_hosts_path: event.target.value })} placeholder="留空使用 ~/.ssh/known_hosts"/><button type="button" className="icon-button" aria-label={`选择${label}已信任主机文件`} onClick={() => void choose('known-hosts')}><FolderOpen size={16}/></button></div></label><p className="form-hint">使用本机已解锁的 SSH 密钥，保留服务器指纹校验。内网仓库需连接公司网络或 VPN。</p></>}
      {source === 'local' && <p className="form-hint">读取本地 Git 当前分支，不执行仓库代码。</p>}
      <button type="button" className="button compact" disabled={!draft.url.trim()} onClick={() => void runTest()}>{testing ? <span className="small-spinner"/> : <PlugZap size={14}/>} {testing ? '正在测试…' : `测试${label}连接`}</button>
    </div></details>
    {test && <p className="repository-test-success" role="status"><CheckCircle2 size={15}/>{label}连接成功 · {test.branches.length} 个分支</p>}
  </fieldset>;
}
export default function ProjectConnectionForm({ onClose, onSaved, notify }: { onClose: () => void; onSaved: () => void; notify: Notify }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [frontend, setFrontend] = useState(emptyDraft);
  const [backend, setBackend] = useState(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submitting = useRef(false);
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (submitting.current) return;
    const invalid = !name.trim() ? '请填写业务项目名称。' : validate(frontend, '前端') || validate(backend, '后端');
    if (invalid) { setError(invalid); return; }
    if (frontend.url.trim() === backend.url.trim()) { setError('前端和后端仓库地址相同，请核对各端仓库。'); return; }
    submitting.current = true; setBusy(true); setError('');
    const payload = (draft: ConnectionDraft) => ({ url: draft.url.trim(), branch: sourceOf(draft.url) === 'local' ? '' : draft.branch.trim(), connection: draft.connection });
    try {
      await post<BusinessProject>('/api/projects/import', { name: name.trim(), description: description.trim(), frontend: [payload(frontend)], backend: [payload(backend)] });
      notify('业务项目已接入，前端和后端正在建立代码索引。'); onSaved(); onClose();
    } catch (err) { setError(errorMessage(err)); }
    finally { submitting.current = false; setBusy(false); }
  };
  return <Modal wide title="接入业务项目" busy={busy} onClose={onClose}><form className="form-stack repository-form" onSubmit={save}>
    <div className="repository-form-scroll"><div className="form-fields"><label>项目名称<input required autoFocus maxLength={100} disabled={busy} value={name} onChange={event => setName(event.target.value)} placeholder="例如：天梯赛事"/></label><p className="form-hint">前端和后端共同组成一个业务项目，分析后统一生成业务地图与总结。更多客户端可在项目中继续追加。</p>
      <RepositorySide label="前端" draft={frontend} onChange={setFrontend} disabled={busy} onError={setError}/>
      <RepositorySide label="后端" draft={backend} onChange={setBackend} disabled={busy} onError={setError}/>
      <label>业务说明 <span className="muted">（可选）</span><textarea rows={2} maxLength={2000} disabled={busy} value={description} onChange={event => setDescription(event.target.value)} placeholder="这些代码共同实现什么业务"/></label><p className="form-hint">接入仅导入代码资料。完成后需绑定项目模型，才会发送代码进行 AI 分析。</p>
    </div></div><div className="repository-form-footer">{error && <p role="alert" className="form-error">{error}</p>}<div className="modal-actions"><button type="button" className="button" disabled={busy} onClick={onClose}>取消</button><button className="button primary" disabled={busy || !name.trim() || !frontend.url.trim() || !backend.url.trim()}>{busy ? '正在接入…' : '接入项目与前后端仓库'}</button></div></div>
  </form></Modal>;
}
