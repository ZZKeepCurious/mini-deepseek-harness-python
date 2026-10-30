import { describe, expect, it } from "vitest";
import { TrajectoryBuffer, unwrapPageRecords } from "../src/wire/follow";

describe("session page unwrap", () => {
  it("unwraps page records and tracks earliest seq", () => {
    const events = unwrapPageRecords([
      { type: "event", event: { type: "turn/start", seq: 1, data: {} } },
      { type: "event", event: { type: "turn/end", seq: 2, data: {} } },
    ]);
    expect(events.map((e) => e.seq)).toEqual([1, 2]);
    const buf = new TrajectoryBuffer();
    buf.pushHistory(events);
    expect(buf.earliestSeq()).toBe(1);
  });
});
