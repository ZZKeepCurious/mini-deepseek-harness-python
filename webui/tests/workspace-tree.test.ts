import { describe, expect, it } from "vitest";
import { groupSessionsByWorkspace } from "../src/wire/workspace-tree";
import { EMPTY_WORKSPACE_ROSTER } from "../src/wire/workspaces";

describe("workspace tree", () => {
  it("groups sessions under workspace roster entries", () => {
    const roster = {
      ...EMPTY_WORKSPACE_ROSTER,
      items: [
        {
          workspaceId: "w1",
          path: "/proj",
          title: "Project",
          sessionIds: ["s1"],
        },
      ],
    };
    const sessions = [
      { sessionId: "s1", cwd: "/proj" },
      { sessionId: "s2", cwd: "/other" },
    ];
    const groups = groupSessionsByWorkspace(roster, sessions, new Set(["s1", "s2"]));
    expect(groups.map((g) => g.title)).toEqual(["Project", "未分组会话"]);
    expect(groups[0]?.sessions[0]?.sessionId).toBe("s1");
    expect(groups[1]?.sessions[0]?.sessionId).toBe("s2");
  });
});
