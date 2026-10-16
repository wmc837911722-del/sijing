import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, FileDiff, FileText } from 'lucide-react';
import { errorMessage, get, post } from '../api';
import type { Note, Notify } from '../types';
import { Busy, ErrorBox, Modal } from './UI';
type Candidate = { id: string; repository_id: string; path: string; content: string; current_revision: string | null; commit: string; source_paths: string[] };

export default function CandidateReview({ notify, refresh, reloadVersion = 0 }: { notify: Notify; refresh: () => void; reloadVersion?: number }) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Candidate | null>(null);
  const [current, setCurrent] = useState<Note | null>(null);
  const [readError, setReadError] = useState('');
  const [reading, setReading] = useState(false);
  const [applying, setApplying] = useState(false);
  const load = useCallback(async () => { try { const data = await get<{ candidates: Candidate[] }>('/api/analysis/candidates'); setCandidates(data.candidates); setError(''); } catch (err) { setError(errorMessage(err)); } }, []);
  useEffect(() => { void load(); const timer = setInterval(() => void load(), 12000); return () => clearInterval(timer); }, [load, reloadVersion]);
  const review = async (candidate: Candidate) => {
    setSelected(candidate); setCurrent(null); setReadError(''); setReading(true);
    try { setCurrent(await get<Note>(`/api/note?path=${encodeURIComponent(candidate.path)}`)); }
    catch (err) { if (candidate.current_revision !== null) setReadError(errorMessage(err)); }
    finally { setReading(false); }
  };
  const apply = async () => {
    if (!selected || reading || readError) return;
    setApplying(true);
    try { await post<Note>(`/api/analysis/candidates/${selected.id}/apply`, { revision: current?.revision || null }); setSelected(null); notify('已应用审阅后的业务知识'); await load(); refresh(); }
    catch (err) { setReadError(errorMessage(err)); }
    finally { setApplying(false); }
  };
  const copy = async () => { if (!selected) return; try { await navigator.clipboard.writeText(selected.content); notify('建议内容已复制，可到笔记编辑器手动合并'); } catch (err) { notify(errorMessage(err), 'error'); } };
  if (!candidates.length && !error) return null;
  return <section className="section-block"><div className="section-heading"><h2><FileDiff size={16}/>待审阅的业务更新</h2><span className="badge amber">{candidates.length} 项</span></div><p className="form-hint">代码变更与人工维护的知识存在差异。以下建议在你确认前不会进入业务索引。</p>{error && <ErrorBox error={error}/>}<div className="candidate-list">{candidates.map(candidate => <div className="candidate-row" key={candidate.id}><FileText size={17}/><div><strong>{candidate.path}</strong><small>代码版本 {candidate.commit?.slice(0, 8) || '未记录'}</small></div><button className="button compact" onClick={() => void review(candidate)}>审阅变更</button></div>)}</div>
    {selected && <Modal title="审阅业务知识更新" busy={applying} wide onClose={() => { if (!applying) setSelected(null); }}><div className="review-content"><div className="review-path">{selected.path}</div>{reading ? <Busy text="正在读取当前笔记…"/> : <>{readError && <ErrorBox error={readError}/>}<div className="review-columns"><label>当前笔记 <span>包含你的人工修改</span><textarea readOnly aria-label="当前笔记内容" value={current?.content || '（尚无此笔记）'} spellCheck={false}/></label><label>AI 建议版本 <span>尚未纳入知识库</span><textarea readOnly aria-label="AI 建议内容" value={selected.content} spellCheck={false}/></label></div><p className="form-hint">应用建议将替换当前笔记内容。需要保留部分人工说明时，请复制建议并在笔记编辑器中手动合并。</p><div className="modal-actions"><button className="button" onClick={() => void copy()}><Copy size={13}/>复制建议</button><button className="button primary" disabled={applying || Boolean(readError)} onClick={() => void apply()}><Check size={14}/>{applying ? '应用中…' : '以建议版本更新笔记'}</button></div></>}</div></Modal>}
  </section>;
}
