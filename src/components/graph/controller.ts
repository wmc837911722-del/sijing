import cytoscape, { type Core, type ElementDefinition, type NodeSingular, type Position, type StylesheetJson } from 'cytoscape';
import type { Graph } from '../../types';
import { themeColor } from '../../theme';
import type { ForceWorkerRequest, ForceWorkerResponse } from './protocol';

const SHAPES = ['ellipse', 'round-rectangle', 'diamond', 'hexagon', 'triangle'] as const;
type Snapshot = { positions: Map<string, Position>; zoom: number; pan: Position; signature: string; settled: boolean };
// Window-local, bounded and scoped to the vault. Never write graph state to Markdown.
const snapshots = new Map<string, Snapshot>();
type Options = { local: boolean; cacheKey: string; onOpen: (id: string) => void; onError: (message: string) => void };
let gpuSupported: boolean | undefined;
function canUseGpu() {
  if (gpuSupported !== undefined) return gpuSupported;
  const probe = document.createElement('canvas');
  const gl = probe.getContext('webgl2');
  gpuSupported = !!gl;
  gl?.getExtension('WEBGL_lose_context')?.loseContext();
  return gpuSupported;
}

function style(local: boolean): StylesheetJson {
  const colors = SHAPES.map((_, i) => themeColor(`--graph-node-${i + 1}`));
  return [
    { selector: 'node', style: { label: '', shape: (n: NodeSingular) => SHAPES[n.data('typeIndex') % SHAPES.length], 'background-color': (n: NodeSingular) => colors[n.data('typeIndex') % colors.length], width: 'data(size)', height: 'data(size)', 'overlay-opacity': 0, 'border-color': themeColor('--graph-node-border'), 'border-width': .6 } },
    { selector: 'edge', style: { width: .8, 'line-color': themeColor('--graph-edge'), 'curve-style': 'haystack', 'haystack-radius': 0, opacity: .42, 'target-arrow-shape': 'none', 'overlay-opacity': 0 } },
    { selector: 'node.is-current', style: { 'background-color': themeColor('--accent'), 'border-color': themeColor('--text'), 'border-width': 1.5 } },
    { selector: '.is-dimmed', style: { opacity: .13 } },
    { selector: 'node.is-hovered, node.is-neighbor', style: { opacity: 1, 'background-color': themeColor('--accent') } },
    { selector: 'node.is-hovered', style: { 'border-width': 2 } },
    { selector: 'edge.is-connected', style: { opacity: .9, width: 1.3, 'line-color': themeColor('--muted') } },
    { selector: '.is-filtered', style: { display: 'none' } },
  ];
}

export class GraphController {
  private cy: Core;
  private worker?: Worker;
  private resize: ResizeObserver;
  private motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  private saved?: Snapshot;
  private signature = '';
  private generation = 0;
  private revision = 0;
  private frame = 0;
  private zoomFrame = 0;
  private labelsFrame = 0;
  private pending?: Extract<ForceWorkerResponse, { type: 'positions' }>;
  private zoomGoal?: { level: number; anchor: Position; world: Position; time: number };
  private interacted = false;
  private firstLayout = true;
  private filterText = '';
  private disposed = false;
  private sizeHidden = false;
  private dragging = false;
  private hoveredId?: string;
  private currentTheme = document.documentElement.dataset.theme;
  private labelCanvas = document.createElement('canvas');
  private labelContext = this.labelCanvas.getContext('2d')!;
  private labelNodes: NodeSingular[] = [];
  private labelColors = { text: themeColor('--text'), muted: themeColor('--muted'), background: themeColor('--bg') };
  private parent: HTMLElement;

  constructor(private element: HTMLDivElement, private options: Options) {
    this.parent = element.parentElement!;
    this.saved = snapshots.get(options.cacheKey);
    const gpu = canUseGpu();
    const config = { container: element, elements: [], style: style(options.local), layout: { name: 'preset', fit: false }, minZoom: .05, maxZoom: 4, pixelRatio: Math.min(window.devicePixelRatio || 1, 1.5), boxSelectionEnabled: false, autounselectify: true };
    try { this.cy = cytoscape({ ...config, ...{ renderer: { name: 'canvas', webgl: gpu } } }); }
    catch {
      // A disabled/failed graphics context must not prevent knowledge navigation.
      element.replaceChildren(); gpuSupported = false;
      this.cy = cytoscape(config);
    }
    element.dataset.renderer = gpuSupported ? 'webgl' : 'canvas';
    this.labelCanvas.className = 'graph-labels'; this.labelCanvas.setAttribute('aria-hidden', 'true');
    element.append(this.labelCanvas);
    this.cy.on('render pan zoom resize', () => this.invalidateLabels());
    if (this.saved) this.cy.viewport({ zoom: this.saved.zoom, pan: { ...this.saved.pan } });
    this.cy.on('tap', 'node', event => options.onOpen(event.target.id()));
    this.cy.on('mouseover', 'node', event => this.highlight(event.target));
    this.cy.on('mouseout', 'node', () => this.highlight());
    this.cy.on('grab', 'node', event => {
      this.dragging = true;
      this.interact(); this.highlight(event.target); element.classList.add('is-dragging');
      this.post({ type: 'resume', generation: this.generation });
      this.drag(event.target, 'start');
    });
    this.cy.on('drag', 'node', event => this.drag(event.target, 'move'));
    this.cy.on('free', 'node', event => { this.dragging = false; this.drag(event.target, 'end'); element.classList.remove('is-dragging'); });
    // Capture only wheel events destined for the canvas. Native pinch and node drag stay enabled.
    this.parent.addEventListener('wheel', this.wheel, { capture: true, passive: false });
    element.addEventListener('pointerdown', this.pointerDown, true);
    element.addEventListener('keydown', this.keyDown);
    this.motion.addEventListener('change', this.motionChanged);
    document.addEventListener('visibilitychange', this.visibilityChanged);
    this.resize = new ResizeObserver(() => {
      const hidden = !element.clientWidth || !element.clientHeight;
      if (hidden) this.post({ type: 'pause', generation: this.generation });
      else {
        this.cy.resize(); // Resizing must not replace positions or force a fit.
        if (this.sizeHidden && !document.hidden && element.dataset.layoutState === 'running') this.post({ type: 'resume', generation: this.generation });
      }
      this.sizeHidden = hidden;
    });
    this.resize.observe(element);
    try {
      this.worker = new Worker(new URL('./force.worker.ts', import.meta.url), { type: 'module' });
      this.worker.onmessage = (event: MessageEvent<ForceWorkerResponse>) => this.receive(event.data);
      this.worker.onerror = () => this.workerFailed();
    } catch { this.workerFailed(); }
  }

  private post(message: ForceWorkerRequest) { this.worker?.postMessage(message); }
  private workerFailed() {
    this.worker?.terminate(); this.worker = undefined;
    this.element.dataset.layoutState = 'unavailable';
    this.options.onError('自动布局暂不可用，仍可拖动、缩放和打开笔记。');
  }
  private receive(message: ForceWorkerResponse) {
    if (this.disposed || message.generation !== this.generation) return;
    if (message.type === 'error') { this.workerFailed(); return; }
    // While dragging, older frames may still animate neighbors. The grabbed node
    // always follows the pointer. After release, only acknowledged frames apply.
    if (!this.dragging && message.revision < this.revision) return;
    this.pending = message;
    // Consume at most one (the latest) worker result per display frame.
    if (!this.frame) this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      const next = this.pending; this.pending = undefined;
      if (!next || next.generation !== this.generation || (!this.dragging && next.revision < this.revision)) return;
      this.cy.batch(() => next.ids.forEach((id, i) => {
        const node = this.cy.getElementById(id);
        if (node.length && !node.grabbed()) {
          const x = next.positions[i * 2], y = next.positions[i * 2 + 1];
          // Accumulate subpixel movement rather than invalidating the whole
          // connected edge set for changes too small to see. Final state is exact.
          if (next.settled || Math.hypot(x - node.position('x'), y - node.position('y')) * this.cy.zoom() >= .25) node.position({ x, y });
        }
      }));
      this.element.dataset.layoutState = next.settled ? 'settled' : 'running';
      this.invalidateLabels();
      if (this.firstLayout && next.ids.length) {
        this.firstLayout = false;
        if (!this.saved && !this.interacted) this.fit(false);
      }
    });
  }

  update(graph: Graph, selected?: string) {
    const cy = this.cy;
    const types = [...new Set(graph.nodes.map(n => n.type || '知识'))];
    const ids = new Set(graph.nodes.map(n => n.id));
    const edgeMap = new Map(graph.edges.filter(e => ids.has(e.source) && ids.has(e.target)).map(e => [JSON.stringify([e.source, e.target]), e]));
    const signature = JSON.stringify([graph.nodes.map(n => n.id), [...edgeMap.keys()]]);
    const changed = signature !== this.signature;
    const neighbors = new Map<string, Set<string>>();
    for (const { source, target } of edgeMap.values()) {
      if (!neighbors.has(source)) neighbors.set(source, new Set());
      if (!neighbors.has(target)) neighbors.set(target, new Set());
      neighbors.get(source)!.add(target); neighbors.get(target)!.add(source);
    }
    const edgeIds = new Set([...edgeMap.keys()].map(key => `edge:${key}`));
    cy.batch(() => {
      cy.edges().filter(edge => !edgeIds.has(edge.id())).remove();
      cy.nodes().filter(node => !ids.has(node.id())).remove();
      const additions: ElementDefinition[] = [];
      graph.nodes.forEach((node, i) => {
        const old = cy.getElementById(node.id);
        const degree = neighbors.get(node.id)?.size || 0;
        const size = (this.options.local ? 7 : 8) + Math.min(13, Math.sqrt(degree) * 2);
        const data = { id: node.id, label: node.title, size, typeIndex: types.indexOf(node.type || '知识') };
        if (old.length) {
          if (old.data('label') !== data.label || old.data('size') !== data.size || old.data('typeIndex') !== data.typeIndex) old.data(data);
        }
        else {
          const cached = this.saved?.positions.get(node.id);
          const existingNeighbor = [...(neighbors.get(node.id) || [])].map(id => cy.getElementById(id)).find(n => n.length);
          const angle = i * 2.399963;
          const radius = 24 * Math.sqrt(i + 1);
          const position = cached || (existingNeighbor && this.signature
            ? { x: existingNeighbor.position('x') + Math.cos(angle) * 35, y: existingNeighbor.position('y') + Math.sin(angle) * 35 }
            : { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
          additions.push({ data, position });
        }
      });
      for (const [key, edge] of edgeMap) if (!cy.getElementById(`edge:${key}`).length) additions.push({ data: { id: `edge:${key}`, ...edge } });
      if (additions.length) cy.add(additions);
      cy.nodes('.is-current').filter(node => node.id() !== selected).removeClass('is-current');
      if (selected && !cy.getElementById(selected).hasClass('is-current')) cy.getElementById(selected).addClass('is-current');
    });
    this.labelNodes = cy.nodes().toArray().sort((a, b) => b.data('size') - a.data('size'));
    this.invalidateLabels();
    this.filter(this.filterText);
    if (!changed) return;
    this.signature = signature; this.generation += 1; this.revision = 0; this.pending = undefined;
    const restored = this.saved?.signature === signature && this.saved.settled;
    if (restored) { this.firstLayout = false; this.element.dataset.layoutState = 'settled'; }
    else this.element.dataset.layoutState = this.worker ? 'running' : 'unavailable';
    this.post({ type: 'set-graph', generation: this.generation, local: this.options.local, reducedMotion: this.motion.matches, paused: document.hidden || restored, settled: !!restored, nodes: cy.nodes().map(n => ({ id: n.id(), x: n.position('x'), y: n.position('y'), radius: n.data('size') / 2 })), edges: [...edgeMap.values()] });
    if (!this.saved && !this.interacted && cy.nodes().length) this.fit(false);
  }

  filter(query: string) {
    this.filterText = query;
    const text = query.trim().toLocaleLowerCase();
    this.cy.batch(() => {
      this.cy.nodes().forEach(n => {
        const hidden = !!text && !String(n.data('label')).toLocaleLowerCase().includes(text);
        if (hidden !== n.hasClass('is-filtered')) n.toggleClass('is-filtered', hidden);
      });
      this.cy.edges().forEach(e => {
        const hidden = e.source().hasClass('is-filtered') || e.target().hasClass('is-filtered');
        if (hidden !== e.hasClass('is-filtered')) e.toggleClass('is-filtered', hidden);
      });
    });
    this.highlight();
    this.invalidateLabels();
  }

  private highlight(node?: NodeSingular) {
    if (this.hoveredId === node?.id()) return;
    this.hoveredId = node?.id();
    this.element.classList.toggle('has-hover', !!node);
    this.cy.batch(() => {
      this.cy.elements('.is-hovered, .is-neighbor, .is-connected, .is-dimmed').removeClass('is-hovered is-neighbor is-connected is-dimmed');
      if (!node) return;
      this.cy.elements().addClass('is-dimmed');
      node.removeClass('is-dimmed').addClass('is-hovered');
      node.neighborhood('node').removeClass('is-dimmed').addClass('is-neighbor');
      node.connectedEdges().removeClass('is-dimmed').addClass('is-connected');
    });
    this.invalidateLabels();
  }
  private invalidateLabels() {
    if (!this.labelsFrame) this.labelsFrame = requestAnimationFrame(() => { this.labelsFrame = 0; this.drawLabels(); });
  }
  private drawLabels() {
    const width = this.cy.width(), height = this.cy.height();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (this.labelCanvas.width !== Math.round(width * ratio) || this.labelCanvas.height !== Math.round(height * ratio)) {
      this.labelCanvas.width = Math.round(width * ratio); this.labelCanvas.height = Math.round(height * ratio);
    }
    const ctx = this.labelContext;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    const zoom = this.cy.zoom();
    const alpha = Math.max(0, Math.min(1, (zoom - (this.options.local ? .25 : .4)) / .35));
    const occupied = new Set<string>();
    const draw = (node: NodeSingular, hovered: boolean) => {
      if (node.hasClass('is-filtered') || (!hovered && alpha === 0)) return;
      const p = node.renderedPosition();
      const y = p.y + node.data('size') * zoom / 2 + 13;
      if (p.x < -80 || p.x > width + 80 || y < 0 || y > height) return;
      const key = `${Math.floor(p.x / 95)},${Math.floor(y / 22)}`;
      if (!hovered && occupied.has(key)) return;
      occupied.add(key);
      const title = String(node.data('label'));
      const limit = this.options.local ? 10 : 24;
      const characters = Array.from(title);
      const label = characters.length > limit && !hovered ? `${characters.slice(0, limit).join('')}…` : title;
      ctx.globalAlpha = hovered ? 1 : alpha * (node.hasClass('is-dimmed') ? .16 : 1);
      ctx.font = `${hovered || node.hasClass('is-current') ? '600 ' : ''}${this.options.local ? 11 : 12}px "Segoe UI", "Microsoft YaHei", sans-serif`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic'; ctx.lineJoin = 'round';
      ctx.strokeStyle = this.labelColors.background; ctx.lineWidth = 3;
      ctx.fillStyle = hovered ? this.labelColors.text : this.labelColors.muted;
      const x = hovered ? Math.max(80, Math.min(width - 80, p.x)) : p.x;
      ctx.strokeText(label, x, Math.min(height - 6, y), Math.max(1, Math.min(hovered ? width - 16 : 180, width - 16)));
      ctx.fillText(label, x, Math.min(height - 6, y), Math.max(1, Math.min(hovered ? width - 16 : 180, width - 16)));
    };
    if (alpha > 0) for (const node of this.labelNodes) if (node.id() !== this.hoveredId) draw(node, false);
    if (this.hoveredId) { const node = this.cy.getElementById(this.hoveredId); if (node.length) draw(node, true); }
  }
  setTheme() {
    const next = document.documentElement.dataset.theme;
    if (next === this.currentTheme) return;
    this.currentTheme = next;
    this.labelColors = { text: themeColor('--text'), muted: themeColor('--muted'), background: themeColor('--bg') };
    this.cy.style(style(this.options.local)).update();
    this.invalidateLabels();
  }
  private drag(node: NodeSingular, phase: 'start' | 'move' | 'end') {
    this.invalidateLabels();
    this.element.dataset.layoutState = this.motion.matches ? 'settled' : 'running';
    this.post({ type: 'drag', generation: this.generation, revision: ++this.revision, id: node.id(), x: node.position('x'), y: node.position('y'), phase });
  }
  private interact() { this.interacted = true; this.cancelZoom(); this.cy.stop(); }
  private pointerDown = () => { this.interact(); this.element.focus({ preventScroll: true }); };
  private motionChanged = () => {
    this.cancelZoom(); this.cy.stop();
    this.post({ type: 'motion', generation: this.generation, reducedMotion: this.motion.matches });
  };
  private visibilityChanged = () => {
    if (document.hidden) { this.cancelZoom(); this.highlight(); }
    if (document.hidden) this.post({ type: 'pause', generation: this.generation });
    else if (!this.sizeHidden && this.element.dataset.layoutState === 'running') this.post({ type: 'resume', generation: this.generation });
  };

  private wheel = (event: WheelEvent) => {
    if (!(event.target instanceof Node) || !this.element.contains(event.target)) return;
    event.preventDefault(); event.stopPropagation();
    const rect = this.element.getBoundingClientRect();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1);
    this.zoom(Math.exp(-Math.max(-240, Math.min(240, delta)) * .0025), { x: event.clientX - rect.left, y: event.clientY - rect.top });
  };
  zoom(factor: number, anchor = { x: this.cy.width() / 2, y: this.cy.height() / 2 }) {
    this.interacted = true; this.cy.stop();
    const level = Math.max(this.cy.minZoom(), Math.min(this.cy.maxZoom(), (this.zoomGoal?.level || this.cy.zoom()) * factor));
    const pan = this.cy.pan();
    this.zoomGoal = { level, anchor, world: { x: (anchor.x - pan.x) / this.cy.zoom(), y: (anchor.y - pan.y) / this.cy.zoom() }, time: performance.now() };
    if (this.motion.matches) { this.applyZoom(level); this.cancelZoom(); return; }
    if (!this.zoomFrame) this.zoomFrame = requestAnimationFrame(this.animateZoom);
  }
  private applyZoom(level: number) {
    const target = this.zoomGoal;
    if (target) this.cy.viewport({ zoom: level, pan: { x: target.anchor.x - target.world.x * level, y: target.anchor.y - target.world.y * level } });
  }
  private animateZoom = (time: number) => {
    this.zoomFrame = 0;
    const goal = this.zoomGoal;
    if (!goal) return;
    const progress = 1 - Math.exp(-Math.min(50, Math.max(1, time - goal.time)) / 45);
    goal.time = time;
    const remaining = Math.log(goal.level / this.cy.zoom());
    this.applyZoom(Math.abs(remaining) < .001 ? goal.level : this.cy.zoom() * Math.exp(remaining * progress));
    if (Math.abs(remaining) < .001) this.zoomGoal = undefined;
    else this.zoomFrame = requestAnimationFrame(this.animateZoom);
  };
  private cancelZoom() { cancelAnimationFrame(this.zoomFrame); this.zoomFrame = 0; this.zoomGoal = undefined; }

  fit(animate = true) {
    this.cancelZoom(); this.cy.stop();
    if (animate) this.interacted = true;
    const nodes = this.cy.nodes(':visible');
    if (!nodes.length) return;
    const bounds = nodes.boundingBox({ includeLabels: false, includeOverlays: false });
    const padding = this.options.local ? 28 : 72;
    const zoom = Math.max(this.cy.minZoom(), Math.min(this.options.local ? 1 : 1.5, (this.cy.width() - padding * 2) / Math.max(1, bounds.w), (this.cy.height() - padding * 2) / Math.max(1, bounds.h)));
    const pan = { x: this.cy.width() / 2 - (bounds.x1 + bounds.w / 2) * zoom, y: this.cy.height() / 2 - (bounds.y1 + bounds.h / 2) * zoom };
    if (animate && !this.motion.matches) this.cy.animate({ zoom, pan }, { duration: 220, easing: 'ease-out-cubic' });
    else this.cy.viewport({ zoom, pan });
  }
  private keyDown = (event: KeyboardEvent) => {
    if (event.target !== this.element || event.ctrlKey || event.metaKey || event.altKey) return;
    const directions: Record<string, Position> = { ArrowLeft: { x: 45, y: 0 }, ArrowRight: { x: -45, y: 0 }, ArrowUp: { x: 0, y: 45 }, ArrowDown: { x: 0, y: -45 } };
    if (directions[event.key]) { event.preventDefault(); this.interact(); this.cy.panBy(directions[event.key]); }
    else if (['+', '=', '-', '0'].includes(event.key)) { event.preventDefault(); if (event.key === '0') this.fit(); else this.zoom(event.key === '-' ? .8 : 1.25); }
  };

  destroy() {
    this.disposed = true; this.cancelZoom(); cancelAnimationFrame(this.frame); cancelAnimationFrame(this.labelsFrame);
    if (this.cy.nodes().length) {
      snapshots.delete(this.options.cacheKey);
      snapshots.set(this.options.cacheKey, { positions: new Map(this.cy.nodes().map(n => [n.id(), { ...n.position() }])), zoom: this.cy.zoom(), pan: { ...this.cy.pan() }, signature: this.signature, settled: this.element.dataset.layoutState === 'settled' });
      if (snapshots.size > 12) snapshots.delete(snapshots.keys().next().value!);
    }
    this.worker?.terminate(); this.resize.disconnect();
    this.parent.removeEventListener('wheel', this.wheel, true);
    this.element.removeEventListener('pointerdown', this.pointerDown, true);
    this.element.removeEventListener('keydown', this.keyDown);
    this.motion.removeEventListener('change', this.motionChanged);
    document.removeEventListener('visibilitychange', this.visibilityChanged);
    this.cy.destroy();
  }
}
