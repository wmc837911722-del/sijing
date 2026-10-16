import { useEffect, useState } from 'react';
import { GitBranch, GitCommitHorizontal, GitFork, Plus, RefreshCw, Sparkles, CheckCircle2, AlertCircle, Clock3, Settings2 } from 'lucide-react';
import { errorMessage, formatTime, get, post } from '../api';
import type { AnalysisStatus, Notify, Provider, Repository, RepositoryModelPolicy, Task } from '../types';
import { Empty, Modal } from '../components/UI';
import CandidateReview from '../components/CandidateReview';
import RepositoryConnectionForm, { type RepositoryConnectionDraft } from '../components/RepositoryConnectionForm';

const policyDescriptions = {
  unbound: '尚未绑定模型。代码索引可用，AI 分析需要先选择允许接收代码的模型。',
  changed: '模型的服务地址或配置已变化。请核对后重新绑定，AI 更新已暂停。',
  missing: '绑定的模型已移除。请选择新的模型后再进行 AI 分析。',
  ready: '',
};
export default function Repositories({ repositories, providers, tasks, refresh, notify }: {
  repositories: Repository[]; providers: Provider[]; tasks: Task[]; refresh: () => void; notify: Notify;
}) {
  const [connectionEditor, setConnectionEditor] = useState<Repository | 'new' | null>(null);
  const [connectionDraft, setConnectionDraft] = useState<RepositoryConnectionDraft>();
  const [busy, setBusy] = useState<string | null>(null);
  const [candidateVersion, setCandidateVersion] = useState(0);
  const [policies, setPolicies] = useState<Record<string, RepositoryModelPolicy>>({});
  const [policyError, setPolicyError] = useState('');
  const [policyVersion, setPolicyVersion] = useState(0);
  const [binding, setBinding] = useState<Repository | null>(null);
  const [providerId, setProviderId] = useState('');
  const [bindingError, setBindingError] = useState('');
  const [analysisStatuses, setAnalysisStatuses] = useState<Record<string, AnalysisStatus>>({});
  const [analysisError, setAnalysisError] = useState('');
  const [analysisVersion, setAnalysisVersion] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void get<{ policies: Record<string, RepositoryModelPolicy> }>('/api/repository-model-policies')
      .then(result => { if (!cancelled) { setPolicies(result.policies); setPolicyError(''); } })
      .catch(err => { if (!cancelled) { setPolicies({}); setPolicyError(errorMessage(err)); } });
    return () => { cancelled = true; };
  }, [repositories, providers, policyVersion]);
  useEffect(() => {
    let cancelled = false;
    void get<{ statuses: Record<string, AnalysisStatus> }>('/api/analysis-status')
      .then(result => { if (!cancelled) { setAnalysisStatuses(result.statuses); setAnalysisError(''); } })
      .catch(err => { if (!cancelled) setAnalysisError(errorMessage(err)); });
    return () => { cancelled = true; };
  }, [repositories, analysisVersion]);
  const operate = async (repo: Repository, action: 'sync' | 'analyze') => {
    setBusy(`${repo.id}-${action}`);
    try {
      const result = await post<{ task_id?: string; notes_created?: number; summary?: string }>(`/api/repositories/${repo.id}/${action}`);
      notify(result.summary || (result.notes_created !== undefined ? `分析完成，生成 ${result.notes_created} 篇业务笔记` : action === 'sync' ? '同步任务已提交' : 'AI 分析任务已提交'));
      setCandidateVersion(version => version + 1);
    } catch (err) { notify(errorMessage(err), 'error'); setPolicyVersion(version => version + 1); }
    finally { setBusy(null); setAnalysisVersion(version => version + 1); refresh(); }
  };
  const toggleAnalysis = async (repo: Repository) => {
    setBusy(`${repo.id}-automatic`);
    try { await post(`/api/repositories/${repo.id}/auto-analysis`, { enabled: !repo.ai_auto_sync }); notify(repo.ai_auto_sync ? '已暂停自动 AI 更新，Git 索引同步保持开启' : '已启用自动 AI 更新'); refresh(); }
    catch (err) { notify(errorMessage(err), 'error'); }
    finally { setBusy(null); }
  };
  const openBinding = (repo: Repository) => { setBinding(repo); setProviderId(policies[repo.id]?.provider_id || ''); setBindingError(''); };
  const bindModel = async (event: React.FormEvent) => {
    event.preventDefault(); if (!binding || !providerId || busy) return;
    setBusy('binding'); setBindingError('');
    try {
      const policy = await post<RepositoryModelPolicy>(`/api/repositories/${binding.id}/model-policy`, { provider_id: providerId });
      setPolicies(current => ({ ...current, [binding.id]: policy })); setBinding(null);
      notify('已绑定分析模型。可手动发起首次 AI 分析。'); refresh();
    } catch (err) { setBindingError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const provider = providers.find(item => item.id === providerId);
  return <div className="page-scroll"><div className="page-content repositories-content">
    <header className="page-heading"><div><div className="eyebrow">SOURCE OF TRUTH</div><h1>仓库</h1><p>从代码理解业务，让知识始终有据可循。</p></div><button data-tour="repository-add" className="button primary" onClick={() => setConnectionEditor('new')}><Plus size={16}/>接入仓库</button></header>
    <div className="info-strip"><GitBranch size={17}/><span>支持公网、公司内网和本地 Git 仓库。接入只建立代码索引；应用内的 AI 分析与自动更新仅使用仓库明确绑定的模型。人工修改会保留供你审阅。</span></div>
    {policyError && <p className="form-error" role="alert">无法读取仓库模型配置：{policyError} <button className="text-button" onClick={() => setPolicyVersion(version => version + 1)}>重试</button></p>}
    {analysisError && <p className="form-error" role="alert">无法更新 AI 分析状态：{analysisError} <button className="text-button" onClick={() => setAnalysisVersion(version => version + 1)}>重试</button></p>}
    {repositories.length ? <div className="repo-grid">{repositories.map(repo => {
      const policy = policies[repo.id];
      const canAnalyze = policy?.status === 'ready';
      const connection = repo.connection;
      const analysis = analysisStatuses[repo.id];
      const analyzing = busy === `${repo.id}-analyze` || analysis?.status === 'running';
      const analysisState = analyzing ? 'running' : analysis?.status;
      const scope = analysis?.scope;
      const fileCount = Array.isArray(scope?.files) ? scope.files.length : scope?.files;
      const moduleCount = Array.isArray(scope?.total_modules) ? scope.total_modules.length : scope?.total_modules;
      return <article className="repo-card" key={repo.id} aria-label={repo.name}>
        <div className="repo-card-top"><span className="repo-symbol"><GitFork size={22}/></span><span className="badge">{({ ready: '已就绪', pending: '等待导入', syncing: '同步中', error: '处理失败' } as Record<string, string>)[repo.status] || '状态未知'}</span></div>
        <h3>{repo.name}</h3><p className="repo-url" title={repo.url}>{repo.url}</p>
        <div className="repo-meta"><span className="repo-branch" title={repo.branch || '默认分支'}><GitBranch size={13}/><span>{repo.branch || '默认分支'}</span></span><span className="repo-commit" title={repo.commit}><GitCommitHorizontal size={14}/>{repo.commit?.slice(0, 8) || '等待索引'}</span></div>
        <div className="repo-sync"><Clock3 size={12}/>最近同步 {formatTime(repo.last_sync)}</div>
        {connection && <div className="repo-connection-status"><span>{({ none: '无需账号', https: '账号认证', 'ssh-agent': 'SSH Agent', 'ssh-key': 'SSH 密钥' })[connection.auth_type]}</span>{connection.secret_storage === 'session' && <span>凭据仅本次运行有效</span>}{connection.auth_type === 'https' && !connection.has_secret && <span>需要补充凭据</span>}</div>}
        {repo.last_error?.message && <p className="form-error repo-error">{repo.last_error.message}</p>}
        <div className="repo-model-binding"><div><strong>{canAnalyze ? `${policy.provider_name || '已绑定模型'} · ${policy.model}` : 'AI 分析模型'}</strong><button className="text-button" onClick={() => openBinding(repo)} aria-label={`${repo.name} 绑定分析模型`}>{canAnalyze ? '更改绑定' : '绑定模型'}</button></div>{canAnalyze ? <p title={policy.base_url}>{policy.base_url}</p> : <p>{policy ? policyDescriptions[policy.status] : '正在读取模型绑定…'}</p>}</div>
        <div className={`repo-analysis-status ${analysisState || ''}`} aria-label={`${repo.name} AI 分析状态`}>
          <div><strong>{analysisState ? ({ never: '尚未分析', running: '正在分析', failed: '分析未完成', completed: '本轮分析完成' })[analysisState] : '正在读取分析状态…'}</strong>{analysis?.updated_at && <time dateTime={analysis.updated_at}>{formatTime(analysis.updated_at)}</time>}</div>
          <p>{analyzing ? '正在分析当前代码片段，请稍候。' : analysis?.summary || '分析结果与错误会保留在这里。'}</p>
          {scope && !analyzing && <p className="repo-analysis-scope">已读 {fileCount ?? 0}{scope.total_files !== undefined ? ` / ${scope.total_files}` : ''} 个文件 · {scope.line_count} 行{moduleCount !== undefined ? ` · 仓库共 ${moduleCount} 个模块` : ''}{scope.truncated ? ' · 范围受限' : ''}</p>}
          {analysis?.commit && analysis.commit !== repo.commit && <p>该结果来自提交 {analysis.commit.slice(0, 12)}，当前代码已更新。</p>}
        </div>
        <div className="repo-auto"><span>自动 AI 更新</span><button role="switch" aria-checked={Boolean(repo.ai_auto_sync && canAnalyze)} aria-label={`${repo.name} 自动 AI 更新`} className={`toggle ${repo.ai_auto_sync && canAnalyze ? 'on' : ''}`} disabled={Boolean(busy) || !canAnalyze} onClick={() => void toggleAnalysis(repo)}><i/></button></div>
        <div className="repo-actions"><button className="button compact" aria-label={`${repo.name} 连接设置`} onClick={() => setConnectionEditor(repo)} disabled={Boolean(busy)}><Settings2 size={14}/>连接设置</button><button className="button compact" onClick={() => void operate(repo, 'sync')} disabled={Boolean(busy) || repo.status === 'pending' || repo.status === 'syncing'}><RefreshCw size={14} className={busy === `${repo.id}-sync` ? 'spin' : ''}/>同步变更</button><button className="button compact accent" onClick={() => void operate(repo, 'analyze')} disabled={Boolean(busy) || analyzing || !canAnalyze || repo.status !== 'ready'} title={!canAnalyze ? '先绑定允许接收代码的模型' : undefined}>{analyzing ? <span className="small-spinner"/> : <Sparkles size={14}/>} {analyzing ? '正在分析…' : 'AI 分析'}</button></div>
      </article>;
    })}</div> : <Empty title="连接你的第一个代码仓库" detail="支持公司内网 HTTPS、SSH 和本地 Git 目录。仓库代码不会被执行。"><button className="button" onClick={() => setConnectionEditor('new')}><Plus size={15}/>接入仓库</button></Empty>}
    <section className="section-block"><div className="section-heading"><h2>任务记录</h2><span className="muted">{tasks.length} 项</span></div>{tasks.length ? <div className="task-list">{tasks.slice(0, 15).map(task => <div className="task-row" key={task.id}><span className={`task-icon ${task.status}`}>{task.status === 'completed' ? <CheckCircle2 size={17}/> : task.status === 'failed' ? <AlertCircle size={17}/> : <RefreshCw size={16} className={task.status === 'running' ? 'spin' : ''}/>}</span><div><strong>{task.message || '仓库任务'}</strong>{task.error ? <small className="danger">{task.error}</small> : <small>{formatTime(task.created_at)}</small>}{['running', 'queued'].includes(task.status) && <progress max={100} value={task.progress}/>}</div><span className="muted">{{ completed: '已完成', failed: '失败', running: '进行中', queued: '等待中' }[task.status]}</span></div>)}</div> : <p className="subtle-empty">仓库导入和同步任务会显示在这里。</p>}</section>
    <CandidateReview notify={notify} refresh={refresh} reloadVersion={candidateVersion}/>
    {connectionEditor && <RepositoryConnectionForm repository={connectionEditor === 'new' ? undefined : connectionEditor} draft={connectionEditor === 'new' ? connectionDraft : undefined} onDraftChange={connectionEditor === 'new' ? setConnectionDraft : undefined} onClose={() => setConnectionEditor(null)} onSaved={() => { if (connectionEditor === 'new') setConnectionDraft(undefined); refresh(); }} notify={notify}/>}
    {binding && <Modal title="绑定仓库分析模型" busy={busy === 'binding'} onClose={() => setBinding(null)}><form className="form-stack" onSubmit={bindModel}>
      <p className="form-hint">为「{binding.name}」选择公司允许接收代码片段的模型。全局默认模型切换不会改变此绑定。</p>
      <label>分析模型<select aria-label="分析模型" autoFocus required disabled={busy === 'binding'} value={providerId} onChange={event => setProviderId(event.target.value)}><option value="">请选择模型</option>{providers.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}</select></label>
      {provider && <div className="repo-provider-preview"><strong>{provider.name} · {provider.model}</strong><p>代码发送至：{provider.base_url}</p><p>绑定仅保存选择，不会立即发送代码。服务地址或型号变更后需重新绑定。</p></div>}
      <p className="form-hint">绑定约束应用内分析和问答；其他 AI 工具通过 MCP 读取内容后使用哪个模型，由该工具控制。</p>
      {!providers.length && <p className="repository-notice">请先在左侧「模型配置」添加模型。当前仍可导入仓库、同步代码索引和浏览已有业务地图。</p>}
      {bindingError && <p role="alert" className="form-error">{bindingError}</p>}
      <div className="modal-actions"><button className="button" type="button" disabled={busy === 'binding'} onClick={() => setBinding(null)}>取消</button><button className="button primary" disabled={busy === 'binding' || !provider}>{busy === 'binding' ? '正在绑定…' : '绑定模型'}</button></div>
    </form></Modal>}
  </div></div>;
}
