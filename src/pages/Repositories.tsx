import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { GitBranch, GitCommitHorizontal, GitFork, Plus, RefreshCw, Sparkles, CheckCircle2, AlertCircle, Clock3, Settings2, FolderOpen, Pause } from 'lucide-react';
import { errorMessage, formatTime, get, post, request } from '../api';
import type { AnalysisStatus, BusinessProject, FullAnalysisStatus, Notify, Provider, Repository, RepositoryModelPolicy, Task } from '../types';
import { Empty, Modal } from '../components/UI';
import CandidateReview from '../components/CandidateReview';
import ProjectConnectionForm from '../components/ProjectConnectionForm';
import RepositoryConnectionForm, { type RepositoryConnectionDraft } from '../components/RepositoryConnectionForm';
import ProjectAnalysisProgress from '../components/ProjectAnalysisProgress';
import { canContinueWithModel, confirmsModelContinuation } from '../project-model-binding';
import './model-binding.css';

const policyDescriptions = {
  unbound: '尚未绑定模型。请先选择允许接收代码的模型。',
  changed: '模型配置或项目成员已变化。请核对后重新绑定。',
  missing: '绑定的模型已移除，请选择新的模型。',
  ready: '',
};
type Binding = { kind: 'project' | 'repository'; id: string; name: string; count?: number };
type ProjectDraft = { project?: BusinessProject; name: string; description: string; repository_ids: string[]; repository_roles: Record<string, 'frontend' | 'backend'> };
function AnalysisState({ name, analysis, running, commit }: { name: string; analysis?: AnalysisStatus; running: boolean; commit?: string }) {
  const state = running ? 'running' : analysis?.status;
  const scope = analysis?.scope;
  const count = Array.isArray(scope?.files) ? scope.files.length : scope?.files;
  return <div className={`repo-analysis-status ${state || ''}`} aria-label={`${name} AI 分析状态`}>
    <div><strong>{state ? ({ never: '尚未分析', running: '正在分析', failed: '分析未完成', completed: '本轮分析完成' })[state] : '正在读取分析状态…'}</strong>{analysis?.updated_at && <time dateTime={analysis.updated_at}>{formatTime(analysis.updated_at)}</time>}</div>
    <p>{running ? '正在结合项目范围内的代码分析业务，请稍候。' : analysis?.summary || '分析结果与覆盖范围会显示在这里。'}</p>
    {scope && !running && <p className="repo-analysis-scope">已读 {count ?? 0}{scope.total_files !== undefined ? ` / ${scope.total_files}` : ''} 个文件 · {scope.line_count} 行{scope.repositories ? ` · ${scope.repositories.length} 个仓库` : ''}{scope.truncated ? ' · 局部分析' : ''}</p>}
    {commit && analysis?.commit && analysis.commit !== commit && <p>该结果来自提交 {analysis.commit.slice(0, 12)}，当前代码已更新。</p>}
  </div>;
}
export default function Repositories({ projects, repositories, providers, tasks, refresh, notify }: {
  projects: BusinessProject[]; repositories: Repository[]; providers: Provider[]; tasks: Task[]; refresh: () => void; notify: Notify;
}) {
  const [importProject, setImportProject] = useState(false);
  const [connectionEditor, setConnectionEditor] = useState<Repository | 'new' | null>(null);
  const [connectionDraft, setConnectionDraft] = useState<RepositoryConnectionDraft>();
  const [projectDraft, setProjectDraft] = useState<ProjectDraft | null>(null);
  const [projectError, setProjectError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [candidateVersion, setCandidateVersion] = useState(0);
  const [policies, setPolicies] = useState<Record<string, RepositoryModelPolicy>>({});
  const [projectPolicies, setProjectPolicies] = useState<Record<string, RepositoryModelPolicy>>({});
  const [policyError, setPolicyError] = useState('');
  const [policyVersion, setPolicyVersion] = useState(0);
  const [binding, setBinding] = useState<Binding | null>(null);
  const [providerId, setProviderId] = useState('');
  const [bindingError, setBindingError] = useState('');
  const [reuseCompleted, setReuseCompleted] = useState(true);
  const [analysisStatuses, setAnalysisStatuses] = useState<Record<string, AnalysisStatus>>({});
  const [projectStatuses, setProjectStatuses] = useState<Record<string, AnalysisStatus>>({});
  const [fullAnalysisStatuses, setFullAnalysisStatuses] = useState<Record<string, FullAnalysisStatus>>({});
  const [fullAnalysisErrors, setFullAnalysisErrors] = useState<Record<string, string>>({});
  const [analysisError, setAnalysisError] = useState('');
  const [analysisVersion, setAnalysisVersion] = useState(0);
  const projectIdsKey = JSON.stringify(projects.map(project => project.id).sort());
  const projectByRepository = useMemo(() => new Map(projects.flatMap(project => project.repository_ids.map(id => [id, project] as const))), [projects]);
  const repositoryById = useMemo(() => new Map(repositories.map(repo => [repo.id, repo])), [repositories]);
  const unassigned = repositories.filter(repo => !projectByRepository.has(repo.id));
  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      get<{ policies: Record<string, RepositoryModelPolicy> }>('/api/repository-model-policies'),
      Promise.all(projects.map(async project => [project.id, await get<RepositoryModelPolicy>(`/api/projects/${project.id}/model-policy`)] as const)),
    ]).then(([result, projectResults]) => { if (!cancelled) { setPolicies(result.policies); setProjectPolicies(Object.fromEntries(projectResults)); setPolicyError(''); } })
      .catch(err => { if (!cancelled) { setPolicies({}); setProjectPolicies({}); setPolicyError(errorMessage(err)); } });
    return () => { cancelled = true; };
  }, [repositories, projects, providers, policyVersion]);
  useEffect(() => {
    let cancelled = false;
    void get<{ statuses: Record<string, AnalysisStatus> }>('/api/analysis-status')
      .then(result => { if (!cancelled) { setAnalysisStatuses(result.statuses); setAnalysisError(''); } })
      .catch(err => { if (!cancelled) setAnalysisError(errorMessage(err)); });
    return () => { cancelled = true; };
  }, [repositories, tasks, analysisVersion]);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const projectIds = JSON.parse(projectIdsKey) as string[];
    const poll = async () => {
      const results = await Promise.all(projectIds.map(async id => {
        const [full, preview] = await Promise.allSettled([
          get<FullAnalysisStatus>(`/api/projects/${id}/full-analysis`),
          get<AnalysisStatus>(`/api/projects/${id}/analysis-status`),
        ]);
        return { id, full, preview };
      }));
      if (cancelled) return;
      setFullAnalysisStatuses(previous => Object.fromEntries(results.flatMap(({ id, full }) => full.status === 'fulfilled' ? [[id, full.value]] : previous[id] ? [[id, previous[id]]] : [])));
      setFullAnalysisErrors(Object.fromEntries(results.flatMap(({ id, full }) => full.status === 'rejected' ? [[id, errorMessage(full.reason)]] : [])));
      setProjectStatuses(Object.fromEntries(results.flatMap(({ id, preview }) => preview.status === 'fulfilled' ? [[id, preview.value]] : [])));
      const running = results.some(({ full }) => full.status === 'fulfilled' && ['planning', 'running'].includes(full.value.status));
      timer = setTimeout(() => void poll(), running ? 3000 : 10000);
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [projectIdsKey, analysisVersion]);
  const operate = async (id: string, action: 'sync' | 'analyze', kind: Binding['kind'] = 'repository') => {
    setBusy(`${id}-${action}`);
    try {
      const result = await post<{ task_id?: string; notes_created?: number; summary?: string }>(`/api/${kind === 'project' ? 'projects' : 'repositories'}/${id}/${action}`);
      notify(result.summary || (action === 'sync' ? '同步任务已提交' : 'AI 分析任务已提交'));
      setCandidateVersion(version => version + 1);
    } catch (err) { notify(errorMessage(err), 'error'); setPolicyVersion(version => version + 1); }
    finally { setBusy(null); setAnalysisVersion(version => version + 1); refresh(); }
  };
  const toggleAnalysis = async (repo: Repository) => {
    setBusy(`${repo.id}-automatic`);
    try { await post(`/api/repositories/${repo.id}/auto-analysis`, { enabled: !repo.ai_auto_sync }); notify(repo.ai_auto_sync ? '已暂停自动 AI 更新' : '已启用自动 AI 更新'); refresh(); }
    catch (err) { notify(errorMessage(err), 'error'); }
    finally { setBusy(null); }
  };
  const pauseFullAnalysis = async (id: string, paused: boolean) => {
    setBusy(`${id}-pause`);
    try {
      await post(`/api/projects/${id}/full-analysis/pause`, { paused });
      notify(paused ? '已请求暂停，正在进行的模型调用完成后停止新批次。' : '已提交继续完整分析，将复用同一版本已完成的批次。');
    } catch (err) { notify(errorMessage(err), 'error'); }
    finally { setBusy(null); setAnalysisVersion(version => version + 1); refresh(); }
  };
  const openBinding = (target: Binding) => { setBinding(target); setProviderId((target.kind === 'project' ? projectPolicies : policies)[target.id]?.provider_id || ''); setReuseCompleted(true); setBindingError(''); };
  const bindingAnalysis = binding?.kind === 'project' ? fullAnalysisStatuses[binding.id] : undefined;
  const canReuseCompleted = binding?.kind === 'project' && canContinueWithModel(projectPolicies[binding.id], bindingAnalysis, providerId);
  const bindModel = async (event: FormEvent) => {
    event.preventDefault(); if (!binding || !providerId || busy) return;
    const continuation = canReuseCompleted && reuseCompleted ? { sourceRunId: bindingAnalysis?.run_id, providerId } : null;
    const acceptContinuation = (analysis: FullAnalysisStatus) => {
      setFullAnalysisStatuses(current => ({ ...current, [binding.id]: analysis }));
      setPolicyVersion(version => version + 1);
      notify(`已核验并接续 ${analysis.reused_chunks ?? 0} 个已完成批次，保留原模型来源。点击「继续完整分析」后才会发送新请求。`);
    };
    const finishBinding = () => { setBinding(null); setAnalysisVersion(version => version + 1); refresh(); };
    setBusy('binding'); setBindingError('');
    try {
      if (continuation) {
        const analysis = await post<FullAnalysisStatus>(`/api/projects/${binding.id}/full-analysis/continue-with-model`, { provider_id: continuation.providerId, source_run_id: continuation.sourceRunId });
        acceptContinuation(analysis);
      } else {
        const policy = await post<RepositoryModelPolicy>(`/api/${binding.kind === 'project' ? 'projects' : 'repositories'}/${binding.id}/model-policy`, { provider_id: providerId });
        (binding.kind === 'project' ? setProjectPolicies : setPolicies)(current => ({ ...current, [binding.id]: policy }));
        notify('已绑定分析模型，可手动发起 AI 分析。');
      }
      finishBinding();
    } catch (err) {
      if (continuation) {
        let analysis: FullAnalysisStatus | undefined;
        try { analysis = await get<FullAnalysisStatus>(`/api/projects/${binding.id}/full-analysis`); } catch { /* The mutation may still be completing; never retry it automatically. */ }
        if (analysis && confirmsModelContinuation(analysis, continuation.sourceRunId, continuation.providerId)) {
          acceptContinuation(analysis); finishBinding();
        } else {
          setBindingError('操作结果尚未确认，请刷新项目状态后再操作。');
        }
      } else { setBindingError(errorMessage(err)); }
    }
    finally { setBusy(null); }
  };
  const editProject = (project?: BusinessProject) => { setProjectDraft({ project, name: project?.name || '', description: project?.description || '', repository_ids: project?.repository_ids || [], repository_roles: project?.repository_roles || {} }); setProjectError(''); };
  const saveProject = async (event: FormEvent) => {
    event.preventDefault(); if (!projectDraft || busy || !projectDraft.name.trim()) return;
    setBusy('project'); setProjectError('');
    try {
      const payload = { name: projectDraft.name.trim(), description: projectDraft.description.trim(), repository_ids: projectDraft.repository_ids, repository_roles: Object.fromEntries(projectDraft.repository_ids.map(id => [id, projectDraft.repository_roles[id] || 'frontend'])), ...(projectDraft.project ? { revision: projectDraft.project.revision } : {}) };
      await request<BusinessProject>(projectDraft.project ? 'PUT' : 'POST', projectDraft.project ? `/api/projects/${projectDraft.project.id}` : '/api/projects', payload);
      setProjectDraft(null); setPolicyVersion(version => version + 1); notify('项目已保存。成员变化后，请核对项目模型绑定再分析。'); refresh();
    } catch (err) { setProjectError(errorMessage(err)); }
    finally { setBusy(null); }
  };
  const provider = providers.find(item => item.id === providerId);
  const renderRepository = (repo: Repository, grouped = false) => {
    const policy = policies[repo.id];
    const canAnalyze = policy?.status === 'ready';
    const connection = repo.connection;
    const analysis = analysisStatuses[repo.id];
    const analyzing = busy === `${repo.id}-analyze` || analysis?.status === 'running';
    return <article className={`repo-card${grouped ? ' project-repository' : ''}`} key={repo.id} aria-label={repo.name}>
      <div className="repo-card-top"><span className="repo-symbol"><GitFork size={22}/></span><span className="badge">{({ ready: '已就绪', pending: '等待导入', syncing: '同步中', error: '处理失败' } as Record<string, string>)[repo.status] || '状态未知'}</span></div>
      <h3>{repo.name}</h3><p className="repo-url" title={repo.url}>{repo.url}</p>
      <div className="repo-meta"><span className="repo-branch" title={repo.branch || '默认分支'}><GitBranch size={13}/><span>{repo.branch || '默认分支'}</span></span><span className="repo-commit" title={repo.commit}><GitCommitHorizontal size={14}/>{repo.commit?.slice(0, 8) || '等待索引'}</span></div>
      <div className="repo-sync"><Clock3 size={12}/>最近同步 {formatTime(repo.last_sync)}</div>
      {connection && <div className="repo-connection-status"><span>{({ none: '无需账号', https: '账号认证', 'ssh-agent': 'SSH Agent', 'ssh-key': 'SSH 密钥' })[connection.auth_type]}</span>{connection.secret_storage === 'session' && <span>凭据仅本次运行有效</span>}{connection.auth_type === 'https' && !connection.has_secret && <span>需要补充凭据</span>}</div>}
      {repo.last_error?.message && <p className="form-error repo-error">{repo.last_error.message}</p>}
      {!grouped && <>
        <div className="repo-model-binding"><div><strong>{canAnalyze ? `${policy.provider_name || '已绑定模型'} · ${policy.model}` : 'AI 分析模型'}</strong><button className="text-button" onClick={() => openBinding({ kind: 'repository', id: repo.id, name: repo.name })}>{canAnalyze ? '更改绑定' : '绑定模型'}</button></div>{canAnalyze ? <p title={policy.base_url}>{policy.base_url}</p> : <p>{policy ? policyDescriptions[policy.status] : '正在读取模型绑定…'}</p>}</div>
        <AnalysisState name={repo.name} analysis={analysis} running={analyzing} commit={repo.commit}/>
        <div className="repo-auto"><span>自动 AI 更新</span><button role="switch" aria-checked={Boolean(repo.ai_auto_sync && canAnalyze)} aria-label={`${repo.name} 自动 AI 更新`} className={`toggle ${repo.ai_auto_sync && canAnalyze ? 'on' : ''}`} disabled={Boolean(busy) || !canAnalyze} onClick={() => void toggleAnalysis(repo)}><i/></button></div>
      </>}
      <div className="repo-actions"><button className="button compact" aria-label={`${repo.name} 连接设置`} onClick={() => setConnectionEditor(repo)} disabled={Boolean(busy)}><Settings2 size={14}/>连接设置</button><button className="button compact" onClick={() => void operate(repo.id, 'sync')} disabled={Boolean(busy) || repo.status === 'pending' || repo.status === 'syncing'}><RefreshCw size={14} className={busy === `${repo.id}-sync` ? 'spin' : ''}/>同步变更</button>{!grouped && <button className="button compact accent" onClick={() => void operate(repo.id, 'analyze')} disabled={Boolean(busy) || analyzing || !canAnalyze || repo.status !== 'ready'}>{analyzing ? <span className="small-spinner"/> : <Sparkles size={14}/>} {analyzing ? '正在分析…' : '单仓分析'}</button>}</div>
    </article>;
  };
  return <div className="page-scroll"><div className="page-content repositories-content">
    <header className="page-heading"><div><h1>项目与仓库</h1><p>一个业务项目，连接前端、后端与各端代码，共同生成业务地图。</p></div><div className="project-heading-actions"><button data-tour="repository-add" className="button primary" onClick={() => setImportProject(true)}><Plus size={16}/>接入业务项目</button></div></header>
    <div className="info-strip"><GitBranch size={17}/><span>接入时分别填写前端和后端仓库，同一个项目可以继续加入小程序、管理端等代码。完整分析会清点全部可分析源码，逐模块处理后统一生成业务知识、地图与总结。</span></div>
    {policyError && <p className="form-error" role="alert">无法读取模型绑定：{policyError} <button className="text-button" onClick={() => setPolicyVersion(version => version + 1)}>重试</button></p>}
    {analysisError && <p className="form-error" role="alert">无法更新分析状态：{analysisError} <button className="text-button" onClick={() => setAnalysisVersion(version => version + 1)}>重试</button></p>}
    {projects.map(project => {
      const members = project.repository_ids.map(id => repositoryById.get(id)).filter((repo): repo is Repository => Boolean(repo));
      const policy = projectPolicies[project.id];
      const fullAnalysis = fullAnalysisStatuses[project.id];
      const analyzing = busy === `${project.id}-analyze` || (!fullAnalysis?.stale && ['planning', 'running'].includes(fullAnalysis?.status || ''));
      const canResume = fullAnalysis && !fullAnalysis.stale && (['paused', 'failed', 'cancelled'].includes(fullAnalysis.status) || fullAnalysis.publication_complete === false);
      const ready = members.length > 0 && members.length === project.repository_ids.length && members.every(repo => repo.status === 'ready');
      return <section className="business-project section-block" key={project.id} aria-label={`${project.name} 项目`}>
        <div className="project-section-heading"><div><h2><FolderOpen size={19}/>{project.name}<span className="badge">{members.length} 个仓库</span></h2>{project.description && <p>{project.description}</p>}</div><button className="button compact" onClick={() => editProject(project)} disabled={Boolean(busy)}><Settings2 size={14}/>编辑项目</button></div>
        <div className="project-analysis-controls"><div className="repo-model-binding"><div><strong>{policy?.status === 'ready' ? `${policy.provider_name || '已绑定模型'} · ${policy.model}` : '项目分析模型'}</strong><button className="text-button" disabled={Boolean(busy) || !members.length || analyzing} onClick={() => openBinding({ kind: 'project', id: project.id, name: project.name, count: members.length })}>{policy?.status === 'ready' ? '更改绑定' : '绑定项目模型'}</button></div><p>{policy?.status === 'ready' ? `${policy.base_url} · 适用于项目全部 ${members.length} 个仓库` : policy ? policyDescriptions[policy.status] : '正在读取模型绑定…'}</p></div><div className="full-analysis-actions"><button className="button primary" disabled={Boolean(busy) || analyzing || !ready || policy?.status !== 'ready'} title={!members.length ? '请先在编辑项目中加入仓库' : !ready ? '等待全部成员仓库同步完成' : policy?.status !== 'ready' ? '先绑定允许接收项目全部仓库代码的模型' : '逐批分析各端全部可分析源码，并复用同版本已完成批次'} onClick={() => fullAnalysis?.status === 'paused' && !fullAnalysis.stale ? void pauseFullAnalysis(project.id, false) : void operate(project.id, 'analyze', 'project')}>{analyzing ? <span className="small-spinner"/> : <Sparkles size={15}/>} {analyzing ? '正在完整分析…' : fullAnalysis?.stale ? '重新完整分析' : canResume ? '继续完整分析' : '完整分析项目'}</button>{analyzing && <button className="button" disabled={Boolean(busy)} onClick={() => void pauseFullAnalysis(project.id, true)}><Pause size={14}/>{busy === `${project.id}-pause` ? '正在请求暂停…' : '暂停分析'}</button>}</div></div>
        {!members.length && <p className="repository-notice">项目还没有仓库。编辑项目加入前端、后端等仓库后，才能分析。</p>}
        <ProjectAnalysisProgress name={project.name} analysis={fullAnalysis} preview={projectStatuses[project.id]} error={fullAnalysisErrors[project.id]} submitting={busy === `${project.id}-analyze`}/>
        {fullAnalysis?.receipt_migration && <p className="form-hint">已核验接续 {fullAnalysis.reused_chunks ?? 0} 个批次，保留 {fullAnalysis.receipt_migration.source_provider_name} 的原始结果与来源；后续批次使用 {fullAnalysis.receipt_migration.target_provider_name}。</p>}
        {fullAnalysisErrors[project.id] && <button className="text-button" onClick={() => setAnalysisVersion(version => version + 1)}>重新读取完整分析状态</button>}
        <div className="project-repositories">{(['frontend', 'backend'] as const).map(role => <div className="project-role-group" key={role}><h3>{role === 'frontend' ? '前端仓库' : '后端仓库'}</h3><div className="repo-grid">{members.filter(repo => (project.repository_roles?.[repo.id] || 'frontend') === role).map(repo => renderRepository(repo, true))}</div>{!members.some(repo => (project.repository_roles?.[repo.id] || 'frontend') === role) && <p className="form-hint">尚未加入{role === 'frontend' ? '前端' : '后端'}仓库，请编辑项目补充。</p>}</div>)}</div>
      </section>;
    })}
    {unassigned.length > 0 && <section className="section-block" aria-label="未归属项目的仓库"><div className="section-heading"><h2>未归属项目的仓库</h2><span className="muted">{unassigned.length} 个</span></div><p className="form-hint">在项目的「编辑项目」中加入这些仓库，可一起分析完整业务；也可单独分析。</p><div className="repo-grid">{unassigned.map(repo => renderRepository(repo))}</div></section>}
    {!projects.length && !repositories.length && <Empty title="连接你的第一个业务项目" detail="填写前端和后端仓库，按完整业务组织知识。"><button className="button" onClick={() => setImportProject(true)}><Plus size={15}/>接入业务项目</button></Empty>}
    <section className="section-block"><div className="section-heading"><h2>任务记录</h2><span className="muted">{tasks.length} 项</span></div>{tasks.length ? <div className="task-list">{tasks.slice(0, 15).map(task => <div className="task-row" key={task.id}><span className={`task-icon ${task.status}`}>{task.status === 'completed' ? <CheckCircle2 size={17}/> : task.status === 'failed' ? <AlertCircle size={17}/> : <RefreshCw size={16} className={task.status === 'running' ? 'spin' : ''}/>}</span><div><strong>{task.message || '项目任务'}</strong>{task.error ? <small className="danger">{task.error}</small> : <small>{formatTime(task.created_at)}</small>}{['running', 'queued'].includes(task.status) && <progress max={100} value={task.progress}/>}</div><span className="muted">{{ completed: '已完成', failed: '失败', running: '进行中', queued: '等待中' }[task.status]}</span></div>)}</div> : <p className="subtle-empty">仓库导入、同步与项目分析任务会显示在这里。</p>}</section>
    <details className="repository-advanced project-advanced"><summary>高级管理 · 追加单个仓库与整理已有仓库</summary><div className="project-advanced-actions"><button className="button compact" onClick={() => setConnectionEditor('new')}><Plus size={14}/>追加单个仓库</button><button className="button compact" onClick={() => editProject()}><FolderOpen size={14}/>用已有仓库组成项目</button><p className="form-hint">追加后，在编辑项目中设置前端或后端归属。</p></div></details>
    {importProject && <ProjectConnectionForm onClose={() => setImportProject(false)} onSaved={refresh} notify={notify}/>}
    <CandidateReview notify={notify} refresh={refresh} reloadVersion={candidateVersion}/>
    {connectionEditor && <RepositoryConnectionForm repository={connectionEditor === 'new' ? undefined : connectionEditor} draft={connectionEditor === 'new' ? connectionDraft : undefined} onDraftChange={connectionEditor === 'new' ? setConnectionDraft : undefined} onClose={() => setConnectionEditor(null)} onSaved={() => { if (connectionEditor === 'new') setConnectionDraft(undefined); refresh(); }} notify={notify}/>}
    {projectDraft && <Modal title={projectDraft.project ? '编辑业务项目' : '新建业务项目'} busy={busy === 'project'} onClose={() => setProjectDraft(null)}><form className="form-stack" onSubmit={saveProject}>
      <label>项目名称<input autoFocus required maxLength={100} disabled={busy === 'project'} value={projectDraft.name} onChange={event => setProjectDraft(current => current && ({ ...current, name: event.target.value }))} placeholder="例如：天梯赛事"/></label>
      <label>业务说明 <span className="muted">（可选）</span><textarea maxLength={2000} rows={3} disabled={busy === 'project'} value={projectDraft.description} onChange={event => setProjectDraft(current => current && ({ ...current, description: event.target.value }))} placeholder="描述这些仓库共同服务的业务"/></label>
      <fieldset className="project-member-fieldset" disabled={busy === 'project'}><legend>项目仓库</legend><p className="form-hint">选择同一业务的前端、后端和其他客户端。一个仓库只能归属一个项目。</p><div className="project-member-options">{repositories.filter(repo => !projectByRepository.has(repo.id) || projectByRepository.get(repo.id)?.id === projectDraft.project?.id).map(repo => <div className="project-member-option" key={repo.id}><label><input type="checkbox" checked={projectDraft.repository_ids.includes(repo.id)} onChange={event => { const checked = event.target.checked; setProjectDraft(current => current && ({ ...current, repository_ids: checked ? [...current.repository_ids, repo.id] : current.repository_ids.filter(id => id !== repo.id) })); }}/><span><strong>{repo.name}</strong><small>{repo.branch || '默认分支'} · {repo.commit?.slice(0, 8) || '等待索引'}</small></span></label><select aria-label={`${repo.name} 仓库角色`} disabled={!projectDraft.repository_ids.includes(repo.id) || busy === 'project'} value={projectDraft.repository_roles[repo.id] || 'frontend'} onChange={event => { const role = event.target.value as 'frontend' | 'backend'; setProjectDraft(current => current && ({ ...current, repository_roles: { ...current.repository_roles, [repo.id]: role } })); }}><option value="frontend">前端</option><option value="backend">后端</option></select></div>)}</div>{!repositories.some(repo => !projectByRepository.has(repo.id) || projectByRepository.get(repo.id)?.id === projectDraft.project?.id) && <p className="repository-notice">没有可加入的仓库。请先接入仓库，或从原项目中移出后再加入。</p>}</fieldset>
      <p className="form-hint">保存项目不会调用模型。调整成员后需要重新核对项目模型绑定。</p>{projectError && <p role="alert" className="form-error">{projectError}</p>}<div className="modal-actions"><button type="button" className="button" disabled={busy === 'project'} onClick={() => setProjectDraft(null)}>取消</button><button className="button primary" disabled={busy === 'project' || !projectDraft.name.trim()}>{busy === 'project' ? '保存中…' : '保存项目'}</button></div>
    </form></Modal>}
    {binding && <Modal title={binding.kind === 'project' ? '绑定项目分析模型' : '绑定仓库分析模型'} busy={busy === 'binding'} onClose={() => setBinding(null)}><form className="form-stack" onSubmit={bindModel}>
      <p className="form-hint">为「{binding.name}」选择允许接收代码片段的模型。{binding.kind === 'project' ? `绑定适用于项目内全部 ${binding.count} 个仓库，包括前端、后端等成员。` : ''}全局默认模型切换不会改变此绑定。</p>
      <label>分析模型<select aria-label="分析模型" autoFocus required disabled={busy === 'binding'} value={providerId} onChange={event => setProviderId(event.target.value)}><option value="">请选择模型</option>{providers.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}</select></label>
      {provider && <div className="repo-provider-preview"><strong>{provider.name} · {provider.model === 'default' && provider.kind === 'codex-local' ? 'Codex CLI 内置默认模型' : provider.model}</strong><p>{provider.kind === 'codex-local' ? '通过本机 Codex CLI 调用已登录账号，代码片段会通过 Codex 发送给 OpenAI，并消耗 Codex 额度。' : `代码发送至：${provider.base_url}`}</p><p>绑定本身不会发送新模型请求。服务地址、型号或项目成员变更后需重新绑定。</p></div>}
      {canReuseCompleted && <div>
        <label className="model-checkpoint-option"><input type="checkbox" checked={reuseCompleted} disabled={busy === 'binding'} onChange={event => setReuseCompleted(event.target.checked)}/>核验并接续已完成结果</label>
        <p className="form-hint">已有 {bindingAnalysis?.coverage?.completed_chunks ?? 0} 个批次。核验版本与输入后，保留原模型的结果和来源，仅让新模型分析未完成批次；本次操作不发送模型请求。</p>
        {!reuseCompleted && <p className="form-hint">本次仅更改绑定，不接续已有断点。旧结果仍保留，后续完整分析将按新模型重新建立任务。</p>}
      </div>}
      {!providers.length && <p className="repository-notice">请先在「模型配置」添加模型。</p>}{bindingError && <p role="alert" className="form-error">{bindingError}</p>}
      <div className="modal-actions"><button className="button" type="button" disabled={busy === 'binding'} onClick={() => setBinding(null)}>取消</button><button className="button primary" disabled={busy === 'binding' || !provider}>{busy === 'binding' ? canReuseCompleted && reuseCompleted ? '正在核验并接续…' : '正在绑定…' : canReuseCompleted && reuseCompleted ? '核验并绑定新模型' : binding.kind === 'project' ? '为全部成员绑定模型' : '绑定模型'}</button></div>
    </form></Modal>}
  </div></div>;
}
