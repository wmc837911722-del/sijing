import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force';
import type { ForceWorkerRequest, ForceWorkerResponse } from './protocol';

interface ForceNode extends SimulationNodeDatum {
  id: string;
  x: number;
  y: number;
  radius: number;
}

type ForceLink = SimulationLinkDatum<ForceNode>;

// Keep the worker contract separate from the renderer's DOM TypeScript globals.
const worker = self as unknown as {
  onmessage: ((event: MessageEvent<ForceWorkerRequest>) => void) | null;
  postMessage: (message: ForceWorkerResponse, transfer?: Transferable[]) => void;
};

const FRAME_MS = 16;
const WARMUP_TICKS = 80;
const MAX_TICKS_PER_WAKE = 640;
let generation = -1;
let revision = 0;
let nodes: ForceNode[] = [];
let nodeById = new Map<string, ForceNode>();
let ids: string[] = [];
let simulation: Simulation<ForceNode, ForceLink> | undefined;
let timer: ReturnType<typeof setTimeout> | undefined;
let paused = true;
let reducedMotion = false;
let warmupRemaining = 0;
let remainingTicks = 0;

function cancelFrame() {
  if (timer !== undefined) clearTimeout(timer);
  timer = undefined;
}

function emitPositions(settled: boolean) {
  const positions = new Float32Array(nodes.length * 2);
  for (let index = 0; index < nodes.length; index++) {
    const node = nodes[index];
    if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) {
      throw new Error('图谱布局出现无效坐标。');
    }
    positions[index * 2] = node.x;
    positions[index * 2 + 1] = node.y;
  }
  worker.postMessage({ type: 'positions', generation, revision, ids, positions, settled }, [positions.buffer]);
}

function fail(error: unknown) {
  cancelFrame();
  simulation?.stop();
  paused = true;
  worker.postMessage({ type: 'error', generation, message: error instanceof Error ? error.message : '图谱布局失败。' });
}

function schedule(delay = FRAME_MS) {
  if (timer !== undefined || paused || !simulation || !nodes.length) return;
  timer = setTimeout(step, delay);
}

function step() {
  timer = undefined;
  if (paused || !simulation || !nodes.length) return;
  try {
    // Warmup/reduced-motion work yields between small batches, so pause, graph
    // replacement and dragging are processed promptly even on larger graphs.
    const batchSize = warmupRemaining > 0 || reducedMotion ? 8 : 2;
    const started = performance.now();
    for (let index = 0; index < batchSize && remainingTicks > 0; index++) {
      simulation.tick();
      remainingTicks--;
      if (warmupRemaining > 0) warmupRemaining--;
      if (simulation.alpha() <= simulation.alphaMin() || performance.now() - started >= 12) break;
    }
    const settled = simulation.alpha() <= simulation.alphaMin() || remainingTicks <= 0;
    if (settled) simulation.alpha(0);
    if (settled || (!reducedMotion && warmupRemaining === 0)) emitPositions(settled);
    if (!settled) schedule(warmupRemaining > 0 || reducedMotion ? 0 : FRAME_MS);
  } catch (error) {
    fail(error);
  }
}

function replaceGraph(message: Extract<ForceWorkerRequest, { type: 'set-graph' }>) {
  if (message.generation < generation) return;
  cancelFrame();
  simulation?.stop();
  const previous = nodeById;
  const firstGraph = generation < 0 || nodes.length === 0;
  generation = message.generation;
  revision = 0;
  paused = message.paused ?? false;
  reducedMotion = message.reducedMotion ?? false;
  const settled = message.settled ?? false;
  const seen = new Set<string>();
  nodes = message.nodes.map(input => {
    if (!input.id || seen.has(input.id) || !Number.isFinite(input.x) || !Number.isFinite(input.y)) {
      throw new Error('图谱节点必须具有唯一标识和有效坐标。');
    }
    seen.add(input.id);
    const old = previous.get(input.id);
    return {
      id: input.id,
      x: input.x,
      y: input.y,
      radius: Number.isFinite(input.radius) ? Math.max(1, input.radius!) : 6,
      vx: settled ? 0 : (old?.vx ?? 0) * 0.4,
      vy: settled ? 0 : (old?.vy ?? 0) * 0.4,
    };
  });
  ids = nodes.map(node => node.id);
  nodeById = new Map(nodes.map(node => [node.id, node]));
  const links: ForceLink[] = message.edges
    .filter(edge => edge.source !== edge.target && seen.has(edge.source) && seen.has(edge.target))
    .map(edge => ({ source: edge.source, target: edge.target }));
  const local = message.local ?? false;
  // Preserve the supplied coordinate frame, including a restored graph's center.
  const centerX = nodes.reduce((sum, node) => sum + node.x, 0) / Math.max(nodes.length, 1);
  const centerY = nodes.reduce((sum, node) => sum + node.y, 0) / Math.max(nodes.length, 1);
  simulation = forceSimulation<ForceNode, ForceLink>(nodes)
    .stop()
    .alpha(settled ? 0 : firstGraph ? 1 : 0.38)
    .alphaMin(0.004)
    .alphaDecay(0.022)
    .alphaTarget(0)
    .velocityDecay(0.36)
    .force('links', forceLink<ForceNode, ForceLink>(links).id(node => node.id).distance(local ? 62 : 105).strength(0.12))
    .force('charge', forceManyBody<ForceNode>().strength(local ? -220 : -650).distanceMax(local ? 350 : 1000))
    .force('collision', forceCollide<ForceNode>().radius(node => node.radius + (local ? 8 : 15)).strength(0.65))
    .force('x', forceX<ForceNode>(centerX).strength(local ? 0.035 : 0.014))
    .force('y', forceY<ForceNode>(centerY).strength(local ? 0.035 : 0.014));
  warmupRemaining = settled ? 0 : firstGraph ? WARMUP_TICKS : 0;
  remainingTicks = MAX_TICKS_PER_WAKE;
  if (!nodes.length || settled) {
    simulation.alpha(0);
    if (!paused) emitPositions(true);
  } else {
    schedule(0);
  }
}

function drag(message: Extract<ForceWorkerRequest, { type: 'drag' }>) {
  if (message.revision < revision || !Number.isFinite(message.x) || !Number.isFinite(message.y)) return;
  const node = nodeById.get(message.id);
  if (!node || !simulation) return;
  revision = message.revision;
  node.x = message.x;
  node.y = message.y;
  node.vx = 0;
  node.vy = 0;
  node.fx = message.phase === 'end' ? null : message.x;
  node.fy = message.phase === 'end' ? null : message.y;
  // User interaction takes precedence over the initial silent warmup.
  warmupRemaining = 0;
  if (reducedMotion) {
    cancelFrame();
    simulation.alpha(0);
    if (!paused) emitPositions(true);
    return;
  }
  simulation.alpha(Math.max(simulation.alpha(), message.phase === 'end' ? 0.16 : 0.24));
  remainingTicks = MAX_TICKS_PER_WAKE;
  schedule();
}

worker.onmessage = ({ data: message }) => {
  try {
    if (message.type === 'set-graph') {
      replaceGraph(message);
      return;
    }
    if (message.generation !== generation) return;
    switch (message.type) {
      case 'drag':
        drag(message);
        break;
      case 'pause':
        paused = true;
        cancelFrame();
        // A hidden page can miss pointerup. Do not resume with an orphaned pin.
        for (const node of nodes) { node.fx = null; node.fy = null; }
        break;
      case 'resume':
        paused = false;
        if (simulation && simulation.alpha() > simulation.alphaMin()) schedule(0);
        else emitPositions(true);
        break;
      case 'motion':
        reducedMotion = message.reducedMotion;
        if (simulation && simulation.alpha() > simulation.alphaMin()) schedule(0);
        break;
    }
  } catch (error) {
    fail(error);
  }
};
