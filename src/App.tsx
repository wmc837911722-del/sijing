import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, MessageSquare, Moon, Sun, Activity as ActivityIcon, ArrowUpRight, BookOpen, Braces, Check, ChevronDown, ChevronRight, CircleHelp, Database, FileBarChart2, FilePlus2, FileText, FolderClosed, FolderOpen, GitBranch, Hash, Link2, List, LoaderCircle, Minus, Network, PanelLeftClose, PanelRightClose, Plus, Search, ShieldCheck, Sparkles, Square, X } from 'lucide-react';
import { errorMessage, formatTime, get, post } from './api';
import type { Datasource, Graph, Note, NoteSummary, Notify, Settings, Task, View, Workspace } from './types';
import { Busy, ErrorBox, Modal } from './components/UI';
import Document from './components/Document';
import GuidedTour from './components/GuidedTour';
import { BrandMark } from './components/BrandMark';
import SourceEvidenceView from './components/SourceEvidenceView';
import './workspace.css';
const Chat = lazy(() => import('./pages/Chat'));
const GraphView = lazy(() => import('./components/GraphView'));
const Repositories = lazy(() => import('./pages/Repositories'));
const Query = lazy(() => import('./pages/Query'));
const Reports = lazy(() => import('./pages/Reports'));
const SettingsPage = lazy(() => import('./pages/Settings'));
const EMPTY_GRAPH: Graph = { nodes: [], edges: [] };
const KNOWLEDGE_SECTIONS = [
  { id: 'sources', label: '资料', directory: '00-Inbox', detail: '导入的代码与原始资料' },
  { id: 'wiki', label: '业务知识', directory: '01-Wiki', detail: '从资料提炼的业务规则与流程' },
  { id: 'maps', label: '业务地图', directory: '02-Areas/业务地图', detail: '按业务主题组织的导航' },
  { id: 'projects', label: '项目总结', directory: '03-Projects', detail: '分析结论与交付文档' },
] as const;
type KnowledgeSectionId = typeof KNOWLEDGE_SECTIONS[number]['id'];
function isSourceDocument(note: NoteSummary) {
  return note.type === 'code' || note.type === 'raw' || /^(00-Inbox|代码索引)\//.test(note.path);
}
function sectionForNote(note: NoteSummary): KnowledgeSectionId {
  if (isSourceDocument(note)) return 'sources';
  if (note.path.startsWith('02-Areas/')) return 'maps';
  if (note.path.startsWith('03-Projects/')) return 'projects';
  if (note.path.startsWith('业务地图/') && (note.type === 'overview' || /(?:业务地图|overview)\.md$/.test(note.path))) return 'maps';
  return 'wiki';
}
function readableFolder(path: string, repositories: Map<string, string>) {
  const relative = path.replace(/^(?:00-Inbox\/代码资料|01-Wiki\/业务知识|02-Areas\/业务地图|03-Projects\/代码业务分析|代码索引|业务地图)(?:\/|$)/, '').replace(/^(?:00-Inbox|01-Wiki|02-Areas|03-Projects)(?:\/|$)/, '');
  return relative.split('/').filter(Boolean).map(part => repositories.get(part) || part).join(' / ') || '文档';
}
const NAV = [
  { view: 'settings' as const, label: '模型配置', icon: Bot },
  { view: 'repositories' as const, label: '仓库', icon: GitBranch },
  { view: 'query' as const, label: '数据库', icon: Database },
  { view: 'chat' as const, label: 'AI 问答', icon: MessageSquare },
  { view: 'knowledge' as const, label: '业务地图', icon: Network },
];
export default function App() {
  const [view, setView] = useState<View>('chat');
  const [queryVisited, setQueryVisited] = useState(false);
  useEffect(() => { if (view === 'query') setQueryVisited(true); }, [view]);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [graph, setGraph] = useState<Graph>(EMPTY_GRAPH);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [datasources, setDatasources] = useState<Datasource[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tabs, setTabs] = useState<string[]>([]);
  const [currentNote, setCurrentNote] = useState<Note | null>(null);
  const [sourceReference, setSourceReference] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [tag, setTag] = useState('');
  const [folded, setFolded] = useState<Set<string>>(() => new Set(['section:sources']));
  const [expandedSources, setExpandedSources] = useState<Set<string>>(new Set());
  const [sidebar, setSidebar] = useState(true);
  const [wideWorkspace, setWideWorkspace] = useState(() => window.matchMedia('(min-width: 1280px)').matches);
  const [rightbarChoice, setRightbarChoice] = useState<boolean | null>(null);
  const rightbar = rightbarChoice ?? wideWorkspace;
  useEffect(() => {
    const media = window.matchMedia('(min-width: 1280px)');
    const resize = () => setWideWorkspace(media.matches);
    media.addEventListener('change', resize);
    return () => media.removeEventListener('change', resize);
  }, []);
  const [theme, setTheme] = useState<'light' | 'dark'>(() => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('businessmap.theme', theme); } catch { /* Theme remains usable without persistence. */ }
  }, [theme]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState('');
  const [toast, setToast] = useState<{ message: string; kind: string } | null>(null);
  const initialSelection = useRef(false);
  const refreshSequence = useRef(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const notify: Notify = useCallback((message, kind = 'success') => setToast({ message, kind }), []);
  useEffect(() => { if (!toast) return; const timeout = setTimeout(() => setToast(null), toast.kind === 'error' ? 10000 : 4500); return () => clearTimeout(timeout); }, [toast]);
  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    const results = await Promise.allSettled([get<Workspace>('/api/workspace'), get<{ notes: NoteSummary[] }>('/api/notes'), get<Graph>('/api/graph'), get<Settings>('/api/settings'), get<{ datasources: Datasource[] }>('/api/datasources'), get<{ tasks: Task[] }>('/api/tasks')]);
    if (sequence !== refreshSequence.current) return;
    const [workspaceResult, notesResult, graphResult, settingsResult, sourcesResult, tasksResult] = results;
    if (workspaceResult.status === 'fulfilled') { setWorkspace(workspaceResult.value); setError(''); } else setError(errorMessage(workspaceResult.reason));
    if (notesResult.status === 'fulfilled') {
      setNotes(notesResult.value.notes);
      if (!initialSelection.current && notesResult.value.notes.length) { const business = notesResult.value.notes.filter(note => !isSourceDocument(note)); const first = business.find(note => sectionForNote(note) === 'maps') || business.find(note => note.type === 'overview' || /概览|总览|欢迎/.test(note.title)) || business[0] || notesResult.value.notes[0]; setSelected(first.path); setTabs([first.path]); initialSelection.current = true; }
    }
    if (graphResult.status === 'fulfilled') setGraph(previous => JSON.stringify(previous) === JSON.stringify(graphResult.value) ? previous : graphResult.value);
    if (settingsResult.status === 'fulfilled') setSettings(settingsResult.value);
    if (sourcesResult.status === 'fulfilled') setDatasources(sourcesResult.value.datasources);
    if (tasksResult.status === 'fulfilled') setTasks(tasksResult.value.tasks);
    setLoading(false);
  }, []);
  useEffect(() => { void refresh(); const timer = setInterval(() => void refresh(), 15000); return () => clearInterval(timer); }, [refresh]);
  const hasRunningTasks = tasks.some(task => task.status === 'running' || task.status === 'queued');
  useEffect(() => { if (!hasRunningTasks) return; const timer = setInterval(() => void refresh(), 3000); return () => clearInterval(timer); }, [hasRunningTasks, refresh]);
  useEffect(() => { const handler = (event: KeyboardEvent) => { if (document.querySelector('dialog[open]')) return; if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); setView('knowledge'); setSidebar(true); setTimeout(() => searchRef.current?.focus(), 0); } }; window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler); }, []);
  const canLeave = () => !dirtyRef.current || window.confirm('当前笔记有未保存的修改。离开将丢弃这些修改，是否继续？');
  const navigate = (next: View) => { if (next !== view && !canLeave()) return false; if (next !== view) { setDirty(false); setCurrentNote(null); } setView(next); return true; };
  const openNote = useCallback((path: string) => {
    if (path.startsWith('source://')) { setSourceReference(path); return; }
    const target = notes.find(note => note.path === path || note.id === path || note.title === path);
    if (!target) { notify(`知识库中未找到「${path}」`, 'info'); return; }
    if (selected !== target.path && dirtyRef.current && !window.confirm('当前笔记有未保存的修改。离开将丢弃这些修改，是否继续？')) return;
    if (selected !== target.path) setDirty(false); setSelected(target.path); setView('knowledge'); setTabs(previous => previous.includes(target.path) ? previous : [...previous, target.path]);
  }, [notes, notify, selected]);
  const closeTab = (path: string) => {
    if (selected === path && !canLeave()) return;
    const rest = tabs.filter(tab => tab !== path); setTabs(rest);
    if (selected === path) { setDirty(false); setSelected(rest.at(-1) || null); setCurrentNote(null); }
  };
  const create = async (event: React.FormEvent) => { event.preventDefault(); if (!title.trim()) return; setCreateBusy(true); setCreateError(''); try { const note = await post<Note>('/api/notes', { title: title.trim() }); setCreating(false); setTitle(''); await refresh(); if (canLeave()) { setDirty(false); setSelected(note.path); setTabs(previous => [...previous.filter(p => p !== note.path), note.path]); setView('knowledge'); } notify('新笔记已创建'); } catch (err) { setCreateError(errorMessage(err)); } finally { setCreateBusy(false); } };
  const changeVault = async () => { if (!canLeave()) return; try { const path = await window.businessmap?.chooseDirectory(); if (!path) return; const next = await post<Workspace>('/api/vault', { path }); initialSelection.current = false; setWorkspace(next); setGraph(EMPTY_GRAPH); setSelected(null); setTabs([]); setCurrentNote(null); setDirty(false); setSearch(''); setSearchResults(null); setTag(''); setFolded(new Set(['section:sources'])); setExpandedSources(new Set()); await refresh(); notify('已切换知识库'); } catch (err) { notify(errorMessage(err), 'error'); } };
  const [searchResults, setSearchResults] = useState<NoteSummary[] | null>(null);
  useEffect(() => { let cancelled = false; if (!search.trim()) { setSearchResults(null); return; } const timer = setTimeout(() => { void get<{ notes: NoteSummary[] }>(`/api/notes?q=${encodeURIComponent(search)}`).then(data => { if (!cancelled) setSearchResults(data.notes); }).catch(err => { if (!cancelled) notify(errorMessage(err), 'error'); }); }, 200); return () => { cancelled = true; clearTimeout(timer); }; }, [search, notify]);
  const visibleNotes = useMemo(() => (searchResults || notes).filter(note => !tag || note.tags.includes(tag)), [searchResults, notes, tag]);
  const sections = useMemo(() => {
    const repositoryNames = new Map((workspace?.repositories || []).map(repo => [repo.id, repo.name]));
    const groups = new Map<KnowledgeSectionId, Map<string, NoteSummary[]>>();
    for (const note of visibleNotes) {
      const section = sectionForNote(note);
      if (!groups.has(section)) groups.set(section, new Map());
      const folder = note.path.includes('/') ? note.path.split('/').slice(0, -1).join('/') : '';
      const folders = groups.get(section)!;
      if (!folders.has(folder)) folders.set(folder, []);
      folders.get(folder)!.push(note);
    }
    return KNOWLEDGE_SECTIONS.map(section => ({ ...section,
      count: [...(groups.get(section.id)?.values() || [])].reduce((count, items) => count + items.length, 0),
      folders: [...(groups.get(section.id)?.entries() || [])].map(([path, items]) => ({ path, label: readableFolder(path, repositoryNames), notes: items })),
    }));
  }, [visibleNotes, workspace?.repositories]);
  const toggleFolded = (key: string) => setFolded(previous => { const next = new Set(previous); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const businessNoteCount = workspace?.stats.business_notes ?? notes.filter(note => !isSourceDocument(note)).length;
  const sourceDocumentCount = workspace?.stats.source_documents ?? notes.filter(isSourceDocument).length;
  const tags = [...new Set(notes.filter(note => !isSourceDocument(note)).flatMap(note => note.tags))].slice(0, 12);
  const activeProvider = settings?.providers.find(provider => provider.active);
  const mapView = view === 'knowledge' || view === 'graph';
  const primaryView = mapView ? 'knowledge' : view === 'reports' ? 'query' : view;
  const currentLabel = NAV.find(item => item.view === primaryView)?.label || 'AI 问答';
  const headings = currentNote?.content.match(/^#{1,3} .+$/gm) || [];
  const provenance = currentNote?.content.match(/^(?:repo(?:sitory)?|commit|source|source_path|branch|generated_at):\s*.+$/gm) || [];
  return <div className="app-shell workspace-shell">
    <header className={`titlebar ${window.businessmap?.platform === 'darwin' ? 'mac-titlebar' : ''}`}>
      <div className="titlebar-brand"><BrandMark size={16}/><strong>思径 Sijing</strong><span className="titlebar-divider"/><span>{currentLabel}</span></div>
      <div className="titlebar-actions">
        {mapView && <><button className="icon-button" aria-label="切换知识目录" aria-pressed={sidebar} title="切换知识目录" onClick={() => setSidebar(!sidebar)}><PanelLeftClose size={16}/></button><button className="icon-button" aria-label="切换右侧面板" aria-pressed={rightbar} title="切换右侧面板" onClick={() => setRightbarChoice(!rightbar)}><PanelRightClose size={16}/></button></>}
        {window.businessmap?.platform !== 'darwin' && <div className="window-buttons"><button aria-label="最小化窗口" onClick={() => window.businessmap?.windowControl('minimize')}><Minus size={14}/></button><button aria-label="最大化或还原窗口" onClick={() => window.businessmap?.windowControl('maximize')}><Square size={11}/></button><button className="close-window" aria-label="关闭窗口" onClick={() => window.businessmap?.windowControl('close')}><X size={15}/></button></div>}
      </div>
    </header>
    <nav className="primary-navigation" aria-label="应用导航">
      <div className="navigation-workspace"><span className="workspace-monogram" aria-hidden="true"><BrandMark size={20}/></span><div><strong>思径 Sijing</strong><span>看懂业务，找到依据</span></div></div>
      <div className="navigation-items">{NAV.map(item => <button key={item.view} className={`navigation-item ${primaryView === item.view ? 'active' : ''}`} aria-label={item.label} aria-current={primaryView === item.view ? 'page' : undefined} onClick={() => navigate(item.view)}><item.icon size={18}/><span>{item.label}</span></button>)}</div>
      <div className="navigation-bottom">
        <GuidedTour ready={!!workspace && !loading} onNavigate={navigate}/>
        <div className="workspace-location"><span className="connection-dot"/><span>{workspace?.demo ? '示例工作区' : workspace?.name || '本地工作区'}</span></div>
        <div className="theme-switch" role="group" aria-label="主题"><button aria-label="浅色主题" aria-pressed={theme === 'light'} className={theme === 'light' ? 'active' : ''} onClick={() => setTheme('light')}><Sun size={14}/>浅色</button><button aria-label="深色主题" aria-pressed={theme === 'dark'} className={theme === 'dark' ? 'active' : ''} onClick={() => setTheme('dark')}><Moon size={14}/>深色</button></div>
        <small className="navigation-version">思径 · 本地预览</small>
      </div>
    </nav>
    <main className="main-workspace workspace-content">
      {error && <div className="global-error"><ErrorBox error={error} retry={() => void refresh()}/></div>}
      <Suspense fallback={<Busy text="正在打开工作台…"/>}>
        <section className="chat-page-host" hidden={view !== 'chat'} aria-label="AI 问答工作区">{workspace && <Chat key={workspace.vault_path} settings={settings} repositories={workspace.repositories} datasources={datasources} notes={notes} businessNoteCount={businessNoteCount} notify={notify} onOpen={openNote} onReport={() => navigate('reports')} onConfigure={() => navigate('settings')} onRepositories={() => navigate('repositories')} refresh={() => void refresh()}/>}</section>
        {view === 'settings' && <SettingsPage settings={settings} refresh={() => void refresh()} notify={notify}/>}
        {view === 'repositories' && <Repositories repositories={workspace?.repositories || []} providers={settings?.providers || []} tasks={tasks} refresh={() => void refresh()} notify={notify}/>}
        {(queryVisited || view === 'query' || view === 'reports') && <div className="database-workspace" hidden={view !== 'query' && view !== 'reports'}><div className="section-tabs" role="group" aria-label="数据库视图"><button className={view === 'query' ? 'active' : ''} onClick={() => navigate('query')}><Database size={15}/>数据源与查询</button><button className={view === 'reports' ? 'active' : ''} onClick={() => navigate('reports')}><FileBarChart2 size={15}/>报表</button></div><section className="query-page-host" hidden={view !== 'query'}>{(queryVisited || view === 'query') && <Query datasources={datasources} refresh={() => void refresh()} notify={notify} onReport={() => navigate('reports')}/>}</section>{view === 'reports' && <Reports/>}</div>}
        {mapView && <div className={`map-layout ${sidebar ? '' : 'without-directory'} ${rightbar ? '' : 'without-context'}`}>
    {sidebar && <aside className="knowledge-sidebar"><div className="vault-heading"><div className="vault-icon"><Braces size={17}/></div><div><strong>{workspace?.name || '业务知识库'}</strong><span>{workspace?.demo ? '示例知识库' : '本地 Markdown Vault'}</span></div><button className="icon-button" title="切换知识库" aria-label="切换知识库" onClick={() => void changeVault()}><ChevronDown size={14}/></button></div><label className="knowledge-search"><Search size={14}/><input ref={searchRef} placeholder="搜索知识库…" aria-label="搜索知识库" value={search} onChange={e => setSearch(e.target.value)}/>{search ? <button className="icon-button" aria-label="清空搜索" onClick={() => setSearch('')}><X size={12}/></button> : <kbd>{window.businessmap?.platform === 'darwin' ? '⌘ K' : 'Ctrl K'}</kbd>}</label><div className="explorer-caption"><span>{search ? `搜索结果 · ${visibleNotes.length}` : '知识目录'}</span><button className="icon-button" title="新建笔记" aria-label="新建笔记" onClick={() => { setCreating(true); setCreateError(''); }}><FilePlus2 size={15}/></button></div><div className="file-tree">{loading ? <Busy text="读取知识库…"/> : !visibleNotes.length && search ? <p className="sidebar-empty">没有匹配的文档</p> : sections.filter(section => !search || section.count > 0).map(section => {
          const sectionCollapsed = !search && folded.has(`section:${section.id}`);
          return <section className="knowledge-section" key={section.id} aria-label={section.label}>
            <button className="folder-row knowledge-section-heading" title={`${section.directory} · ${section.detail}`} onClick={() => toggleFolded(`section:${section.id}`)} aria-expanded={!sectionCollapsed}>
              {sectionCollapsed ? <ChevronRight size={12}/> : <ChevronDown size={12}/>}<FolderClosed size={14}/><span>{section.label}</span><small>{section.count}</small>
            </button>
            {!sectionCollapsed && <div className="knowledge-section-content">{section.folders.length ? section.folders.map(folder => {
              const folderCollapsed = !search && (section.id === 'sources' ? !expandedSources.has(folder.path) : folded.has(`folder:${folder.path}`));
              return <div className="tree-folder" key={folder.path}><button className="folder-row" title={folder.path || section.directory} onClick={() => section.id === 'sources' ? setExpandedSources(previous => { const next = new Set(previous); if (next.has(folder.path)) next.delete(folder.path); else next.add(folder.path); return next; }) : toggleFolded(`folder:${folder.path}`)} aria-expanded={!folderCollapsed}>
                {folderCollapsed ? <ChevronRight size={12}/> : <ChevronDown size={12}/>}<FolderClosed size={14}/><span>{folder.label}</span><small>{folder.notes.length}</small>
              </button>{!folderCollapsed && <div className="folder-children">{folder.notes.map(note => <button key={note.id} className={`file-row ${selected === note.path && view === 'knowledge' ? 'active' : ''}`} title={note.path} onClick={() => openNote(note.path)}><FileText size={14}/><span>{note.title}</span>{selected === note.path && view === 'knowledge' && <span className="active-file-dot"/>}</button>)}</div>}</div>;
            }) : <p className="section-empty">{section.detail}</p>}</div>}
          </section>;
        })}</div>{tags.length > 0 && <div className="sidebar-tags"><div className="section-kicker">标签{tag && <button className="text-button" onClick={() => setTag('')}>清除</button>}</div><div>{tags.map(item => <button key={item} className={tag === item ? 'active' : ''} onClick={() => setTag(tag === item ? '' : item)}><Hash size={11}/>{item}</button>)}</div></div>}<div className="vault-summary"><div><span className="connection-dot"/><span>{businessNoteCount} 篇业务知识</span><span>·</span><span>{workspace?.stats.links || 0} 条关联</span></div><div className="source-document-count"><FileText size={12}/><span>{sourceDocumentCount} 份资料</span></div><button onClick={() => navigate('repositories')}><GitBranch size={13}/>{workspace?.stats.repositories || 0} 个代码仓库<ArrowUpRight size={12}/></button></div></aside>}
          <section className="map-main">
            <div className="section-tabs" role="group" aria-label="业务地图视图"><button className={view === 'knowledge' ? 'active' : ''} onClick={() => navigate('knowledge')}><BookOpen size={15}/>文档</button><button className={view === 'graph' ? 'active' : ''} onClick={() => navigate('graph')}><Network size={15}/>关系图谱</button><div className="tab-spacer"/><button className="map-folder-button" onClick={() => { void window.businessmap?.openVault().catch(err => notify(errorMessage(err), 'error')); }}><FolderOpen size={15}/>打开目录</button></div>
            {view === 'knowledge' && <><div className="workspace-tabs" role="tablist" aria-label="打开的笔记">{view === 'knowledge' ? tabs.map(path => { const note = notes.find(item => item.path === path); return <div className={`workspace-tab ${selected === path ? 'active' : ''}`} key={path}><button role="tab" aria-selected={selected === path} onClick={() => openNote(path)}><FileText size={13}/><span>{note?.title || path.split('/').at(-1)}</span>{selected === path && dirty && <i/>}</button><button className="tab-close" aria-label={`关闭 ${note?.title || path}`} onClick={() => closeTab(path)}><X size={12}/></button></div>; }) : <div className="workspace-tab active"><span className="page-tab-label">{currentLabel}</span></div>}<button className="icon-button new-tab" aria-label="新建笔记标签" title="新建笔记" onClick={() => { setCreating(true); setCreateError(''); }}><Plus size={15}/></button><div className="tab-spacer"/>{workspace?.demo && <span className="demo-indicator">示例工作区</span>}</div><Document path={selected} notes={notes} onOpen={openNote} onNote={setCurrentNote} onDirty={setDirty} notify={notify} onSaved={() => void refresh()}/></>}
            {view === 'graph' && <GraphView scope={workspace?.vault_path || ''} graph={graph} selected={selected || undefined} onOpen={openNote}/>}
          </section>
          {rightbar && <aside className="right-panel"><div className="map-context-heading"><span><List size={14}/>关联与依据</span><button className="icon-button" aria-label="打开 AI 问答" title="打开 AI 问答" onClick={() => navigate('chat')}><MessageSquare size={15}/></button></div><div className="context-scroll">{view === 'knowledge' && currentNote ? <><section className="context-section outline-section"><h3><List size={13}/>本文大纲</h3>{headings.length ? headings.map((heading, index) => <button className={`outline-item level-${heading.match(/^#+/)?.[0].length}`} key={index} onClick={() => document.querySelectorAll('.note-article .markdown h1, .note-article .markdown h2, .note-article .markdown h3')[index]?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>{heading.replace(/^#+\s*/, '')}</button>) : <p className="context-empty">笔记还没有章节标题。</p>}</section><section className="context-section"><h3><Network size={13}/>局部图谱<button className="icon-button" title="打开完整图谱" aria-label="打开完整图谱" onClick={() => navigate('graph')}><ArrowUpRight size={13}/></button></h3><Suspense fallback={<Busy/>}><GraphView scope={workspace?.vault_path || ''} graph={graph} selected={selected || undefined} local onOpen={openNote}/></Suspense></section><section className="context-section"><h3><Link2 size={13}/>反向链接<span className="count-pill">{currentNote.backlinks.length}</span></h3>{currentNote.backlinks.length ? currentNote.backlinks.map(note => <button className="backlink-card" key={note.id} onClick={() => openNote(note.path)}><div><FileText size={13}/><strong>{note.title}</strong></div><p>{note.excerpt?.slice(0, 100) || note.path}</p></button>) : <p className="context-empty">还没有其他笔记引用这篇知识。</p>}</section><section className="context-section"><h3><ShieldCheck size={13}/>来源依据</h3><div className="source-card"><FileText size={15}/><div><strong>Markdown 源文件</strong><code>{currentNote.path}</code><small>修订 {String(currentNote.revision).slice(0, 12)}</small></div></div>{provenance.map((line, index) => <code className="provenance-line" key={index}>{line}</code>)}<p className="context-empty">业务与代码的引用依据可在笔记正文中查看。</p></section></> : <><section className="context-section"><h3><ActivityIcon size={13}/>最近活动</h3>{workspace?.recent_activity?.length ? workspace.recent_activity.slice(0, 8).map(activity => <div className="activity-item" key={activity.id}><span className="activity-dot"/><div><strong>{activity.title}</strong><p>{activity.detail}</p><small>{formatTime(activity.created_at)}</small></div></div>) : <p className="context-empty">知识更新与仓库活动会显示在这里。</p>}</section><section className="context-section"><h3><CircleHelp size={13}/>知识如何连接</h3><p className="context-tip">用 <code>[[笔记名称]]</code> 建立双向链接。图谱、反向链接和业务助手共享同一份 Markdown 知识。</p><button className="button compact" onClick={() => { navigate('chat'); }}><Sparkles size={13}/>打开 AI 问答</button></section></>}</div></aside>}
        </div>}
      </Suspense>
    </main>
    <footer className="statusbar"><div><span className={`connection-dot ${error ? 'offline' : ''}`}/>{error ? '服务未连接' : '本地服务已连接'}<span className="status-divider"/><ShieldCheck size={12}/><span>数据库只读</span></div><div>{hasRunningTasks && <span><LoaderCircle className="spin" size={12}/>后台任务进行中</span>}<span>{activeProvider ? `${activeProvider.name} · ${activeProvider.model}` : '未连接模型 · 原文检索可用'}</span><span className="status-divider"/><span>思径 0.3.0</span></div></footer>
    {toast && <div className={`toast ${toast.kind}`} role={toast.kind === 'error' ? 'alert' : 'status'}>{toast.kind === 'success' ? <Check size={16}/> : <CircleHelp size={16}/>}<span>{toast.message}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setToast(null)}><X size={14}/></button></div>}
    {creating && <Modal title="新建知识笔记" busy={createBusy} onClose={() => { if (!createBusy) setCreating(false); }}><form className="form-stack" onSubmit={create}><label>笔记名称<input autoFocus required value={title} onChange={e => setTitle(e.target.value)} placeholder="例如：订单关闭规则"/></label><p className="form-hint">新建 Markdown 笔记，使用 [[双向链接]] 将它连接到业务地图。</p>{createError && <p className="form-error" role="alert">{createError}</p>}<div className="modal-actions"><button type="button" className="button" disabled={createBusy} onClick={() => setCreating(false)}>取消</button><button className="button primary" disabled={createBusy || !title.trim()}>{createBusy ? '创建中…' : '创建笔记'}</button></div></form></Modal>}
    {sourceReference && <SourceEvidenceView key={sourceReference} reference={sourceReference} onClose={() => setSourceReference(null)}/>}
  </div>;
}
