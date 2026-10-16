import { useEffect, useMemo, useRef, useState } from 'react';
import { Maximize, Minus, Plus, Search, Network, X } from 'lucide-react';
import type { Graph } from '../types';
import { Empty } from './UI';
import { noteTypeLabel } from '../api';
import { useTheme } from '../theme';
import { GraphController } from './graph/controller';
import './graph/graph.css';

export default function GraphView({ graph, selected, local = false, scope, onOpen }: { graph: Graph; selected?: string; local?: boolean; scope: string; onOpen: (path: string) => void }) {
  const theme = useTheme();
  const container = useRef<HTMLDivElement>(null);
  const instance = useRef<GraphController | null>(null);
  const callback = useRef(onOpen);
  callback.current = onOpen;
  const [filter, setFilter] = useState('');
  const [layoutError, setLayoutError] = useState('');
  const data = useMemo(() => {
    if (!local) return graph;
    const ids = new Set([selected, ...graph.edges.filter(e => e.source === selected || e.target === selected).flatMap(e => [e.source, e.target])]);
    return { nodes: graph.nodes.filter(n => ids.has(n.id)), edges: graph.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
  }, [graph, selected, local]);
  const types = useMemo(() => [...new Set(data.nodes.map(n => n.type || '知识'))], [data]);
  const shown = useMemo(() => {
    const query = filter.trim().toLocaleLowerCase();
    const nodes = data.nodes.filter(n => !query || n.title.toLocaleLowerCase().includes(query));
    const ids = new Set(nodes.map(n => n.id));
    return { nodes, edges: data.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
  }, [data, filter]);
  const cacheKey = JSON.stringify([scope, local ? selected : 'global', local]);
  useEffect(() => {
    if (!container.current) return;
    setLayoutError('');
    const controller = new GraphController(container.current, { local, cacheKey, onOpen: path => callback.current(path), onError: setLayoutError });
    instance.current = controller;
    return () => { controller.destroy(); instance.current = null; };
  }, [cacheKey, local]);
  useEffect(() => { instance.current?.update(data, selected); }, [data, selected, cacheKey]);
  useEffect(() => { instance.current?.filter(filter); }, [filter, data, cacheKey]);
  useEffect(() => { instance.current?.setTheme(); }, [theme, local, cacheKey]);
  return <div className={`graph-view ${local ? 'local' : ''}`}>
    {!local && <div className="graph-head"><div><h1>业务关系图谱</h1><p>拖动节点探索关联，滚轮缩放，点击打开笔记。</p></div><label className="search-input"><Search size={15}/><input aria-label="筛选图谱节点" placeholder="筛选节点…" value={filter} onChange={e => setFilter(e.target.value)}/>{filter && <button type="button" className="icon-button graph-clear" aria-label="清除图谱筛选" onClick={() => setFilter('')}><X size={13}/></button>}</label></div>}
    <div className="graph-canvas-area">
      <div ref={container} className="graph-canvas" role="img" tabIndex={0} aria-label={local ? '当前笔记的局部关系图谱' : '知识库全局关系图谱'} aria-description="拖动节点或空白处，滚轮缩放。键盘方向键平移，加减键缩放，0 键适应窗口。"/>
      {!shown.nodes.length && <Empty title={filter ? '没有匹配的节点' : '还没有关联节点'} detail={filter ? '试试其他关键词，或清除筛选。' : '在笔记中使用 [[双向链接]] 连接知识。'}/>}
      {layoutError && <p className="graph-layout-error" role="status">{layoutError}</p>}
      <div className="graph-controls"><button className="icon-button" aria-label="放大图谱" title="放大 (+)" onClick={() => instance.current?.zoom(1.25)}><Plus size={16}/></button><button className="icon-button" aria-label="缩小图谱" title="缩小 (-)" onClick={() => instance.current?.zoom(.8)}><Minus size={16}/></button><button className="icon-button" aria-label="适应图谱大小" title="适应窗口 (0)" onClick={() => instance.current?.fit()}><Maximize size={15}/></button></div>
      {!local && <div className="graph-legend">{types.map((type, index) => <span key={type}><i className={`graph-shape-${index % 5}`} style={{ background: `var(--graph-node-${index % 5 + 1})` }}/>{noteTypeLabel(type)}</span>)}</div>}
    </div>
    {!local && <div className="graph-footer"><span><Network size={13}/> {shown.nodes.length} 个节点 · {shown.edges.length} 条关联</span><details><summary>节点列表 · 键盘导航</summary><div className="graph-node-list">{shown.nodes.map(node => <button key={node.id} onClick={() => onOpen(node.id)}>{node.title}</button>)}</div></details></div>}
  </div>;
}
