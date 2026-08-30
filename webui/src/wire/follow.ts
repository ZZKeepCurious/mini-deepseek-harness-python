// session.follow client — single-session history follow stream.
// Wire contract (source: miniharness/web/streams.py `_follow`):
//   open session.follow with payload {args:{address:{kind:'session',sessionId}, maxMessages?}}
//   first server frame: {type:'snapshot', header, cursor, records[], hasMore, projections}
//   then: {type:'event', event} frames (event[seq] strictly increasing).
// No `since` resume cursor exists → on re-open you re-pull a fresh snapshot
// (this is a known simplification shared with the backend; see verified-diffs §3.4).

import type { EventEnvelope } from "./types";

export interface FollowSnapshotPayload {
  header: Record<string, unknown>;
  cursor: number;
  records: EventEnvelope[];
  hasMore: boolean;
  projections: Record<string, unknown>;
}

export interface FollowEvent {
  type: "event";
  event: EventEnvelope;
}

export type FollowFrame =
  | { type: "snapshot"; header: Record<string, unknown>; cursor: number; records: EventEnvelope[]; hasMore: boolean; projections: Record<string, unknown> }
  | { type: "event"; event: EventEnvelope };

export function isFollowFrame(x: unknown): x is FollowFrame {
  if (typeof x !== "object" || x === null) return false;
  const t = (x as { type?: unknown }).type;
  return t === "snapshot" || t === "event";
}

/** Accumulate a bounded list of events in seq order, deduplicated by seq. */
export class TrajectoryBuffer {
  private bySeq = new Map<number, EventEnvelope>();
  private max: number;

  constructor(max = 10000) {
    this.max = max;
  }

  reset(): void {
    this.bySeq.clear();
  }

  push(events: EventEnvelope[]): void {
    for (const ev of events) {
      if (typeof ev?.seq === "number" && !this.bySeq.has(ev.seq)) {
        this.bySeq.set(ev.seq, ev);
      }
    }
    if (this.bySeq.size > this.max) {
      const drop = this.bySeq.size - this.max;
      const toDrop = [...this.bySeq.keys()].sort((a, b) => a - b).slice(0, drop);
      for (const k of toDrop) this.bySeq.delete(k);
    }
  }

  events(): EventEnvelope[] {
    return [...this.bySeq.values()].sort((a, b) => a.seq - b.seq);
  }

  get size(): number {
    return this.bySeq.size;
  }
}
