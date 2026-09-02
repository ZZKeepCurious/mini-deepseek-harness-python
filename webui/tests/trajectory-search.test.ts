// TrajectorySearchIndex tests (src/trajectory/search.ts): incremental update
// with stale pruning + space-separated AND term matching.

import { describe, expect, it } from "vitest";
import { applyCollapse, groupRows } from "../src/trajectory/model";
import { TrajectorySearchIndex } from "../src/trajectory/search";
import { standardSession } from "./fixtures";

describe("TrajectorySearchIndex", () => {
  const rows = groupRows(standardSession());
  const ix = new TrajectorySearchIndex();
  ix.update(rows);

  it("indexes message text and tool name/args/result", () => {
    expect(ix.search("你好")).toEqual(new Set(["msg:2"]));
    expect(ix.search("回答")).toEqual(new Set(["msg:3"]));
    expect(ix.search("clean")).toEqual(new Set(["tool:4"]));
  });

  it("matches case-insensitively", () => {
    expect(ix.search("GIT")).toEqual(new Set(["tool:4"]));
    expect(ix.search("Clean")).toEqual(new Set(["tool:4"]));
  });

  it("ANDs space-separated terms", () => {
    expect(ix.search("git clean")).toEqual(new Set(["tool:4"]));
    expect(ix.search("git 回答")).toEqual(new Set());
  });

  it("includes the turn number as a source", () => {
    const hit = ix.search("turn 2");
    expect(hit).toEqual(new Set(["turn:2", "msg:9", "msg:10"]));
  });

  it("returns null for empty queries", () => {
    expect(ix.search("")).toBeNull();
    expect(ix.search("   ")).toBeNull();
  });

  it("returns an empty set (not null) when nothing matches", () => {
    expect(ix.search("zzzQQQ")).toEqual(new Set());
  });

  it("matches collapsed summary previews", () => {
    const collapsed = new TrajectorySearchIndex();
    collapsed.update(applyCollapse(groupRows(standardSession()), new Set([1])));
    expect(collapsed.search("你好")).toEqual(new Set(["sum:1"]));
  });

  it("short-circuits on the same array reference", () => {
    const fresh = new TrajectorySearchIndex();
    expect(fresh.update(rows)).toBe(true);
    expect(fresh.update(rows)).toBe(false);
  });

  it("prunes stale entries when rows shrink", () => {
    const fresh = new TrajectorySearchIndex();
    fresh.update(groupRows(standardSession()));
    expect(fresh.search("你好")?.size).toBeGreaterThan(0);
    fresh.update(groupRows([standardSession()[0], standardSession()[1], standardSession()[2]]));
    expect(fresh.search("你好")).toEqual(new Set(["msg:2"]));
    expect(fresh.search("回答")).toEqual(new Set(["msg:3"]));
    expect(fresh.search("clean")).toEqual(new Set());
  });

  it("indexes full (non-collapsed) and collapsed views independently", () => {
    const query = new TrajectorySearchIndex();
    query.update(groupRows(standardSession()));
    expect(query.search("好的")).toEqual(new Set(["msg:10"]));
  });
});