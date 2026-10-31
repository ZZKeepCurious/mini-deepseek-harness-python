import { describe, expect, it } from "vitest";
import { modelBadgeLabel } from "../src/wire/modelBadge";

describe("modelBadgeLabel", () => {
  it("prefers zh then en", () => {
    expect(modelBadgeLabel({ tone: "info", text: { en: "New", zh: "新" } })).toBe("新");
    expect(modelBadgeLabel({ text: { en: "New" } })).toBe("New");
  });
});
