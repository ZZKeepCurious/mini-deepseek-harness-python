import { describe, expect, it } from "vitest";
import { displayHomePath } from "../src/wire/home-path";

describe("displayHomePath", () => {
  it("abbreviates a home prefix to a tilde", () => {
    expect(displayHomePath("/home/u", "/home/u/proj")).toBe("~/proj");
    expect(displayHomePath("/home/u", "/home/u")).toBe("~");
    expect(displayHomePath("C:\\Users\\u", "C:\\Users\\u\\proj")).toBe("~/proj");
  });

  it("leaves a path outside home untouched", () => {
    expect(displayHomePath("/home/u", "/other/proj")).toBe("/other/proj");
    expect(displayHomePath(undefined, "/home/u/proj")).toBe("/home/u/proj");
    expect(displayHomePath("/home/u", undefined)).toBe("");
  });
});
