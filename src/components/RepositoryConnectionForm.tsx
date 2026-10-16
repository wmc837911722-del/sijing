import { useState } from 'react';
import { CheckCircle2, FolderOpen, PlugZap } from 'lucide-react';
import { errorMessage, post, request } from '../api';
import type { Notify, Repository, RepositoryConnectionInput } from '../types';
import { Modal } from './UI';
import './repository-connection.css';

type Source = 'https' | 'ssh' | 'local';
type ConnectionTest = { ok: boolean; branches: string[]; default_branch: string | null };
export type RepositoryConnectionDraft = { url: string; name: string; source: Source; auth: RepositoryConnectionInput['auth_type']; username: string; proxy: string; ca: string; keyPath: string; hostsPath: string; branch: string };
function sourceOf(url: string): Source {
  if (/^(ssh:\/\/|[^\s/@:]+@[^\s/:]+:)/i.test(url)) return 'ssh';
  if (/^https?:\/\//i.test(url) || !url) return 'https';
  return 'local';
}

export default function RepositoryConnectionForm({ repository, draft, onDraftChange, onClose, onSaved, notify }: {
  repository?: Repository; draft?: RepositoryConnectionDraft; onDraftChange?: (draft: RepositoryConnectionDraft) => void; onClose: () => void; onSaved: () => void; notify: Notify;
}) {
  const saved = repository?.connection;
  const [url, setUrl] = useState(repository?.url || draft?.url || '');
  const [name, setName] = useState(draft?.name || '');
  const [source, setSource] = useState<Source>(() => repository ? sourceOf(repository.url) : draft?.source || 'https');
  const [auth, setAuth] = useState<RepositoryConnectionInput['auth_type']>(saved?.auth_type || draft?.auth || (source === 'ssh' ? 'ssh-agent' : 'none'));
  const [username, setUsername] = useState(saved?.username || draft?.username || '');
  const [secret, setSecret] = useState('');
  const [clearSecret, setClearSecret] = useState(false);
  const [proxy, setProxy] = useState(saved?.proxy_url || draft?.proxy || '');
  const [ca, setCa] = useState(saved?.ca_bundle || draft?.ca || '');
  const [keyPath, setKeyPath] = useState(saved?.ssh_key_path || draft?.keyPath || '');
  const [hostsPath, setHostsPath] = useState(saved?.known_hosts_path || draft?.hostsPath || '');
  const [branch, setBranch] = useState(repository?.branch || draft?.branch || '');
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<'test' | 'save' | null>(null);
  const close = () => {
    onDraftChange?.({ url, name, source, auth, username, proxy, ca, keyPath, hostsPath, branch });
    setSecret(''); onClose();
  };
  const resetTest = () => { setTest(null); setError(''); };
  const changeSource = (next: Source) => {
    setSource(next); setAuth(next === 'ssh' ? 'ssh-agent' : 'none'); setSecret(''); setClearSecret(false); setBranch(''); resetTest();
  };
  const changeUrl = (value: string) => {
    setUrl(value); resetTest();
    // Pasted clone URLs/local paths select the appropriate fields immediately.
    if (/^(https?:\/\/|ssh:\/\/|[^\s/@:]+@[^\s/:]+:|[A-Za-z]:[\\/]|\/|~\/)/.test(value)) {
      const next = sourceOf(value); if (next !== source) changeSource(next);
    }
  };
  const connection = (): RepositoryConnectionInput => source === 'local' ? { auth_type: 'none' } : source === 'ssh' ? {
    auth_type: auth === 'ssh-key' ? 'ssh-key' : 'ssh-agent', ssh_key_path: auth === 'ssh-key' ? keyPath.trim() : '', known_hosts_path: hostsPath.trim(),
  } : {
    auth_type: auth === 'https' ? 'https' : 'none', username: auth === 'https' ? username.trim() : '',
    ...(auth === 'https' && (secret || clearSecret) ? { secret: clearSecret ? '' : secret } : {}), proxy_url: proxy.trim(), ca_bundle: ca.trim(),
  };
  const validate = () => {
    if (!url.trim()) return '请填写仓库地址或选择本地目录。';
    if (source === 'https' && !/^https?:\/\//i.test(url.trim())) return '请填写完整的 HTTPS 地址，或切换为 SSH / 本地目录。';
    if (source === 'ssh' && sourceOf(url.trim()) !== 'ssh') return '请填写 SSH 克隆地址，例如 git@git.company.local:team/project.git。';
    if (source === 'local' && /^(https?:|ssh:)/i.test(url.trim())) return '这里需要本地 Git 目录，请切换仓库类型后填写远程地址。';
    if (auth === 'https' && !username.trim()) return '请填写 Git 用户名。Token 的用户名要求由公司 Git 服务决定。';
    if (auth === 'https' && !secret && !saved?.has_secret && !clearSecret) return '请填写 Token 或密码。';
    if (source === 'ssh' && auth === 'ssh-key' && !keyPath.trim()) return '请选择 SSH 私钥文件，或使用 SSH Agent。';
    return '';
  };
  const runTest = async () => {
    const invalid = validate(); if (invalid) { setError(invalid); return; }
    setBusy('test'); setError(''); setTest(null);
    try { setTest(await post<ConnectionTest>('/api/repositories/test', { url: url.trim(), connection: connection(), repository_id: repository?.id })); }
    catch (err) { setError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); if (busy) return;
    const invalid = validate(); if (invalid) { setError(invalid); return; }
    setBusy('save'); setError('');
    try {
      const payload = { connection: connection(), branch: source === 'local' ? '' : branch };
      const result = repository
        ? await request<Repository>('PUT', `/api/repositories/${repository.id}/connection`, payload)
        : await post<Repository>('/api/repositories', { ...payload, url: url.trim(), name: name.trim() || undefined });
      setSecret('');
      notify(result.connection?.secret_storage === 'session' ? '连接已保存。凭据仅本次运行有效，重启后需重新填写。' : repository ? '连接已更新，可同步重试' : '仓库已接入，导入进度见任务列表');
      onSaved(); onClose();
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const choosePath = async (set: (value: string) => void, kind: 'directory' | 'ca' | 'ssh-key' | 'known-hosts') => {
    try {
      const value = kind === 'directory' ? await window.businessmap?.chooseDirectory() : await window.businessmap?.chooseRepositoryFile(kind);
      if (value) { set(value); resetTest(); }
    } catch (err) { setError(errorMessage(err)); }
  };
  return <Modal wide busy={Boolean(busy)} title={repository ? '仓库连接设置' : '接入代码仓库'} onClose={close}>
    <form className="form-stack repository-form" onSubmit={save}>
      <div className="repository-form-scroll"><fieldset className="form-fields" disabled={Boolean(busy)}>
        <label>仓库类型<select aria-label="仓库类型" value={source} disabled={Boolean(repository)} onChange={event => changeSource(event.target.value as Source)}><option value="https">HTTPS · 公网或公司内网</option><option value="ssh">SSH · 公司密钥 / Agent</option><option value="local">本地 Git 目录</option></select></label>
        <label>仓库地址或本地路径<div className="input-action"><input required autoFocus readOnly={Boolean(repository)} value={url} onChange={event => changeUrl(event.target.value)} placeholder={source === 'local' ? '选择已克隆的 Git 目录' : source === 'ssh' ? 'git@git.company.local:team/project.git' : 'https://git.company.local/team/project.git'}/>{!repository && <button type="button" className="icon-button" title="选择本地仓库" aria-label="选择本地仓库目录" onClick={() => void choosePath(value => { changeSource('local'); setUrl(value); }, 'directory')}><FolderOpen size={17}/></button>}</div></label>
        {!repository && <label>显示名称 <span className="muted">（可选）</span><input value={name} onChange={event => setName(event.target.value)} placeholder="例如：交易服务"/></label>}
        {source !== 'local' && <label>认证方式<select aria-label="认证方式" value={auth} onChange={event => { setAuth(event.target.value as RepositoryConnectionInput['auth_type']); setSecret(''); setClearSecret(false); resetTest(); }}>{source === 'https' ? <><option value="none">无需账号 · 公开仓库</option><option value="https">用户名 + Token / 密码</option></> : <><option value="ssh-agent">使用 SSH Agent</option><option value="ssh-key">指定 SSH 私钥文件</option></>}</select></label>}
        {source === 'https' && auth === 'https' && <>
          <label>Git 用户名<input value={username} autoComplete="username" onChange={event => { setUsername(event.target.value); resetTest(); }}/></label>
          <label>Token 或密码<input type="password" autoComplete="new-password" value={secret} disabled={clearSecret} onChange={event => { setSecret(event.target.value); resetTest(); }} placeholder={saved?.has_secret ? '已保存凭据；留空继续使用' : '推荐使用只读仓库 Token'}/></label>
          {saved?.has_secret && <label className="repository-checkbox"><input type="checkbox" checked={clearSecret} onChange={event => { setClearSecret(event.target.checked); setSecret(''); resetTest(); }}/>清除已保存凭据</label>}
          <p className="form-hint">凭据优先保存在系统凭据库；不可用时仅本次运行有效。网页登录状态不会自动授权 Git，请使用公司允许的 Token 或 Git 密码。</p>
          {saved?.secret_storage === 'session' && <p className="repository-notice">当前凭据仅本次运行有效，重启后需要重新填写。</p>}
        </>}
        {source === 'ssh' && <>
          {auth === 'ssh-key' && <label>SSH 私钥文件<div className="input-action"><input value={keyPath} onChange={event => { setKeyPath(event.target.value); resetTest(); }} placeholder="本机私钥路径"/><button className="icon-button" type="button" aria-label="选择 SSH 私钥文件" onClick={() => void choosePath(setKeyPath, 'ssh-key')}><FolderOpen size={16}/></button></div></label>}
          <p className="form-hint">使用本机 SSH Agent 中已解锁的密钥；有口令的私钥请先加入 Agent。首次访问须先在公司认可的终端确认服务器指纹。</p>
        </>}
        {source !== 'local' && <details className="repository-advanced"><summary>网络与信任设置 <span className="muted">（可选）</span></summary><div className="form-fields">
          {source === 'https' ? <>
            <label>代理地址<input value={proxy} onChange={event => { setProxy(event.target.value); resetTest(); }} placeholder="http://127.0.0.1:7890"/></label>
            <label>公司 CA 证书文件<div className="input-action"><input value={ca} onChange={event => { setCa(event.target.value); resetTest(); }} placeholder="PEM 证书文件路径"/><button type="button" className="icon-button" aria-label="选择公司 CA 证书" onClick={() => void choosePath(setCa, 'ca')}><FolderOpen size={16}/></button></div></label>
          </> : <label>已信任主机文件<div className="input-action"><input value={hostsPath} onChange={event => { setHostsPath(event.target.value); resetTest(); }} placeholder="留空使用 ~/.ssh/known_hosts"/><button type="button" className="icon-button" aria-label="选择已信任主机文件" onClick={() => void choosePath(setHostsPath, 'known-hosts')}><FolderOpen size={16}/></button></div></label>}
          <p className="form-hint">内网仓库需先连接公司的网络或 VPN。保持证书和服务器指纹校验。</p>
        </div></details>}
        {source === 'local' ? <p className="form-hint">读取本地当前分支并建立索引，不执行仓库代码。后续拉取请使用公司的 Git 客户端，再在这里同步变更。</p> : <label>同步分支{test ? <select aria-label="同步分支" value={branch} onChange={event => setBranch(event.target.value)}><option value="">默认分支{test.default_branch ? `（${test.default_branch}）` : ''}</option>{branch && !test.branches.includes(branch) && <option value={branch}>{branch}（未找到）</option>}{test.branches.map(item => <option value={item} key={item}>{item}</option>)}</select> : <input value={branch} onChange={event => setBranch(event.target.value)} placeholder="留空使用默认分支；测试连接可读取分支"/>}</label>}
        {test && <p className="repository-test-success" role="status"><CheckCircle2 size={16}/><span>连接成功 · 发现 {test.branches.length} 个分支{source === 'local' ? '，将读取本地当前分支。' : '，可选择分支后接入。'}</span></p>}
        <p className="form-hint">接入仅建立代码索引。接入后需单独绑定允许接收代码的模型，才可发起 AI 分析。</p>
      </fieldset></div>
      <div className="repository-form-footer">
        {busy === 'test' && <p className="form-hint" role="status">正在联系仓库并读取分支，最长等待 45 秒…</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="modal-actions"><button type="button" className="button repository-test-button" disabled={Boolean(busy) || !url.trim()} onClick={() => void runTest()}>{busy === 'test' ? <span className="small-spinner"/> : <PlugZap size={15}/>} {busy === 'test' ? '正在测试…' : '测试连接'}</button><button className="button" type="button" disabled={Boolean(busy)} onClick={close}>取消</button><button className="button primary" disabled={Boolean(busy) || !url.trim()}>{busy === 'save' ? '正在保存…' : repository ? '保存连接' : '接入仓库'}</button></div>
      </div>
    </form>
  </Modal>;
}
