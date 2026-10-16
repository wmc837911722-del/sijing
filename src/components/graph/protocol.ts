/** Positions are world coordinates, independent of the canvas viewport. */
export interface ForceNodeInput {
  id: string;
  x: number;
  y: number;
  radius?: number;
}

export interface ForceEdgeInput {
  source: string;
  target: string;
}

export type ForceWorkerRequest =
  | {
      type: 'set-graph';
      /** Increment when replacing the graph; other messages must match this value. */
      generation: number;
      nodes: ForceNodeInput[];
      edges: ForceEdgeInput[];
      local?: boolean;
      reducedMotion?: boolean;
      paused?: boolean;
      /** Cached coordinates already settled; resume must not reheat them. */
      settled?: boolean;
    }
  | {
      type: 'drag';
      generation: number;
      /** Monotonically increasing within a generation, including the end message. */
      revision: number;
      id: string;
      x: number;
      y: number;
      phase: 'start' | 'move' | 'end';
    }
  | { type: 'pause'; generation: number }
  | { type: 'resume'; generation: number }
  | { type: 'motion'; generation: number; reducedMotion: boolean };

export type ForceWorkerResponse =
  | {
      type: 'positions';
      generation: number;
      /** Latest processed drag revision; discard frames older than the latest sent drag. */
      revision: number;
      /** Stable input order for this generation. positions contains x, y pairs. */
      ids: string[];
      positions: Float32Array;
      settled: boolean;
    }
  | { type: 'error'; generation: number; message: string };
