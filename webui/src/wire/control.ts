// session.control client — host-level live control (queues / jobs / projections).
// Wire contract (source: miniharness/web/streams.py `_control`):
//   open session.control with payload {args:{}} → first frame:
//     {type:'baseline', value:{queues:{[sid]: items[]}, jobs:{[sid]: jobs[]}, projections:{}}}
//   then replacement frames: {type:'queue', sessionId, items} | {type:'jobs', sessionId, jobs}

export interface QueueItem {
  [key: string]: unknown;
}

export interface JobRow {
  id?: string;
  kind?: string;
  label?: string;
  status?: string;
  [key: string]: unknown;
}

export interface ControlBaseline {
  queues: Record<string, QueueItem[]>;
  jobs: Record<string, JobRow[]>;
  projections: Record<string, unknown>;
}

export interface ControlSnapshot {
  sessionId: string;
  queue: QueueItem[];
  jobs: JobRow[];
}

export type ControlFrame =
  | { type: "baseline"; value: ControlBaseline }
  | { type: "queue"; sessionId: string; items: QueueItem[] }
  | { type: "jobs"; sessionId: string; jobs: JobRow[] };

export function isControlFrame(x: unknown): x is ControlFrame {
  if (typeof x !== "object" || x === null) return false;
  const t = (x as { type?: unknown }).type;
  return t === "baseline" || t === "queue" || t === "jobs";
}

/** Project a control frame onto a per-session view; replace-on-frame semantics. */
export function applyControlFrame(
  current: ControlSnapshot | null,
  frame: ControlFrame
): ControlSnapshot | null {
  if (frame.type === "baseline") {
    // Not tied to one session here; caller selects. Return unchanged marker.
    return current;
  }
  if (!current) return current;
  if (frame.type === "queue") {
    if (frame.sessionId !== current.sessionId) return current;
    return { ...current, queue: frame.items };
  }
  if (frame.type === "jobs") {
    if (frame.sessionId !== current.sessionId) return current;
    return { ...current, jobs: frame.jobs };
  }
  return current;
}
