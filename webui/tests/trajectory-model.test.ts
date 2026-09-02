// Pure trajectory display-model tests: groupRows / applyCollapse / buildOverview
// / windowedRows / offsetOfRow / previewLine (src/trajectory/model.ts).

import { describe, expect, it } from "vitest";
import {
  H_MESSAGE_BASE,
  H_MESSAGE_LINE,
  H_SUMMARY,
  H_TOOL_BASE,
  H_TOOL_LINE,
  H_TURN,
  applyCollapse,
  buildOverview,
  groupRows,
  offsetOfRow,
  previewLine,
  windowedRows,
} from "../src/trajectory/model";
import type { TrajectoryRow } from "../src/trajectory/model";
import { assistantMsg, bigTurn, standardSession, toolCall, userMsg } from "./fixtures";

describe("groupRows", () => {
  it("projects a two-turn session into turn/message/tool rows", () => {
    const rows = groupRows(standardSession());
    expect(rows).toHaveLength(7);
    const kinds = rows.map((r) => r.kind);
    expect(kinds).toEqual(["turn", "message", "message", "tool", "turn", "message", "message"]);
    expect(rows[0]).toMatchObject({ kind: "turn", key: "turn:1", turn: 1 });
    expect(rows[1]).toMatchObject({ kind: "message", key: "msg:2", turn: 1, role: "user" });
  });

  it("merges a consecutive tool/call + tool/result pair into one tool row", () => {
    const rows = groupRows(standardSession());
    const tool = rows.find((r) => r.kind === "tool");
    expect(tool).toMatchObject({
      kind: "tool",
      key: "tool:4",
      name: "git",
      arguments: '{"cmd":"status"}',
      result: "clean",
      isError: false,
    });
  });

  it("joins reasoning + text blocks into message text and keeps blocks for styling", () => {
    const rows = groupRows(standardSession());
    const msg = rows[2] as Extract<TrajectoryRow, { kind: "message" }>;
    expect(msg.text).toBe("思考一下\n回答一下问题");
    expect(msg.blocks.map((b) => b.type)).toEqual(["reasoning", "text"]);
  });

  it("ignores control-plane events (agent/created) and turn/end", () => {
    const rows = groupRows(standardSession());
    expect(rows.some((r) => r.key === "agent/created")).toBe(false);
    expect(rows.filter((r) => r.kind === "turn")).toHaveLength(2);
  });

  it("defaults to turn 1 when no turn/start marker is present", () => {
    const rows = groupRows([userMsg(1, "a"), assistantMsg(2, "b")]);
    expect(rows.every((r) => r.turn === 1)).toBe(true);
  });

  it("keeps a lone tool/call with an empty result when no pair follows", () => {
    const rows = groupRows([userMsg(1, "x"), toolCall(2, "pwd", "")]);
    expect(rows[1]).toMatchObject({ kind: "tool", name: "pwd", result: "", isError: false });
  });

  it("emits distinct stable keys per sequ", () => {
    const rows = groupRows(standardSession());
    const keys = rows.map((r) => r.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("heights", () => {
  it("estimates message height from text length", () => {
    const rows = groupRows([userMsg(1, "短")]);
    expect(rows[0].height).toBe(H_MESSAGE_BASE + 1 * H_MESSAGE_LINE);
    const long = groupRows([userMsg(1, "x".repeat(400))]);
    expect(long[0].height).toBeGreaterThan(H_MESSAGE_BASE + 1 * H_MESSAGE_LINE);
  });

  it("estimates tool height from args+result", () => {
    const session = groupRows(standardSession());
    const tool = session.find((r) => r.kind === "tool");
    expect(tool?.height).toBe(H_TOOL_BASE + 2 * H_TOOL_LINE);
  });
});

describe("previewLine", () => {
  it("flattens whitespace and truncates with an ellipsis", () => {
    expect(previewLine("  a \n b  ")).toBe("a b");
    expect(previewLine("x".repeat(60))).toBe(`${"x".repeat(48)}…`);
    expect(previewLine("")).toBe("");
  });
});

describe("applyCollapse", () => {
  it("replaces a collapsed turn with one summary row", () => {
    const rows = groupRows(standardSession());
    const collapsed = applyCollapse(rows, new Set([1, 2]));
    expect(collapsed).toHaveLength(2);
    expect(collapsed[0]).toMatchObject({
      kind: "summary",
      key: "sum:1",
      turn: 1,
      count: 2,
      toolCount: 1,
      preview: "你好世界",
      height: H_SUMMARY,
    });
    expect(collapsed[1]).toMatchObject({ kind: "summary", turn: 2, count: 2, toolCount: 0 });
  });

  it("returns a copy when nothing is collapsed", () => {
    const rows = groupRows(standardSession());
    const out = applyCollapse(rows, new Set());
    expect(out).not.toBe(rows);
    expect(out).toHaveLength(rows.length);
  });
});

describe("buildOverview", () => {
  it("aggregates message/tool counts and first preview per turn", () => {
    const overview = buildOverview(groupRows(standardSession()));
    expect(overview).toEqual([
      { turn: 1, messageCount: 2, toolCount: 1, preview: "你好世界" },
      { turn: 2, messageCount: 2, toolCount: 0, preview: "再查下日志" },
    ]);
  });
});

describe("windowedRows", () => {
  const rows: TrajectoryRow[] = [
    { kind: "turn", key: "turn:1", turn: 1, height: H_TURN },
    { kind: "message", key: "msg:2", turn: 1, seq: 2, role: "user", text: "a", blocks: [], height: 100 },
    { kind: "message", key: "msg:3", turn: 1, seq: 3, role: "user", text: "b", blocks: [], height: 100 },
    { kind: "message", key: "msg:4", turn: 1, seq: 4, role: "user", text: "c", blocks: [], height: 100 },
    { kind: "message", key: "msg:5", turn: 1, seq: 5, role: "user", text: "d", blocks: [], height: 100 },
  ];

  it("renders everything when the viewport fits the content", () => {
    const win = windowedRows(rows, 0, 100000, 0);
    expect(win).toEqual({ start: 0, end: 5, topPad: 0, bottomPad: 0, totalHeight: 422 });
  });

  it("windows by scroll offset with overscan and pads to the total height", () => {
    const win = windowedRows(rows, 150, 100, 1);
    expect(win.start).toBeGreaterThanOrEqual(1);
    expect(win.end).toBeGreaterThan(win.start);
    const visible = rows.slice(win.start, win.end).reduce((s, r) => s + r.height, 0);
    expect(win.topPad + visible + win.bottomPad).toBe(win.totalHeight);
  });

  it("clamps at the edges", () => {
    const win = windowedRows(rows, 0, 50, 0);
    expect(win.start).toBe(0);
    const end = windowedRows(rows, 100000, 50, 0);
    expect(end.end).toBe(5);
    expect(end.bottomPad).toBe(0);
  });

  it("returns a zero window for no rows", () => {
    const win = windowedRows([], 0, 100, 0);
    expect(win).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 0, totalHeight: 0 });
  });
});

describe("offsetOfRow", () => {
  const rows: TrajectoryRow[] = [
    { kind: "turn", key: "turn:1", turn: 1, height: 10 },
    { kind: "message", key: "m", turn: 1, seq: 1, role: "user", text: "", blocks: [], height: 20 },
    { kind: "message", key: "m2", turn: 1, seq: 2, role: "user", text: "", blocks: [], height: 30 },
  ];
  it("accumulates preceding heights", () => {
    expect(offsetOfRow(rows, 0)).toBe(0);
    expect(offsetOfRow(rows, 1)).toBe(10);
    expect(offsetOfRow(rows, 2)).toBe(30);
  });
});

describe("big session windowing", () => {
  it("windows a 300-message turn down to a bounded slice", () => {
    const rows = groupRows(bigTurn(300));
    expect(rows).toHaveLength(301);
    const win = windowedRows(rows, 0, 300);
    expect(win.end - win.start).toBeLessThan(60);
    const mid = windowedRows(rows, 2000, 300);
    expect(mid.topPad).toBeGreaterThan(0);
    expect(mid.bottomPad).toBeGreaterThan(0);
    expect(mid.topPad + mid.bottomPad).toBeLessThan(win.totalHeight);
  });
});