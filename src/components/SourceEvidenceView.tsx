import { useEffect, useState } from 'react';
import { errorMessage, get } from '../api';
import type { SourceEvidence } from '../types';
import { Busy, ErrorBox, Modal } from './UI';
import './source-evidence.css';

export default function SourceEvidenceView({ reference, onClose }: { reference: string; onClose: () => void }) {
  const [pageReference, setPageReference] = useState(reference);
  const [evidence, setEvidence] = useState<SourceEvidence | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [retryVersion, setRetryVersion] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setError(''); setEvidence(null);
    void get<SourceEvidence>(`/api/source-evidence?reference=${encodeURIComponent(pageReference)}`)
      .then(result => { if (!cancelled) setEvidence(result); })
      .catch(reason => { if (!cancelled) setError(errorMessage(reason)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [pageReference, retryVersion]);
  const nextPage = () => {
    if (!evidence?.next_line || evidence.partial_line) return;
    const end = Math.min(evidence.total_lines, evidence.next_line + 199);
    setPageReference(`${reference.split('#')[0]}#L${evidence.next_line}-L${end}`);
  };
  return <Modal title="源码依据" wide onClose={onClose}><div className="source-evidence-view">
    {loading ? <Busy text="正在读取引用的源码…"/> : error ? <ErrorBox error={error} retry={() => setRetryVersion(version => version + 1)}/> : evidence ? <>
      <div className="source-evidence-heading"><strong>{evidence.path.split('/').at(-1)}</strong><span className="badge">只读</span></div>
      <p className="source-evidence-path">{evidence.path}</p>
      <dl className="source-evidence-meta"><dt>提交</dt><dd><code>{evidence.commit}</code></dd><dt>当前范围</dt><dd>L{evidence.line_start}–L{evidence.line_end} · 文件共 {evidence.total_lines} 行</dd></dl>
      <pre className="source-evidence-code" tabIndex={0} aria-label={`${evidence.path} 源码 L${evidence.line_start} 至 L${evidence.line_end}`}><code>{evidence.content || '此文件没有文本内容。'}</code></pre>
      {evidence.truncated && <p className="form-hint">{evidence.partial_line ? '当前行超过显示长度，已截断。' : '当前片段尚未完整显示，可继续读取后续行。'}</p>}
      <div className="source-evidence-actions"><span>来自已同步的 Git 快照</span>{evidence.next_line && !evidence.partial_line ? <button type="button" className="button compact" onClick={nextPage}>继续读取 L{evidence.next_line} 起</button> : null}</div>
    </> : null}
  </div></Modal>;
}
