import { describe, expect, it } from "vitest";
import {
  EMPTY_WORKSPACE_ROSTER,
  applyWorkspaceFollowFrame,
  workspaceFollowFrameOf,
} from "../src/wire/workspaces";

const WORKSPACE = {
  workspaceId: "w1",
  path: "/home/u/proj",
  title: "proj",
  sessionIds: ["s1"],
};

describe("workspace/follow fold", () => {
  it("reads a baseline and replaces archived ids", () => {
    const baseline = workspaceFollowFrameOf({
      type: "baseline",
      value: { items: [WORKSPACE], archivedSessionIds: ["s2"], pinnedSessionIds: ["s1"] },
    });
    expect(baseline).toEqual({
      type: "baseline",
      value: { items: [WORKSPACE], archivedSessionIds: ["s2"], pinnedSessionIds: ["s1"] },
    });
    const next = applyWorkspaceFollowFrame(EMPTY_WORKSPACE_ROSTER, baseline!);
    expect(applyWorkspaceFollowFrame(next, {
      type: "archived",
      archivedSessionIds: ["s3"],
    }).archivedSessionIds).toEqual(["s3"]);
  });
});
