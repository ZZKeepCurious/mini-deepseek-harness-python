// Pure trajectory display model: event stream → measurable virtual rows.
//
// Mirrors the upstream ui-trajectory virtual-rows concept (packages/client/
// ui-trajectory/src/client/trajectory-virtual-rows.ts): events are projected
// into rows carrying an estimated pixel height that drives windowing; real
// rendering stays content-sized (pre-wrap), so a small estimate drift only
// affects scroll geometry, mitigated by overscan.
//
// This module is pure data/geometry — no DOM, no React.

import type { EventEnvelope } from "../wire";

export const OVERSCAN_ROWS = 12;
export const DEFAULT_VIEWPORT_HEIGHT = 480;

export const H_TURN = 22;
export const H_SUMMARY = 26;
export const H_NOTICE = 54;
export const H_MESSAGE_BASE = 34;
export const H_MESSAGE_LINE = 17;
export const H_TOOL_BASE = 30;
export const H_TOOL_LINE = 15;
const CHAR_PER_LINE = 96;
const MAX_LINES = 8;

export interface TurnRow {
  kind: "turn";
  key: string;
  turn: number;
  height: number;
}

export interface Block {
  type: string;
  text: string;
}

export interface MessageRow {
  kind: "message";
  key: string;
  turn: number;
  seq: number;
  role: string;
  text: string;
  blocks: Block[];
  height: number;
}

export interface ToolRow {
  kind: "tool";
  key: string;
  turn: number;
  seq: number;
  callId: string;
  name: string;
  arguments: string;
  result: string;
  isError: boolean;
  meta: Record<string, unknown> | null;
  height: number;
}

export interface SummaryRow {
  kind: "summary";
  key: string;
  turn: number;
  count: number;
  toolCount: number;
  preview: string;
  height: number;
}

/** Display-safe turn failure: a provider code when present and a message never derived from credentials. */
export interface TurnFailure {
  code?: string;
  message: string;
}

/**
 * Terminal turn feedback mirroring the upstream Chat nodes `turn-max-tokens`
 * and `turn-error`: the provider stopped at the output-token cap, the turn
 * failed with an error reason, or a restored turn was closed by recovery.
 */
export interface NoticeRow {
  kind: "notice";
  key: string;
  turn: number;
  seq: number;
  notice: "max-tokens" | "error" | "stopped";
  failure: TurnFailure | null;
  height: number;
}

export type TrajectoryRow = TurnRow | MessageRow | ToolRow | SummaryRow | NoticeRow;

export interface RowWindow {
  start: number;
  end: number;
  topPad: number;
  bottomPad: number;
  totalHeight: number;
}

export interface TurnOverview {
  turn: number;
  messageCount: number;
  toolCount: number;
  preview: string;
}

function dataOf(ev: EventEnvelope): Record<string, unknown> {
  return ev.data ?? {};
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

interface MessageLike {
  role?: unknown;
  content?: unknown;
}

function messageOf(ev: EventEnvelope): MessageLike | null {
  if (!ev.type.endsWith("/message") && ev.type !== "session/append" && ev.type !== "event/envelope") {
    return null;
  }
  return asRecord(dataOf(ev).message);
}

function blockText(block: unknown): string {
  const b = asRecord(block);
  if (!b) return "";
  return typeof b.text === "string" ? b.text : "";
}

export function messageText(blocks: unknown): string {
  if (!Array.isArray(blocks)) return "";
  return blocks.filter((b) => blockText(b).length > 0).map((b) => blockText(b)).join("\n");
}

function blocksOf(content: unknown): Block[] {
  if (!Array.isArray(content)) return [];
  const out: Block[] = [];
  for (const b of content) {
    const r = asRecord(b);
    if (!r) continue;
    out.push({ type: typeof r.type === "string" ? r.type : "", text: blockText(b) });
  }
  return out;
}

function lines(text: string, perLine: number): number {
  if (!text) return 0;
  return Math.max(1, Math.min(Math.ceil(text.length / perLine), MAX_LINES));
}

function messageHeight(text: string): number {
  return H_MESSAGE_BASE + lines(text, CHAR_PER_LINE) * H_MESSAGE_LINE;
}

function toolHeight(args: string, result: string): number {
  return H_TOOL_BASE + (lines(args, CHAR_PER_LINE) + lines(result, CHAR_PER_LINE)) * H_TOOL_LINE;
}

function stringOf(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function toolResultOf(ev: EventEnvelope): {
  callId: string;
  text: string;
  isError: boolean;
  meta: Record<string, unknown> | null;
} {
  const d = dataOf(ev);
  const message = asRecord(d.message);
  const source = asRecord(message?.source);
  const content = message?.content ?? d.content;
  const text = Array.isArray(content)
    ? messageText(content)
    : stringOf(content);
  return {
    callId: stringOf(d.callId) || stringOf(message?.toolCallId) || stringOf(source?.callId),
    text,
    isError: d.error !== undefined && d.error !== null
      ? true
      : d.isError === true || message?.isError === true,
    meta: asRecord(d.meta),
  };
}

// Classify a turn/end reason into the terminal notice it renders, mirroring
// upstream turn-error `endFailure` and turn-max-tokens: completed, blocked and
// plugin-merged endings have no notice; a user/parent cancellation shows none.
function failureOf(reason: unknown): TurnFailure | null {
  const record = asRecord(reason);
  if (record?.kind !== "error") return null;
  const failure = asRecord(record.error);
  const code = typeof failure?.code === "string" ? failure.code : undefined;
  return {
    ...(code === undefined ? {} : { code }),
    message: typeof failure?.message === "string" ? failure.message : "",
  };
}

/**
 * The recorded failure of a failed turn, for consumers outside the row model
 * (the frame-wide quota notice reads it from the live append).
 */
export function turnEndFailure(ev: EventEnvelope): TurnFailure | null {
  if (ev.type !== "turn/end") return null;
  return failureOf(dataOf(ev).reason);
}

function noticeOf(reason: unknown): { notice: NoticeRow["notice"]; failure: TurnFailure | null } | null {
  const record = asRecord(reason);
  if (record === null) return null;
  switch (record.kind) {
    case "max-tokens":
      return { notice: "max-tokens", failure: null };
    case "error":
      return { notice: "error", failure: failureOf(reason) };
    case "aborted": {
      const cause = asRecord(record.reason);
      if (cause?.kind === "hook" && cause.reason === "deepseek-account/signed-out") {
        return {
          notice: "stopped",
          failure: {
            code: "ACCOUNT_SIGNED_OUT",
            message: "Stopped because you signed out of DeepSeek.",
          },
        };
      }
      return null;
    }
    case "interrupted":
      return {
        notice: "stopped",
        failure: {
          code: "INTERRUPTED",
          message: "The DSH process exited before this turn finished.",
        },
      };
    default:
      // Merge-extensible TurnEndReason: completed, blocked, forked and every
      // plugin-contributed ending render no notice row.
      return null;
  }
}

// Project the followed event log into display rows. Consecutive tool/call +
// tool/result pairs fold into a single tool row; a turn/end that carries a
// terminal failure or the output-token cap emits one notice row at the turn's
// closing position (upstream turn-error / turn-max-tokens Conversation nodes).
export function groupRows(events: readonly EventEnvelope[]): TrajectoryRow[] {
  const rows: TrajectoryRow[] = [];
  const results = new Map<string, ReturnType<typeof toolResultOf>>();
  for (const ev of events) {
    if (ev.type !== "tool/result") continue;
    const result = toolResultOf(ev);
    if (result.callId !== "") results.set(result.callId, result);
  }
  let turn = 1;
  for (const ev of events) {
    const d = dataOf(ev);
    if (ev.type === "turn/start") {
      const t = Number(d.turn ?? 0);
      if (Number.isFinite(t) && t > 0) turn = t;
      rows.push({ kind: "turn", key: `turn:${turn}`, turn, height: H_TURN });
      continue;
    }
    if (ev.type === "turn/end") {
      const t = Number(d.turn ?? 0);
      if (Number.isFinite(t) && t > 0) turn = t;
      const notice = noticeOf(d.reason);
      if (notice !== null) {
        rows.push({
          kind: "notice",
          key: `notice:${ev.seq}`,
          turn,
          seq: ev.seq,
          notice: notice.notice,
          failure: notice.failure,
          height: H_NOTICE,
        });
      }
      continue;
    }
    const msg = messageOf(ev);
    if (msg) {
      const blocks = blocksOf(msg.content);
      const text = messageText(msg.content);
      rows.push({
        kind: "message",
        key: `msg:${ev.seq}`,
        turn,
        seq: ev.seq,
        role: stringOf(msg.role) || "unknown",
        text,
        blocks,
        height: messageHeight(text),
      });
      continue;
    }
    if (ev.type === "tool/call") {
      const callId = stringOf(d.callId);
      const name = stringOf(d.name) || "?";
      const args = stringOf(d.arguments);
      const result = callId === "" ? undefined : results.get(callId);
      rows.push({
        kind: "tool",
        key: `tool:${ev.seq}`,
        turn,
        seq: ev.seq,
        callId,
        name,
        arguments: args,
        result: result?.text ?? "",
        isError: result?.isError ?? false,
        meta: result?.meta ?? null,
        height: toolHeight(args, result?.text ?? ""),
      });
      continue;
    }
  }
  return rows;
}

// Flatten a text into a single-line preview (used for Overview chips, collapsed
// summaries and search previews).
export function previewLine(text: string, max = 48): string {
  const one = text.split(/\s+/).join(" ").trim();
  return one.length > max ? `${one.slice(0, max).trimEnd()}…` : one;
}

// Replace every collapsed turn with a single summary row.
export function applyCollapse(rows: readonly TrajectoryRow[], collapsed: ReadonlySet<number>): TrajectoryRow[] {
  if (rows.length === 0 || collapsed.size === 0) return rows.slice();
  const out: TrajectoryRow[] = [];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.kind === "turn" && collapsed.has(r.turn)) {
      let count = 0;
      let toolCount = 0;
      let preview = "";
      let j = i + 1;
      while (j < rows.length && rows[j].kind !== "turn") {
        const k = rows[j];
        if (k.kind === "message") {
          count += 1;
          if (!preview) preview = k.text;
        } else if (k.kind === "tool") {
          toolCount += 1;
          if (!preview) preview = `${k.name}(…)`;
        }
        j += 1;
      }
      out.push({
        kind: "summary",
        key: `sum:${r.turn}`,
        turn: r.turn,
        count,
        toolCount,
        preview: previewLine(preview),
        height: H_SUMMARY,
      });
      i = j - 1;
      continue;
    }
    out.push(r);
  }
  return out;
}

// Per-turn overview facts for the top bar.
export function buildOverview(rows: readonly TrajectoryRow[]): TurnOverview[] {
  const out: TurnOverview[] = [];
  let cur: TurnOverview | null = null;
  for (const r of rows) {
    if (r.kind === "turn") {
      cur = { turn: r.turn, messageCount: 0, toolCount: 0, preview: "" };
      out.push(cur);
      continue;
    }
    if (!cur) continue;
    if (r.kind === "message") {
      cur.messageCount += 1;
      if (!cur.preview) cur.preview = previewLine(r.text, 40);
    } else if (r.kind === "tool") {
      cur.toolCount += 1;
      if (!cur.preview) cur.preview = `${r.name}(…)`;
    }
  }
  return out;
}

// Window of rows to render for a scroll offset: everything inside the viewport
// plus `overscan` rows on each side, padded top/bottom so the total height
// always equals the sum of the row estimates.
export function windowedRows(
  rows: readonly TrajectoryRow[],
  scrollTop: number,
  viewportHeight: number,
  overscan: number = OVERSCAN_ROWS,
): RowWindow {
  const n = rows.length;
  if (n === 0) return { start: 0, end: 0, topPad: 0, bottomPad: 0, totalHeight: 0 };
  const starts = new Array<number>(n);
  const heights = new Array<number>(n);
  let total = 0;
  for (let i = 0; i < n; i++) {
    starts[i] = total;
    const h = rows[i].height;
    heights[i] = h;
    total += h;
  }
  const top = Math.max(0, scrollTop);
  const bottom = Math.min(total, top + Math.max(0, viewportHeight));
  let start = 0;
  while (start < n && starts[start] + heights[start] <= top) start += 1;
  let end = start;
  while (end < n && starts[end] < bottom) end += 1;
  start = Math.max(0, start - overscan);
  end = Math.min(n, end + overscan);
  const topPad = start === 0 ? 0 : starts[start];
  const bottomPad = end === n ? 0 : total - starts[end];
  return { start, end, topPad, bottomPad, totalHeight: total };
}

// Estimated scroll offset of a row (O(n); used for jump-to-turn).
export function offsetOfRow(rows: readonly TrajectoryRow[], index: number): number {
  let acc = 0;
  for (let i = 0; i < index && i < rows.length; i++) acc += rows[i].height;
  return acc;
}