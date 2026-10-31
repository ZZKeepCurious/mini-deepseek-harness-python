import { describe, expect, it } from "vitest";
import { groupRows } from "../src/trajectory/model";
import type { EventEnvelope } from "../src/wire";

describe("webhook notice rows", () => {
  it("folds webhook user messages into a notice chip", () => {
    const events: EventEnvelope[] = [
      {
        seq: 1,
        type: "user/message",
        data: {
          message: {
            role: "user",
            content: [{ type: "text", text: "ignored body" }],
            source: {
              kind: "webhook",
              provider: "github",
              source: "review",
              deliveryId: "d1",
              ruleId: "r1",
              form: "notice",
              summary: "GitHub event received",
            },
          },
        },
      },
    ];
    const rows = groupRows(events);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.kind).toBe("webhook-notice");
    if (rows[0]?.kind === "webhook-notice") {
      expect(rows[0].summary).toBe("GitHub event received");
    }
  });
});
