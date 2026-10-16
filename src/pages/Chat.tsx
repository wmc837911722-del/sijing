import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, ArrowUp, BookOpen, Check, ChevronDown, Database, FileBarChart2, FileText, GitBranch, LoaderCircle, MessageSquare, PanelLeftClose, PanelLeftOpen, Plus, Search, Settings2, Sparkles, Square, TriangleAlert } from 'lucide-react';
import { errorMessage, formatTime, get, post } from '../api';
import { MarkdownView } from '../components/MarkdownView';
import { ResultsTable } from '../components/UI';
import { BrandMark } from '../components/BrandMark';
import type { Datasource, NoteSummary, Notify, QueryResult, Report, Repository, RepositoryModelPolicy, Settings } from '../types';
import '../chat.css';

type Mode = 'knowledge' | 'data';
type ChatActivity = { id: string; tool: string; label: string; status: 'running' | 'complete' | 'error'; path?: string; detail?: string };
type ChatMessage = { id: string; role: 'user' | 'assistant'; content: string; sources?: { path: string; title: string; revision?: string }[]; result?: QueryResult; provider?: string; error?: string | boolean; activities?: ChatActivity[]; completion?: 'complete' | 'limited' };
type ChatSession = { id: string; title: string; created_at: string; updated_at: string; messages: ChatMessage[]; mode: Mode; provider_id?: string | null; repository_id?: string | null; datasource_id?: string | null; pending?: boolean; pending_activity?: ChatActivity[]; cancel_requested?: boolean; earlier_messages_removed?: number };
type ChatSummary = Pick<ChatSession, 'id' | 'title' | 'created_at' | 'updated_at' | 'pending'>;
type Selection = { mode: Mode; provider_id: string; repository_id: string; datasource_id: string };
type Failure = { message: string; question: string; saved: boolean };
type Props = { settings: Settings | null; repositories: Repository[]; datasources: Datasource[]; notes: NoteSummary[]; businessNoteCount: number; notify: Notify; onOpen: (path: string) => void; onReport: () => void; onConfigure: () => void; onRepositories: () => void; refresh: () => void };

const NEW_CHAT = '__new__';
const MESSAGE_LIMIT = 8000;
const DEFAULT_SELECTION: Selection = { mode: 'knowledge', provider_id: '', repository_id: '', datasource_id: '' };
const SUGGESTIONS = {
  knowledge: [ ['梳理业务', '这个系统有哪些核心业务流程？'], ['理解规则', '订单退款需要满足哪些条件？'], ['追溯代码', '订单状态如何流转，对应哪些代码？'] ],
  data: [ ['汇总数据', '按渠道统计订单数量和金额'], ['分析趋势', '最近 7 天每天的订单数量是多少？'], ['查看分布', '各订单状态分别有多少条记录？'] ],
};

function selectionFrom(session: ChatSession): Selection {
  return { mode: session.mode || 'knowledge', provider_id: session.provider_id || '', repository_id: session.repository_id || '', datasource_id: session.datasource_id || '' };
}

function mergeHistory(current: ChatSummary[], incoming: ChatSummary[]): ChatSummary[] {
  const entries = new Map(current.map(item => [item.id, item]));
  for (const item of incoming) {
    const existing = entries.get(item.id);
    if (!existing || item.updated_at >= existing.updated_at) entries.set(item.id, item);
  }
  return [...entries.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

function ExplorationActivity({ activities, pending = false, limited = false, onOpen }: { activities: ChatActivity[]; pending?: boolean; limited?: boolean; onOpen: (path: string) => void }) {
  return <details className="bm-chat-exploration" open={pending || undefined}>
    <summary><ChevronDown size={14}/><span>{pending ? '正在探索业务知识' : limited ? '探索记录 · 未完成' : '探索记录'}</span><small>{activities.length} 个步骤</small></summary>
    <ol>{activities.map(activity => <li key={activity.id}>
      <span className="bm-chat-activity-status" aria-label={activity.status === 'running' ? '进行中' : activity.status === 'error' ? '未完成' : '已完成'}>{activity.status === 'running' ? <LoaderCircle size={14} className="bm-chat-spin"/> : activity.status === 'error' ? <TriangleAlert size={14}/> : <Check size={14}/>}</span>
      <div><span>{activity.label}</span>{activity.path && <button type="button" title={`打开笔记：${activity.path}`} onClick={() => onOpen(activity.path!)}><FileText size={12}/>{activity.path}</button>}{activity.detail && <p>{activity.detail}</p>}</div>
    </li>)}</ol>
  </details>;
}

function QueryCard({ result, title, notify, onReport, refresh }: { result: QueryResult; title: string; notify: Notify; onReport: () => void; refresh: () => void }) {
  const [formOpen, setFormOpen] = useState(false);
  const [reportTitle, setReportTitle] = useState(title || '业务查询报表');
  const [chartType, setChartType] = useState<Report['chart_type']>('table');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const savingRef = useRef(false);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (savingRef.current || !reportTitle.trim()) return;
    savingRef.current = true;
    setSaving(true); setError('');
    try {
      await post<Report>('/api/reports', { result_id: result.id, title: reportTitle.trim(), chart_type: chartType });
      setSaved(true); setFormOpen(false); notify('报表已保存'); refresh();
    } catch (err) { setError(errorMessage(err)); }
    finally { savingRef.current = false; setSaving(false); }
  };
  return <section className="bm-chat-result" aria-label="只读查询结果">
    <div className="bm-chat-result-heading"><span><Database size={15}/>查询结果</span><small>{result.row_count.toLocaleString()} 行 · {Math.round(result.elapsed_ms)} ms{result.truncated ? ' · 已截断' : ''}</small></div>
    <ResultsTable result={result} compact/>
    <details className="bm-chat-sql"><summary><ChevronDown size={14}/>查看执行的 SQL</summary><pre>{result.sql}</pre><p>查询时间：{formatTime(result.executed_at)} · 数据源：{result.datasource_id}</p></details>
    <div className="bm-chat-result-actions"><span>数据库只读查询{result.datasource_id === 'demo' ? ' · 示例数据' : ''}</span>{saved ? <button type="button" className="bm-chat-button" onClick={onReport}><Check size={14}/>已保存 · 查看报表<ArrowRight size={14}/></button> : <button type="button" className="bm-chat-button" onClick={() => setFormOpen(value => !value)} aria-expanded={formOpen}><FileBarChart2 size={14}/>保存为报表</button>}</div>
    {formOpen && <form className="bm-chat-report-form" onSubmit={event => void save(event)}>
      <label>报表名称<input value={reportTitle} maxLength={200} onChange={event => setReportTitle(event.target.value)} required disabled={saving}/></label>
      <label>呈现方式<select value={chartType} onChange={event => setChartType(event.target.value as Report['chart_type'])} disabled={saving}><option value="table">数据表</option><option value="bar">柱状图</option><option value="line">折线图</option></select></label>
      <button type="submit" className="bm-chat-button bm-chat-primary" disabled={saving || !reportTitle.trim()}>{saving ? <LoaderCircle size={14} className="bm-chat-spin"/> : <Check size={14}/>}保存报表</button>
      {error && <p className="bm-chat-inline-error" role="alert">{error}</p>}
    </form>}
  </section>;
}

export default function Chat({ settings, repositories, datasources, notes, businessNoteCount, notify, onOpen, onReport, onConfigure, onRepositories, refresh }: Props) {
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [sessions, setSessions] = useState<Record<string, ChatSession>>({});
  const [activeId, setActiveId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [selections, setSelections] = useState<Record<string, Selection>>({});
  const [pending, setPending] = useState<Record<string, string>>({});
  const [canceling, setCanceling] = useState<Record<string, boolean>>({});
  const [failures, setFailures] = useState<Record<string, Failure | undefined>>({});
  const [loadingIds, setLoadingIds] = useState<Record<string, boolean>>({});
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [repositoryPolicy, setRepositoryPolicy] = useState<{ id: string; policy?: RepositoryModelPolicy; error?: string } | null>(null);
  const sendLocks = useRef(new Set<string>());
  const cancelLocks = useRef(new Set<string>());
  const submittedIds = useRef<Record<string, Set<string>>>({});
  const loadLocks = useRef(new Set<string>());
  const creatingRef = useRef(false);
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const nearBottom = useRef(true);
  const key = activeId || NEW_CHAT;
  const session = activeId ? sessions[activeId] : undefined;
  const selection = selections[key] || (session ? selectionFrom(session) : DEFAULT_SELECTION);
  const text = drafts[key] || '';
  const messageLength = Array.from(text.trim()).length;
  const exceedsLimit = messageLength > MESSAGE_LIMIT;
  const localPending = pending[key] !== undefined;
  const busy = localPending || Boolean(session?.pending);
  const stopRequested = Boolean(canceling[key] || session?.cancel_requested);
  const loading = Boolean(activeId && loadingIds[activeId]);
  const failure = failures[key];
  const messages = session?.messages || [];
  const submittedQuestionSaved = localPending && messages.some(message => message.role === 'user' && message.content === pending[key] && !submittedIds.current[key]?.has(message.id));
  const pendingActivities = session?.pending ? session.pending_activity || [] : [];
  const effectiveProviderId = selection.provider_id || settings?.active_provider_id;
  const provider = settings?.providers.find(item => item.id === effectiveProviderId);
  const repository = repositories.find(item => item.id === selection.repository_id);
  const currentPolicy = repositoryPolicy?.id === selection.repository_id ? repositoryPolicy : null;
  const policy = currentPolicy?.policy;
  const policyMismatch = Boolean(provider && selection.repository_id && (policy?.status !== 'ready' || policy.provider_id !== effectiveProviderId));
  const filteredChats = chats.filter(chat => chat.title.toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (!selection.repository_id) { setRepositoryPolicy(null); return; }
    let live = true;
    const id = selection.repository_id;
    void get<RepositoryModelPolicy>(`/api/repositories/${encodeURIComponent(id)}/model-policy`)
      .then(value => { if (live) setRepositoryPolicy({ id, policy: value }); })
      .catch(err => { if (live) setRepositoryPolicy({ id, error: errorMessage(err) }); });
    return () => { live = false; };
  }, [selection.repository_id, repositories, settings?.providers]);

  useEffect(() => {
    let live = true;
    void get<{ chats: ChatSummary[] }>('/api/chats').then(data => { if (live) setChats(previous => mergeHistory(previous, data.chats)); }).catch(err => { if (live) setHistoryError(errorMessage(err)); }).finally(() => { if (live) setHistoryLoading(false); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    nearBottom.current = true;
    list.current?.scrollTo({ top: messages.length || busy || failure ? list.current.scrollHeight : 0 });
  }, [activeId]);

  useEffect(() => {
    if (nearBottom.current) list.current?.scrollTo({ top: messages.length || busy || failure ? list.current.scrollHeight : 0 });
  }, [messages.length, busy, loading, failure, pendingActivities.length]);

  const remember = (value: ChatSession) => {
    setSessions(previous => previous[value.id]?.updated_at > value.updated_at ? previous : { ...previous, [value.id]: value });
    setChats(previous => mergeHistory(previous, [value]));
  };
  useEffect(() => {
    if (!activeId || !busy) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await get<ChatSession>(`/api/chats/${encodeURIComponent(activeId)}`);
        if (live) { remember(value); setFailures(previous => ({ ...previous, [activeId]: undefined })); }
      } catch (err) {
        if (live) setFailures(previous => ({ ...previous, [activeId]: { message: errorMessage(err), question: '', saved: true } }));
      } finally { if (live) timer = setTimeout(() => void poll(), 1800); }
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { live = false; clearTimeout(timer); };
  }, [activeId, busy]);
  const updateSelection = (change: Partial<Selection>) => setSelections(previous => ({ ...previous, [key]: { ...selection, ...change } }));
  const setDraft = (value: string) => setDrafts(previous => ({ ...previous, [key]: value }));

  const retryHistory = async () => {
    setHistoryLoading(true); setHistoryError('');
    try { const data = await get<{ chats: ChatSummary[] }>('/api/chats'); setChats(previous => mergeHistory(previous, data.chats)); }
    catch (err) { setHistoryError(errorMessage(err)); }
    finally { setHistoryLoading(false); }
  };

  const openChat = async (id: string, force = false) => {
    if (creatingRef.current) return;
    setActiveId(id);
    if ((!force && sessions[id]) || loadLocks.current.has(id) || sendLocks.current.has(id)) return;
    loadLocks.current.add(id); setLoadingIds(previous => ({ ...previous, [id]: true }));
    try {
      const value = await get<ChatSession>(`/api/chats/${encodeURIComponent(id)}`);
      remember(value);
      setFailures(previous => {
        const failure = previous[id];
        const stillUnsent = failure?.question && !failure.saved && !value.messages.some(message => message.role === 'user' && message.content === failure.question);
        return stillUnsent ? previous : { ...previous, [id]: undefined };
      });
    } catch (err) { setFailures(previous => ({ ...previous, [id]: { message: errorMessage(err), question: previous[id]?.question || '', saved: previous[id]?.saved || false } })); }
    finally { loadLocks.current.delete(id); setLoadingIds(previous => ({ ...previous, [id]: false })); }
  };

  const newChat = () => {
    if (creatingRef.current) return;
    if (activeId && !drafts[NEW_CHAT]?.trim()) setSelections(previous => ({ ...previous, [NEW_CHAT]: { ...selection } }));
    setActiveId(null);
    requestAnimationFrame(() => input.current?.focus());
  };

  const stop = async () => {
    if (!activeId || !session?.pending || stopRequested || cancelLocks.current.has(activeId)) return;
    const target = activeId;
    cancelLocks.current.add(target);
    setCanceling(previous => ({ ...previous, [target]: true }));
    try { remember(await post<ChatSession>(`/api/chats/${encodeURIComponent(target)}/cancel`, {})); }
    catch (err) { notify(errorMessage(err), 'error'); }
    finally { cancelLocks.current.delete(target); setCanceling(previous => ({ ...previous, [target]: false })); }
  };

  const send = async () => {
    const message = text.trim();
    if (!message || exceedsLimit || busy || sendLocks.current.has(key) || creatingRef.current || loading || (activeId && !session)) return;
    if (policyMismatch) { notify('请先核对仓库绑定，并使用仓库允许的模型', 'error'); return; }
    if (selection.mode === 'data' && !datasources.some(item => item.id === selection.datasource_id)) { notify('请先选择本次查询使用的数据库', 'error'); return; }
    let target = activeId;
    let lockKey = key;
    let ownsCreation = false;
    sendLocks.current.add(lockKey);
    submittedIds.current[lockKey] = new Set(session?.messages.map(item => item.id));
    setFailures(previous => ({ ...previous, [key]: undefined }));
    setPending(previous => ({ ...previous, [key]: message }));
    nearBottom.current = true;
    try {
      if (!target) {
        ownsCreation = true; creatingRef.current = true; setCreating(true);
        const created = await post<ChatSession>('/api/chats', {});
        target = created.id; remember(created); setActiveId(target);
        setSelections(previous => ({ ...previous, [created.id]: { ...selection } }));
        setDrafts(previous => ({ ...previous, [NEW_CHAT]: '', [created.id]: '' }));
        setPending(previous => { const next = { ...previous, [created.id]: message }; delete next[NEW_CHAT]; return next; });
        submittedIds.current[created.id] = submittedIds.current[NEW_CHAT]; delete submittedIds.current[NEW_CHAT];
        sendLocks.current.delete(lockKey); lockKey = target; sendLocks.current.add(lockKey);
        ownsCreation = false; creatingRef.current = false; setCreating(false);
        requestAnimationFrame(() => { if (document.activeElement === document.body && input.current?.offsetParent) input.current.focus(); });
      } else {
        setDrafts(previous => ({ ...previous, [target!]: '' }));
      }
      const value = await post<ChatSession>(`/api/chats/${encodeURIComponent(target)}/messages`, {
        message, mode: selection.mode, provider_id: selection.provider_id || null,
        repository_id: selection.repository_id || null, datasource_id: selection.datasource_id || null,
      });
      remember(value); refresh();
    } catch (err) {
      let saved = false;
      if (target) {
        try {
          const recovered = await get<ChatSession>(`/api/chats/${encodeURIComponent(target)}`);
          const previousIds = new Set(session?.messages.map(item => item.id));
          saved = recovered.messages.some(item => item.role === 'user' && item.content === message && !previousIds.has(item.id));
          remember(recovered);
        } catch { /* Preserve the submitted question locally if the service is unavailable. */ }
      }
      setFailures(previous => ({ ...previous, [target || NEW_CHAT]: { message: errorMessage(err), question: message, saved } }));
      if (!saved) setDrafts(previous => previous[target || NEW_CHAT]?.trim() ? previous : { ...previous, [target || NEW_CHAT]: message });
    } finally {
      sendLocks.current.delete(lockKey);
      delete submittedIds.current[lockKey];
      if (ownsCreation) { creatingRef.current = false; setCreating(false); }
      setPending(previous => { const next = { ...previous }; delete next[lockKey]; return next; });
    }
  };

  return <div className={`bm-chat${sidebarOpen ? '' : ' bm-chat-sidebar-collapsed'}`}>
    {sidebarOpen && <aside className="bm-chat-sidebar" aria-label="对话历史">
      <div className="bm-chat-sidebar-title"><span>对话</span><button type="button" className="bm-chat-icon" aria-label="收起对话历史" title="收起对话历史" onClick={() => setSidebarOpen(false)}><PanelLeftClose size={17}/></button></div>
      <button type="button" className="bm-chat-new" aria-label="新建对话" title={activeId && drafts[NEW_CHAT]?.trim() ? '返回尚未发送的草稿' : '新建对话'} onClick={newChat} disabled={creating}>{creating ? <LoaderCircle size={16} className="bm-chat-spin"/> : <Plus size={17}/>}新建对话{activeId && drafts[NEW_CHAT]?.trim() ? <span className="bm-chat-new-draft">草稿</span> : null}</button>
      <label className="bm-chat-search"><Search size={14}/><input aria-label="搜索对话历史" placeholder="搜索对话" value={filter} onChange={event => setFilter(event.target.value)}/></label>
      <div className="bm-chat-history-caption">最近对话 <span>{chats.length}</span></div>
      <nav className="bm-chat-history" aria-label="选择对话">
        {historyLoading ? <p className="bm-chat-history-hint" role="status"><LoaderCircle size={14} className="bm-chat-spin"/>正在加载对话…</p> : historyError ? <div className="bm-chat-history-error" role="alert"><p>{historyError}</p><button type="button" className="bm-chat-text-button" onClick={() => void retryHistory()}>重新加载</button></div> : filteredChats.length ? filteredChats.map(chat => <button type="button" key={chat.id} className={`bm-chat-history-item${chat.id === activeId ? ' is-active' : ''}`} aria-current={chat.id === activeId ? 'page' : undefined} title={chat.title || '新对话'} disabled={creating} onClick={() => void openChat(chat.id)}>
          {pending[chat.id] !== undefined || chat.pending ? <LoaderCircle size={15} className="bm-chat-spin"/> : <MessageSquare size={15}/>}<span><strong>{chat.title || '新对话'}</strong><small>{formatTime(chat.updated_at)}</small></span>{drafts[chat.id]?.trim() ? <span className="bm-chat-draft-tag">草稿</span> : null}
        </button>) : <p className="bm-chat-history-hint">{filter ? '没有匹配的对话' : '还没有对话。发起一个业务问题，回答会保存在这里。'}</p>}
      </nav>
      <div className="bm-chat-history-footer"><BookOpen size={14}/><span>对话保存在当前工作区</span></div>
    </aside>}

    <section className="bm-chat-main" aria-label="AI 问答">
      <header className="bm-chat-header"><div>{!sidebarOpen && <button type="button" className="bm-chat-icon" aria-label="展开对话历史" title="展开对话历史" onClick={() => setSidebarOpen(true)}><PanelLeftOpen size={18}/></button>}<div className="bm-chat-header-title"><h1>{session?.messages.length ? session.title : 'AI 问答'}</h1><span>{selection.mode === 'data' ? '用自然语言探索业务数据' : '让业务知识成为工作的助力'}</span></div></div><button type="button" className="bm-chat-text-button" onClick={onConfigure}><Settings2 size={15}/>模型配置</button></header>
      <div className="bm-chat-conversation" ref={list} onScroll={event => { const element = event.currentTarget; nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100; }}>
        <div className="bm-chat-transcript">
          {loading ? <div className="bm-chat-loading" role="status"><LoaderCircle size={20} className="bm-chat-spin"/>正在加载对话…</div> : !messages.length && !busy && !failure ? <div className="bm-chat-welcome">
            <div className="bm-chat-welcome-brand"><div className="bm-chat-welcome-symbol"><BrandMark size={23}/></div><p className="bm-chat-eyebrow">思径助理</p></div><h2>从一个业务问题开始</h2><p>看懂业务，找到依据。</p>
            <div className="bm-chat-suggestions">{SUGGESTIONS[selection.mode].map(([label, question]) => <button type="button" key={question} onClick={() => { setDraft(question); input.current?.focus(); }}><span>{label}<ArrowRight size={14}/></span><strong>{question}</strong></button>)}</div>
            <div className="bm-chat-welcome-context"><span><BookOpen size={14}/>{businessNoteCount} 篇业务知识</span><span><GitBranch size={14}/>{repositories.length} 个仓库</span></div>
          </div> : null}
          {Boolean(session?.earlier_messages_removed) && <p className="bm-chat-earlier-note">这段对话较长，当前保留最近 {messages.length} 条消息。</p>}
          <div className="bm-chat-log" role="log" aria-label="对话消息" aria-live="polite" aria-relevant="additions">
            {messages.map(message => <article key={message.id} className={`bm-chat-message bm-chat-${message.role}${message.error ? ' bm-chat-message-error' : ''}`}>
              <div className="bm-chat-message-author">{message.role === 'assistant' ? <><span className="bm-chat-avatar"><BrandMark size={14}/></span><strong>思径</strong><span>{message.error ? '请求未完成' : message.provider || '本地知识检索'}</span></> : <strong>你</strong>}</div>
              <div className="bm-chat-message-content"><MarkdownView content={message.content || (typeof message.error === 'string' ? message.error : '本次请求未完成，请检查配置后重试。')} notes={notes} onOpen={onOpen}/>
                {message.activities?.length ? <ExplorationActivity activities={message.activities} limited={message.completion === 'limited'} onOpen={onOpen}/> : null}
                {message.sources?.length ? <div className="bm-chat-sources"><span>参考来源</span>{message.sources.map((source, index) => <button type="button" key={`${source.path}-${index}`} title={source.path} onClick={() => onOpen(source.path)}><FileText size={13}/>{source.title || source.path}</button>)}</div> : null}
                {message.result && <QueryCard result={message.result} title={session?.title || '业务查询报表'} notify={notify} onReport={onReport} refresh={refresh}/>}</div>
            </article>)}
            {localPending && !submittedQuestionSaved && <article className="bm-chat-message bm-chat-user"><div className="bm-chat-message-author"><strong>你</strong></div><div className="bm-chat-pending-question">{pending[key]}</div></article>}
            {busy && <div className="bm-chat-progress"><div className="bm-chat-waiting" role="status"><LoaderCircle size={17} className="bm-chat-spin"/><span>{stopRequested ? '已请求停止，当前模型调用或工具执行结束后停止。' : pendingActivities.at(-1)?.label || (selection.mode === 'data' ? '正在准备只读问数…' : '正在查找业务知识入口…')}</span></div>{pendingActivities.length ? <ExplorationActivity activities={pendingActivities} pending onOpen={onOpen}/> : null}</div>}
          </div>
          {failure && <div className="bm-chat-failure" role="alert"><TriangleAlert size={17}/><div><strong>请求未完成</strong><p>{failure.message}</p>{failure.question && !failure.saved && <blockquote>{failure.question}</blockquote>}<p>{failure.question ? failure.saved ? '已重新加载服务中保存的消息。' : '问题已保留在此处，重新发送前可先加载对话确认结果。' : '重新加载后可以继续这段对话。'}</p><div>{activeId && <button type="button" className="bm-chat-text-button" onClick={() => void openChat(activeId, true)}>重新加载对话</button>}{failure.question && <button type="button" className="bm-chat-text-button" onClick={() => { setDraft(failure.question); input.current?.focus(); }}>将问题放回输入框</button>}</div></div></div>}
        </div>
      </div>

      <div className="bm-chat-composer-area">
        {selection.repository_id && <div className="bm-chat-repository-policy" role="status">
          <span>{currentPolicy?.error ? `无法读取仓库模型绑定：${currentPolicy.error}` : !policy ? '正在读取仓库允许的模型…' : policy.status === 'ready' ? `仓库绑定：${policy.provider_name || '已配置模型'} · ${policy.model} · ${policy.base_url}` : policy.status === 'changed' ? '仓库绑定的模型配置已变化，请重新确认。' : policy.status === 'missing' ? '仓库绑定的模型已移除，请重新绑定。' : '此仓库尚未绑定模型；绑定后可进行 AI 问答。'}{!provider && ' 当前使用本地检索，不会发送代码。'}</span>
          {policy?.status === 'ready' && policy.provider_id !== effectiveProviderId ? <button type="button" className="bm-chat-text-button" disabled={busy || creating} onClick={() => updateSelection({ provider_id: policy.provider_id || '' })}>使用绑定模型</button> : policy && policy.status !== 'ready' ? <button type="button" className="bm-chat-text-button" onClick={onRepositories}>前往仓库绑定</button> : null}
        </div>}
        <form className="bm-chat-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
          <label className="bm-chat-input-label" htmlFor="bm-chat-input">{selection.mode === 'data' ? '描述你想查询的业务数据' : '询问业务规则、流程或代码'}</label>
          <textarea id="bm-chat-input" ref={input} placeholder={busy ? '继续写下一个问题，当前回答完成后发送…' : selection.mode === 'data' ? '例如：按渠道统计最近一个月的订单金额…' : '例如：订单退款需要满足哪些条件？'} value={text} rows={3} maxLength={20000} aria-invalid={exceedsLimit || undefined} aria-describedby={messageLength > 7000 ? 'bm-chat-input-limit' : undefined} disabled={creating} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }}/>
          {messageLength > 7000 && <p id="bm-chat-input-limit" className={`bm-chat-input-limit${exceedsLimit ? ' is-invalid' : ''}`} role={exceedsLimit ? 'alert' : undefined}>{messageLength.toLocaleString()} / {MESSAGE_LIMIT.toLocaleString()} 字符{exceedsLimit ? ' · 请缩短问题后发送，当前文字已保留。' : ''}</p>}
          <div className="bm-chat-composer-bottom"><div className="bm-chat-controls">
            <label className="bm-chat-select"><Sparkles size={14}/><span>模型</span><select aria-label="问答使用的模型" value={selection.provider_id} onChange={event => updateSelection({ provider_id: event.target.value })} disabled={busy || creating}><option value="">{settings?.active_provider_id ? `默认 · ${settings.providers.find(item => item.id === settings.active_provider_id)?.name || '当前模型'}` : '本地知识检索'}</option>{selection.provider_id && !settings?.providers.some(item => item.id === selection.provider_id) && <option value={selection.provider_id} disabled>已移除的模型</option>}{settings?.providers.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}</select></label>
            <label className="bm-chat-select"><GitBranch size={14}/><span>仓库</span><select aria-label="问答使用的仓库" value={selection.repository_id} onChange={event => updateSelection({ repository_id: event.target.value })} disabled={busy || creating}><option value="">当前知识库</option>{selection.repository_id && !repository && <option value={selection.repository_id} disabled>已移除的仓库</option>}{repositories.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
            <label className="bm-chat-select"><BookOpen size={14}/><span>模式</span><select aria-label="问答模式" value={selection.mode} onChange={event => updateSelection({ mode: event.target.value as Mode })} disabled={busy || creating}><option value="knowledge">知识问答</option><option value="data">只读问数</option></select></label>
            <label className={`bm-chat-select${selection.mode !== 'data' ? ' is-inactive' : ''}`}><Database size={14}/><span>数据库</span><select aria-label="问答使用的数据库" value={selection.datasource_id} onChange={event => updateSelection({ datasource_id: event.target.value })} disabled={selection.mode !== 'data' || busy || creating}><option value="">{datasources.length ? '选择数据库' : '尚未配置'}</option>{selection.datasource_id && !datasources.some(item => item.id === selection.datasource_id) && <option value={selection.datasource_id} disabled>已移除的数据库</option>}{datasources.map(item => <option key={item.id} value={item.id}>{item.name}{item.demo ? '（示例）' : ''}</option>)}</select></label>
          </div>{busy ? <button type="button" className="bm-chat-send" disabled={stopRequested || !session?.pending} onClick={() => void stop()} aria-label={stopRequested ? '正在停止' : '停止回答'} title={stopRequested ? '当前模型调用或工具执行结束后停止' : '停止探索；当前模型调用或工具执行会先结束'}>{stopRequested ? <LoaderCircle size={18} className="bm-chat-spin"/> : <Square size={15} fill="currentColor"/>}</button> : <button type="submit" className="bm-chat-send" disabled={!text.trim() || exceedsLimit || creating || loading || policyMismatch || Boolean(activeId && !session)} aria-label="发送问题" title={policyMismatch ? '先使用仓库绑定的模型' : '发送问题（Enter）'}>{creating ? <LoaderCircle size={19} className="bm-chat-spin"/> : <ArrowUp size={21}/>}</button>}</div>
        </form>
        <div className="bm-chat-composer-note"><span>{selection.mode === 'data' ? '仅执行只读查询，结果可保存为报表' : provider ? `${provider.model} · 回答附业务知识来源` : '未选择模型时，使用本地笔记检索'}</span><span>Enter 发送 · Shift + Enter 换行</span></div>
      </div>
    </section>
  </div>;
}
