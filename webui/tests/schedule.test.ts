import { describe, expect, it } from "vitest";
import {
  formatScheduleInstant,
  scheduleKindLabel,
  scheduleRecordOf,
  sortSchedules,
  scheduleDeleteSucceeded,
  parseScheduleHistory,
} from "../src/wire/schedule";

describe("schedule wire", () => {
  it("parses minimal records", () => {
    const r = scheduleRecordOf({
      id: "sch-1",
      kind: "after",
      title: "Ping",
      prompt: "Say hi",
      scheduledAt: "2026-09-16T15:00:00.120Z",
    });
    expect(r?.id).toBe("sch-1");
    expect(scheduleRecordOf({ id: "", kind: "at", title: "x", prompt: "y", scheduledAt: "z" })).toBeNull();
  });

  it("sorts by scheduledAt then id", () => {
    const sorted = sortSchedules([
      { id: "b", kind: "at", title: "", prompt: "", scheduledAt: "2026-09-17T00:00:00.000Z" },
      { id: "a", kind: "at", title: "", prompt: "", scheduledAt: "2026-09-16T00:00:00.000Z" },
      { id: "c", kind: "at", title: "", prompt: "", scheduledAt: "2026-09-17T00:00:00.000Z" },
    ]);
    expect(sorted.map((r) => r.id)).toEqual(["a", "b", "c"]);
  });

  it("labels kinds in Chinese", () => {
    expect(scheduleKindLabel("after")).toBe("延迟一次");
    expect(scheduleKindLabel("every")).toBe("固定间隔");
  });

  it("formats instants for display", () => {
    const text = formatScheduleInstant("2026-09-16T15:00:00.120Z", "UTC");
    expect(text).toContain("2026");
  });

  it("detects delete outcomes", () => {
    expect(scheduleDeleteSucceeded({ id: "x", deleted: true })).toBe(true);
    expect(scheduleDeleteSucceeded({ id: "x", deleted: false, code: "schedule_not_found" })).toBe(false);
  });

  it("parses history deliveries", () => {
    const items = parseScheduleHistory({
      items: [{ id: "d1", deliveredAt: "2026-09-16T15:00:00.120Z", messageId: "m1" }],
    });
    expect(items[0]?.messageId).toBe("m1");
    expect(parseScheduleHistory({ code: "schedule_not_found" })).toEqual([]);
  });
});
