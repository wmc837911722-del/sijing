import { useRef, useState } from 'react';
import { ArrowUp, Bot, BookOpen, Database, FileText, RotateCcw, Sparkles, BarChart3 } from 'lucide-react';
import { post, errorMessage } from '../api';
import type { ChatResponse, Datasource, NoteSummary, Notify, QueryResult, Report } from '../types';
import { MarkdownView } from './MarkdownView';
import { ResultsTable } from './UI';
type Message = { role: 'user' | 'assistant'; content: string; sources?: ChatResponse['sources']; result?: QueryResult; provider?: string; error?: boolean };

export default function Assistant({ datasources, notes, onOpen, notify, onReport }: { datasources: Datasource[]; notes: NoteSummary[]; onOpen: (path: string) => void; notify: Notify; onReport: () => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [mode, setMode] = useState<'knowledge' | 'data'>('knowledge');
  const [datasource, setDatasource] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  const send = async (input = text) => {
    if (!input.trim() || busy) return;
    const source = datasource || datasources[0]?.id;
    if (mode === 'data' && !source) { notify('请先在 SQL 工作台配置数据源', 'error'); return; }
    const history = messages.filter(m => !m.error).slice(-10).map(({ role, content }) => ({ role, content }));
    setMessages(previous => [...previous, { role: 'user', content: input }]); setText(''); setBusy(true);
    try { const result = await post<ChatResponse>('/api/chat', { message: input, mode, datasource_id: mode === 'data' ? source : undefined, history }); setMessages(previous => [...previous, { role: 'assistant', content: result.answer, sources: result.sources, result: result.result, provider: result.provider }]); }
    catch (err) { setMessages(previous => [...previous, { role: 'assistant', content: errorMessage(err), error: true }]); }
    finally { setBusy(false); setTimeout(() => list.current?.scrollTo({ top: list.current.scrollHeight, behavior: 'smooth' }), 50); }
  };
  const makeReport = async (result: QueryResult) => {
    try { await post<Report>('/api/reports', { result_id: result.id, title: '业务查询报表', chart_type: 'table' }); notify('已生成报表'); onReport(); } catch (err) { notify(errorMessage(err), 'error'); }
  };
  return <div className="assistant"><div className="assistant-top"><div><Sparkles size={16}/><strong>业务助手</strong></div><button className="icon-button" title="清空对话" aria-label="清空对话" onClick={() => setMessages([])} disabled={busy || !messages.length}><RotateCcw size={14}/></button></div><div className="assistant-mode"><button className={mode === 'knowledge' ? 'active' : ''} onClick={() => setMode('knowledge')} disabled={busy}><BookOpen size={13}/>知识问答</button><button className={mode === 'data' ? 'active' : ''} onClick={() => setMode('data')} disabled={busy}><Database size={13}/>只读问数</button></div>{mode === 'data' && <select className="assistant-source" aria-label="助手使用的数据源" value={datasource || datasources[0]?.id || ''} onChange={e => setDatasource(e.target.value)}>{!datasources.length && <option value="">请先添加数据源</option>}{datasources.map(source => <option key={source.id} value={source.id}>{source.name}{source.demo ? '（示例）' : ''}</option>)}</select>}
    <div className="chat-messages" ref={list}>{messages.length === 0 ? <div className="assistant-welcome"><div className="assistant-orbit"><Bot size={26}/></div><h3>让业务知识，成为答案</h3><p>{mode === 'knowledge' ? '从知识库中寻找业务规则、流程与代码依据。' : '结合业务口径生成 SQL，通过只读连接查询实际数据。'}</p><div className="suggested-questions">{(mode === 'knowledge' ? ['这个系统有哪些核心业务？', '订单和退款之间有什么关系？', '如何理解业务地图？'] : ['按渠道统计订单金额', '各订单状态分别有多少条记录？']).map(question => <button key={question} onClick={() => void send(question)}>{question}<span>↗</span></button>)}</div></div> : messages.map((message, index) => <div key={index} className={`chat-message ${message.role} ${message.error ? 'chat-error' : ''}`}><div className="chat-author">{message.role === 'assistant' ? <><Sparkles size={12}/>{message.error ? '请求未完成' : message.provider || '知识库检索'}</> : '你'}</div><MarkdownView content={message.content} notes={notes} onOpen={onOpen}/>{message.sources?.length ? <div className="chat-sources">{message.sources.map(source => <button key={source.path} onClick={() => onOpen(source.path)}><FileText size={12}/>{source.title}</button>)}</div> : null}{message.result && <><ResultsTable result={message.result} compact/><button className="button compact" onClick={() => void makeReport(message.result!)}><BarChart3 size={13}/>保存为报表</button></>}</div>)}{busy && <div className="chat-thinking" role="status"><span className="small-spinner"/>正在检索与分析…</div>}</div>
    <div className="chat-compose"><textarea aria-label="发送给业务助手的问题" placeholder={mode === 'knowledge' ? '询问业务规则、流程或代码…' : '描述你想查询的业务数据…'} rows={3} value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }}/><div><span>{mode === 'knowledge' ? '仅检索知识库' : '数据库只读访问'}</span><button className="send-button" aria-label="发送问题" onClick={() => void send()} disabled={busy || !text.trim()}><ArrowUp size={18}/></button></div></div><div className="assistant-footnote">回答附知识来源 · 未配置模型时使用原文检索</div>
  </div>;
}
