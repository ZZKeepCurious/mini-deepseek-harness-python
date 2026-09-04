// session.follow client — single-session history follow stream.
// Wire contract (source: miniharness/web/streams.py `_follow`, aligned with
// upstream packages/api/session-controller history.ts):
//   open session.follow with payload {args:{address:{kind:'session',sessionId}, maxMessages?}}
//   first server frame: {type:'snapshot', header(SessionWireHeader), cursor(last committed seq),
//     records:{type:'event',event}[], hasMore, projections:{asOfSeq,values}}
//   then: {type:'event', event} frames (event[seq] strictly increasing).
// No `since` resume cursor exists → on re-open you re-pull a fresh snapshot
// (this is a known simplification shared with the backend; see verified-diffs §3.4).

import type { EventEnvelope } from "./types";

/** SessionHistoryRecord — `{type:'event', event}` entry (upstream history.ts entryFor). */
export interface FollowRecord {
  type: "event";
  event: EventEnvelope;
}

export interface FollowSnapshotPayload {
  header: Record<string, unknown>;
  cursor: number;
  records: FollowRecord[];
  hasMore: boolean;
  projections: { asOfSeq: number; values: Record<string, unknown> };
}

export interface FollowEvent {
  type: "event";
  event: EventEnvelope;
}

export type FollowFrame =
  | { type: "snapshot"; header: Record<string, unknown>; cursor: number; records: FollowRecord[]; hasMore: boolean; projections: { asOfSeq: number; values: Record<string, unknown> } }
  | { type: "event"; event: EventEnvelope };

export function isFollowFrame(x: unknown): x is FollowFrame {
  if (typeof x !== "object" || x === null) return false;
  const t = (x as { type?: unknown }).type;
  return t === "snapshot" || t === "event";
}

/** Unwrap `{type:'event', event}` records into bare event envelopes. */
export function unwrapFollowRecords(records: FollowRecord[]): EventEnvelope[] {
  return records.map((r) => r.event);
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
