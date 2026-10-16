import type { AnalysisStatus, FullAnalysisStatus } from '../types';

const statusLabels: Record<FullAnalysisStatus['status'], string> = {
  never: '尚未完整分析', ready: '已规划，待完整分析', planning: '正在规划完整分析', running: '正在完整分析', paused: '完整分析已暂停', failed: '完整分析未完成', blocked: '完整分析被阻止', cancelled: '完整分析已停止', completed: '完整分析完成',
};
const moduleStatusLabels: Record<string, string> = { pending: '待分析', running: '分析中', completed: '已完成', failed: '未完成' };

export default function ProjectAnalysisProgress({ name, analysis, preview, error, submitting }: {
  name: string; analysis?: FullAnalysisStatus; preview?: AnalysisStatus; error?: string; submitting: boolean;
}) {
  const coverage = analysis?.coverage;
  const never = analysis?.status === 'never';
  const hasPreview = never && preview && preview.status !== 'never';
  const complete = analysis?.status === 'completed';
  const stale = analysis?.stale === true;
  const pendingPublication = complete && analysis.publication_complete === false;
  const invalidCompletion = complete && (!coverage || coverage.analyzed_files !== coverage.eligible_files || coverage.completed_chunks !== coverage.total_chunks || coverage.completed_modules !== coverage.total_modules);
  const state = error || invalidCompletion || stale ? 'failed' : submitting && (!analysis || never) ? 'planning' : analysis?.status;
  const modules = analysis?.modules || [];
  const previewFiles = Array.isArray(preview?.scope?.files) ? preview.scope.files.length : preview?.scope?.files;
  return <div className={`repo-analysis-status project-full-analysis ${state || ''}`} aria-label={`${name} 完整分析状态`}>
    <div><strong>{error ? '无法确认完整分析状态' : stale ? '代码或授权已变化，需重新完整分析' : invalidCompletion ? '完整性校验未通过' : pendingPublication ? '源码分析完成，文档待合并' : state ? statusLabels[state] : '正在读取完整分析状态…'}</strong></div>
    {error && <p role="alert">{error}。读取成功前无法确认当前覆盖范围，请重试。</p>}
    {invalidCompletion && <p role="alert">服务返回的完成状态与覆盖数量不一致，请重新读取或继续分析。</p>}
    {!error && !invalidCompletion && <p>{stale ? '上次覆盖属于旧代码或旧授权范围，不计作当前版本已完成。请重新完整分析。' : pendingPublication ? `源码与业务归纳已完成，但${analysis.conflicts?.length ? ` ${analysis.conflicts.length} 篇` : ''}文档仍有冲突。请在更新建议中核对并合并，当前业务地图尚未全部更新。` : never ? hasPreview ? '尚未完整分析，上次结果为局部预览。开始完整分析后，会逐批读取各仓库的全部可分析源码。' : '完整分析将清点项目各端源码，逐模块分批处理，全部批次完成后生成统一业务地图和总结。' : analysis?.summary || (state === 'planning' ? '正在清点各端源码和分析批次。' : '完整分析的覆盖范围与模块进度会显示在这里。')}</p>}
    {coverage && !never && !stale && <>
      <dl className="full-analysis-coverage" aria-label="源码覆盖进度">
        <div><dt>已分析文件</dt><dd>{coverage.analyzed_files.toLocaleString()} <span>/ {coverage.eligible_files.toLocaleString()}</span></dd></div>
        <div><dt>已完成批次</dt><dd>{coverage.completed_chunks.toLocaleString()} <span>/ {coverage.total_chunks.toLocaleString()}</span></dd></div>
        <div><dt>已完成模块</dt><dd>{coverage.completed_modules.toLocaleString()} <span>/ {coverage.total_modules.toLocaleString()}</span></dd></div>
      </dl>
      {coverage.total_chunks > 0 && <progress aria-label="完整分析批次进度" max={coverage.total_chunks} value={coverage.completed_chunks}/>}
      <p className="repo-analysis-scope">可分析源码 {coverage.eligible_files.toLocaleString()} 个文件；另有 {coverage.excluded_files.toLocaleString()} 个文件因安全或文件类型规则排除，未计为已分析。</p>
      <p>同一代码版本会复用已完成批次，暂停或失败后可继续。源码覆盖完成后，业务结论仍需核对。</p>
    </>}
    {analysis?.error && !error && <p className="form-error" role="alert">{analysis.error}</p>}
    {hasPreview && <details className="analysis-preview"><summary>查看上次局部预览记录</summary><p>{preview.summary || '上次预览尚未完成。'}</p>{preview.scope && <p>历史预览已读 {previewFiles ?? 0}{preview.scope.total_files !== undefined ? ` / ${preview.scope.total_files}` : ''} 个文件 · {preview.scope.line_count} 行。此记录不代表完整分析。</p>}</details>}
    {modules.length > 0 && <details className="full-analysis-modules"><summary>{stale ? '上次分析模块（旧版本）' : '全部分析模块'} · {modules.length.toLocaleString()} 项</summary><ul>{modules.map(module => <li key={module.id}><span><strong>{module.name}</strong><small>{module.file_count.toLocaleString()} 个文件 · {module.repository_ids.length} 个仓库</small></span><span className={`analysis-module-state ${module.status}`}>{moduleStatusLabels[module.status] || module.status}</span></li>)}</ul></details>}
  </div>;
}
