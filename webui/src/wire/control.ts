// session/control client — host-level live control (projections only).
// Wire contract (source: miniharness/web/streams.py `_control`, aligned with
// upstream packages/api/session-controller/src/control.ts):
//   open session/control with payload {args:{}} → first frame:
//     {type:'baseline', value:{projections:{[sid]:{asOfSeq, values:{...}}}}}
//   then replacement frames: {type:'projection', sessionId, key, value, seq}
// The legacy queue/jobs replacement frames were removed from rc.1; queue data
// now arrives as the `inbox` projection unit (see inboxQueue below) and jobs
// as the job-controller `job/list` stream (see ./jobs.ts).

export interface ProjectionBaseline {
  asOfSeq: number;
  values: Record<string, unknown>;
}

export interface ControlBaseline {
  projections: Record<string, ProjectionBaseline>;
}

/** Inbox projection value: `{next-turn: PendingMessage[], next-step: PendingMessage[]}`. */
export interface InboxProjection {
  "next-turn": unknown[];
  "next-step": unknown[];
}

export type ControlFrame =
  | { type: "baseline"; value: ControlBaseline }
  | { type: "projection"; sessionId: string; key: string; value: unknown; seq: number };

export function isControlFrame(x: unknown): x is ControlFrame {
  if (typeof x !== "object" || x === null) return false;
  const t = (x as { type?: unknown }).type;
  return t === "baseline" || t === "projection";
}

/** Per-session projection fold: sessionId → key → value. */
export type SessionProjections = Record<string, Record<string, unknown>>;

/** Fold a control frame onto a per-session projection map (replace-on-frame). */
export function applyControlFrame(
  current: SessionProjections,
  frame: ControlFrame
): SessionProjections {
  if (frame.type === "baseline") {
    const next: SessionProjections = {};
    for (const [sid, block] of Object.entries(frame.value.projections)) {
      next[sid] = { ...block.values };
    }
    return next;
  }
  const session = { ...(current[frame.sessionId] ?? {}) };
  session[frame.key] = frame.value;
  return { ...current, [frame.sessionId]: session };
}

/** Flatten an inbox projection value into pending messages (next-turn then next-step). */
export function inboxQueue(inbox: unknown): unknown[] {
  if (typeof inbox !== "object" || inbox === null) return [];
  const state = inbox as Partial<InboxProjection>;
  return [...(state["next-turn"] ?? []), ...(state["next-step"] ?? [])];
}