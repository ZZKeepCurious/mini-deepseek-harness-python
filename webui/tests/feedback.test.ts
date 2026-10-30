import { describe, expect, it } from "vitest";
import { parseMessageFeedbackList } from "../src/wire/feedback";

describe("feedback wire", () => {
  it("parses list payloads", () => {
    const state = parseMessageFeedbackList({
      version: "v1",
      items: [{ messageId: "m1", rating: "up" }, { messageId: "m2", rating: "negative" }],
    });
    expect(state.version).toBe("v1");
    expect(state.items).toEqual([
      { messageId: "m1", rating: "up" },
      { messageId: "m2", rating: "down" },
    ]);
  });
});
