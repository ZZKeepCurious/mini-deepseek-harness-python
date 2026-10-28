import { describe, expect, it } from "vitest";
import {
  detectTrigger,
  fileCandidates,
  formatFileMention,
  formatSessionMention,
  parseSlashLine,
  rankCandidates,
  sessionCandidates,
  sessionReferenceUri,
  slashCandidates,
} from "../src/composer/triggers";
import type { MenuCandidate } from "../src/composer/triggers";

describe("detectTrigger", () => {
  it("opens a slash token at the start, after whitespace, and after punctuation", () => {
    expect(detectTrigger("/pl", 3)).toMatchObject({ trigger: "/", query: "pl", start: 0, end: 3 });
    expect(detectTrigger("run /pl", 7)).toMatchObject({ trigger: "/", query: "pl", start: 4, end: 7 });
    expect(detectTrigger("( /pl", 5)).toMatchObject({ trigger: "/", query: "pl", start: 2, end: 5 });
  });

  it("keeps slash dead inside URLs and word tokens", () => {
    expect(detectTrigger("https://example.com/pa", 21)).toBeNull();
    expect(detectTrigger("https:/", 7)).toBeNull();
    expect(detectTrigger("path/to", 7)).toBeNull();
  });

  it("opens @ for a plain token and a quoted token spanning spaces", () => {
    expect(detectTrigger("see @ref", 8)).toMatchObject({
      trigger: "@",
      query: "ref",
      quoted: false,
      start: 4,
      end: 8,
    });
    expect(detectTrigger('@"meeting notes', 15)).toMatchObject({
      trigger: "@",
      query: "meeting notes",
      quoted: true,
      start: 0,
      end: 15,
    });
  });

  it("does not treat an email address as a reference trigger", () => {
    expect(detectTrigger("mail me@example.com", 19)).toBeNull();
  });
});

describe("mention formatting", () => {
  it("quotes paths with whitespace and keeps a quoted directory open", () => {
    expect(formatFileMention({ path: "src/a.ts", kind: "file" }, false)).toBe("@src/a.ts");
    expect(formatFileMention({ path: "src/a b.ts", kind: "file" }, false)).toBe('@"src/a b.ts"');
    expect(formatFileMention({ path: "src/folder", kind: "directory" }, true)).toBe('@"src/folder/');
  });

  it("rejects control characters and quotes in path candidates", () => {
    expect(formatFileMention({ path: 'bad"name', kind: "file" }, false)).toBeUndefined();
    expect(formatFileMention({ path: "bad\nname", kind: "file" }, false)).toBeUndefined();
  });

  it("encodes a session id as the dsh-session URI with an escaped label", () => {
    expect(sessionReferenceUri("s1")).toBe("dsh-session:InMxIg");
    expect(
      formatSessionMention({ sessionId: "s1", label: "a]b\\c", displayTitle: "a]b\\c" }),
    ).toBe("@[a\\]b\\\\c](dsh-session:InMxIg)");
  });

  it("uses the host-provided mention when present", () => {
    expect(
      formatSessionMention({
        sessionId: "s1",
        label: "ignored",
        mention: "@[host](dsh-session:InMxIg)",
      }),
    ).toBe("@[host](dsh-session:InMxIg)");
  });
});

describe("slash parsing", () => {
  it("matches the host command grammar", () => {
    expect(parseSlashLine("/plan now")).toEqual({ name: "plan", args: " now" });
    expect(parseSlashLine("/plan")).toEqual({ name: "plan", args: "" });
    expect(parseSlashLine("/Plan")).toBeNull();
    expect(parseSlashLine("plan")).toBeNull();
  });
});

describe("candidate menus", () => {
  const commands = [
    { name: "plan", description: "Enter plan mode" },
    { name: "compact", description: "Compact history" },
  ];
  const skills = [
    { name: "policy-user-only", description: "User invocation", modelInvocable: false },
    { name: "policy-shared", description: "Both invocations", modelInvocable: true },
  ];

  it("keeps commands before skills and ranks a fuzzy skill hit", () => {
    const all = slashCandidates(commands, skills, "");
    expect(all.map((candidate) => candidate.group)).toEqual([
      "Commands",
      "Commands",
      "Skills",
      "Skills",
    ]);
    const fuzzy = slashCandidates(commands, skills, "ps");
    expect(fuzzy.some((candidate) => candidate.value === "policy-shared")).toBe(true);
  });

  it("marks user-only skills", () => {
    const rows = slashCandidates(commands, skills, "policy");
    expect(rows.find((row) => row.value === "policy-user-only")?.badge).toBe("user-only");
    expect(rows.find((row) => row.value === "policy-shared")?.badge).toBeUndefined();
  });

  it("groups files before sessions and labels directories", () => {
    const files = fileCandidates(
      [
        { path: "folderx", kind: "directory" },
        { path: "reference.txt", kind: "file" },
      ],
      "",
    );
    expect(files[0]).toMatchObject({
      group: "Files & folders",
      label: "folderx/",
      detail: "Browse folder",
      kind: "directory",
    });
    const sessions = sessionCandidates(
      [
        {
          sessionId: "s2",
          label: "Reference target",
          displayTitle: "Reference target",
          cwd: "C:/work",
        },
      ],
      "",
    );
    expect(sessions[0]).toMatchObject({
      group: "Sessions",
      label: "Reference target",
      detail: "C:/work",
      value: "s2",
    });
    expect([...files, ...sessions].map((row) => row.group)).toEqual([
      "Files & folders",
      "Files & folders",
      "Sessions",
    ]);
  });

  it("ranks prefix matches before substring and subsequence matches", () => {
    const rows: MenuCandidate[] = [
      { id: "a", group: "g", label: "xxplan", kind: "command", value: "a" },
      { id: "b", group: "g", label: "plan", kind: "command", value: "b" },
      { id: "c", group: "g", label: "p-l-a-n", kind: "command", value: "c" },
    ];
    expect(rankCandidates(rows, "plan").map((row) => row.value)).toEqual(["b", "a", "c"]);
  });
});
