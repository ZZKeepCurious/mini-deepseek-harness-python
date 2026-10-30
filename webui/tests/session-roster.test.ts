import { describe, expect, it } from "vitest";
import {
  childSessionsOf,
  parentSessionOf,
  patchSessionRunning,
  sessionSummaryOf,
  upsertSessionSummary,
} from "../src/wire/session-roster";

describe("session roster", () => {
  it("parses list rows", () => {
    expect(sessionSummaryOf({ sessionId: "s1" })?.sessionId).toBe("s1");
    expect(sessionSummaryOf({})).toBeNull();
  });

  it("upserts and patches running", () => {
    const base = [{ sessionId: "s1", running: false }];
    const added = upsertSessionSummary(base, { sessionId: "s2", running: true });
    expect(added.map((s) => s.sessionId)).toEqual(["s2", "s1"]);
    const patched = patchSessionRunning(added, "s1", true);
    expect(patched.find((s) => s.sessionId === "s1")?.running).toBe(true);
  });

  it("finds parent/child links", () => {
    const sessions = [
      { sessionId: "parent", parentSessionId: undefined },
      { sessionId: "child", parentSessionId: "parent", running: true },
    ];
    expect(childSessionsOf("parent", sessions).map((s) => s.sessionId)).toEqual(["child"]);
    expect(parentSessionOf("child", sessions)?.sessionId).toBe("parent");
  });
});
