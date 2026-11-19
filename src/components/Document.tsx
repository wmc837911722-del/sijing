import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { BookOpen, FileText, Save, Pencil, Link2, Clock3, Check } from 'lucide-react';
import { get, request, errorMessage, formatTime, noteTypeLabel } from '../api';
import type { Note, NoteSummary, Notify } from '../types';
import { MarkdownView, stripFrontmatter } from './MarkdownView';
import { Busy, Empty, ErrorBox } from './UI';
const MarkdownEditor = lazy(() => import('./MarkdownEditor'));

export default function Document({ path, notes, onOpen, onNote, onDirty, notify, onSaved }: { path: string | null; notes: NoteSummary[]; onOpen: (path: string) => void; onNote: (note: Note | null) => void; onDirty: (dirty: boolean) => void; notify: Notify; onSaved: () => void }) {
  const [note, setNote] = useState<Note | null>(null);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const dirty = Boolean(note && draft !== note.content);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const savingRef = useRef(false);
  const readSequence = useRef(0);
  const documentEpoch = useRef(0);
  const draftRef = useRef(draft); draftRef.current = draft;
  const revisionRef = useRef(note?.revision); revisionRef.current = note?.revision;
  const onNoteRef = useRef(onNote); onNoteRef.current = onNote;
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => { if (dirtyRef.current) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, []);
  useEffect(() => {
    documentEpoch.current += 1;
    let cancelled = false;
    savingRef.current = false; setSaving(false);
    setNote(null); onNoteRef.current(null); setEditing(false); setError('');
    setLoading(Boolean(path));
    if (!path) return;
    const read = async (initial: boolean) => {
      const sequence = ++readSequence.current;
      try {
        const next = await get<Note>(`/api/note?path=${encodeURIComponent(path)}`);
        if (cancelled || sequence !== readSequence.current || (!initial && (dirtyRef.current || savingRef.current))) return;
        setError('');
        if (initial || next.revision !== revisionRef.current) { setNote(next); setDraft(next.content); onNoteRef.current(next); }
      } catch (err) { if (!cancelled && sequence === readSequence.current) setError(initial ? errorMessage(err) : `检查外部文件更新失败：${errorMessage(err)}`); }
      finally { if (!cancelled && initial) setLoading(false); }
    };
    void read(true);
    const timer = setInterval(() => { if (!dirtyRef.current && !savingRef.current) void read(false); }, 12000);
    return () => { cancelled = true; documentEpoch.current += 1; clearInterval(timer); };
  }, [path]);
  const save = async () => {
    if (!note || savingRef.current || !dirty) return;
    const epoch = documentEpoch.current;
    const submitted = draft;
    savingRef.current = true; readSequence.current += 1; setSaving(true); setError('');
    try {
      const saved = await request<Note>('PUT', '/api/note', { path: note.path, content: submitted, revision: note.revision });
      onSaved();
      if (epoch !== documentEpoch.current) return;
      setNote(saved);
      setDraft(current => current === submitted ? saved.content : current);
      onNote(saved);
      notify(draftRef.current === submitted ? '笔记已保存' : '提交的内容已保存，后续修改仍待保存');
    }
    catch (err) { if (epoch === documentEpoch.current) setError(errorMessage(err)); }
    finally { if (epoch === documentEpoch.current) { savingRef.current = false; setSaving(false); } }
  };
  const saveRef = useRef(save); saveRef.current = save;
  useEffect(() => { const handler = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key === 's') { event.preventDefault(); if (document.querySelector('dialog[open]')) return; void saveRef.current(); } }; window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler); }, []);
  if (loading) return <Busy text="正在打开笔记…"/>;
  if (!path) return <Empty title="从一条知识，连接整个业务" detail="选择左侧笔记开始阅读，或接入代码仓库建立你的业务知识库。"/>;
  if (!note) return <div className="page-pad"><ErrorBox error={error || '这篇笔记尚不存在，请先在知识库中创建。'}/></div>;
  const headings = stripFrontmatter(draft).match(/^#{1,3} .+$/gm) || [];
  return <div className="document-view">
    <div className="document-toolbar"><div className="breadcrumb"><FileText size={14}/><span>{note.path.split('/').slice(0, -1).join(' / ') || '知识库'}</span><span className="slash">/</span><strong>{note.title}</strong></div><div className="toolbar-actions"><button className={`icon-button ${!editing ? 'selected' : ''}`} aria-label="阅读模式" title="阅读" onClick={() => setEditing(false)}><BookOpen size={16}/></button><button className={`icon-button ${editing ? 'selected' : ''}`} aria-label="编辑文档" title="编辑文档" onClick={() => setEditing(true)}><Pencil size={15}/></button><span className="toolbar-divider"/><button className="button compact primary" onClick={() => void save()} disabled={!dirty || saving}>{saving ? <span className="small-spinner"/> : dirty ? <Save size={14}/> : <Check size={14}/>} {saving ? '保存中' : dirty ? '保存' : '已保存'}</button></div></div>
    {error && <div className="inline-error"><ErrorBox error={error}/></div>}
    {editing ? <Suspense fallback={<Busy/>}><MarkdownEditor value={draft} onChange={setDraft}/></Suspense> : <div className="document-scroll"><article className="note-article" data-note-path={note.path}><div className="note-topline"><span className="type-label"><BookOpen size={13}/>{noteTypeLabel(note.type)}</span>{note.tags.includes('示例') || note.tags.includes('demo') ? <span className="badge amber">示例内容</span> : null}</div><div className="note-meta"><span><Clock3 size={12}/>{formatTime(note.updated_at)}</span><span><Link2 size={12}/>{note.links.length} 个关联</span><span><FileText size={12}/>业务文档</span></div><MarkdownView content={draft} notes={notes} currentPath={note.path} onOpen={onOpen}/>{note.links.length > 0 && <section className="related-section"><div className="section-kicker">继续探索</div><div className="related-grid">{note.links.map(link => <button key={link.id} className="related-note" onClick={() => onOpen(link.path)}><FileText size={16}/><span>{link.title}<small>{noteTypeLabel(link.type)}</small></span><Link2 size={13}/></button>)}</div></section>}<div className="note-end"><span/>文档结束<span/></div></article></div>}
    <div className="document-bottom"><span>{dirty ? '有未保存的更改' : '文件与知识库同步'}{editing ? ' · Ctrl / ⌘ S 保存' : ''}</span><span>{draft.length.toLocaleString()} 字符 · {headings.length} 个章节</span></div>
  </div>;
}
