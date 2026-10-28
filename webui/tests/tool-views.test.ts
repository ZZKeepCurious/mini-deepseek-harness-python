import { describe, expect, it } from "vitest";
import type { ToolRow } from "../src/trajectory/model";
import { searchModel, shellModel, todoModel } from "../src/trajectory/tool-views";
import {
  diffModel,
  imageModel,
  readModel,
  webFetchModel,
  webSearchModel,
} from "../src/trajectory/tool-views";

function row(overrides: Partial<ToolRow> & Pick<ToolRow, "name" | "arguments" | "result">): ToolRow {
  return {
    kind: "tool",
    key: "tool:1",
    turn: 1,
    seq: 1,
    callId: "call-1",
    isError: false,
    meta: null,
    height: 0,
    ...overrides,
  };
}

describe("shellModel", () => {
  it("reads command, cwd, output and a non-zero exit", () => {
    const model = shellModel(
      row({
        name: "bash",
        arguments: JSON.stringify({
          command: "printf 'hello\\n'",
          description: "Print a greeting",
          cwd: "src",
        }),
        result: "hello\n[stderr]\nwarning\n[exit code: 2]",
        meta: { cwd: "/workspace" },
      }),
    );
    expect(model).toMatchObject({
      command: "printf 'hello\\n'",
      description: "Print a greeting",
      cwd: "/workspace",
      background: false,
      output: "hello",
      stderr: "warning",
      exitCode: 2,
      summary: "退出码 2",
    });
  });

  it("parses timeout, stop and signal markers", () => {
    expect(
      shellModel(
        row({
          name: "bash",
          arguments: '{"command":"sleep 1","description":"Wait"}',
          result: "partial\n[timed out after 1000ms]",
        }),
      )?.summary,
    ).toBe("超时");
    expect(
      shellModel(
        row({
          name: "bash",
          arguments: '{"command":"sleep 1","description":"Wait"}',
          result: "partial\n[stopped: user]",
        }),
      )?.summary,
    ).toBe("已停止");
    expect(
      shellModel(
        row({
          name: "bash",
          arguments: '{"command":"sleep 1","description":"Wait"}',
          result: "partial\n[killed by signal: SIGTERM]",
        }),
      )?.summary,
    ).toBe("信号 SIGTERM");
  });

  it("returns null for missing commands and malformed arguments", () => {
    expect(shellModel(row({ name: "bash", arguments: "{}", result: "" }))).toBeNull();
    expect(shellModel(row({ name: "bash", arguments: "{", result: "" }))).toBeNull();
    expect(shellModel(row({ name: "git", arguments: "{}", result: "" }))).toBeNull();
  });
});

describe("searchModel", () => {
  it("groups grep matches by file and keeps the truncation note", () => {
    const model = searchModel(
      row({
        name: "grep",
        arguments: '{"pattern":"search","path":"src"}',
        result: [
          "src/a.ts:",
          "  16: export const DEFAULT_SEARCH_MAX_LINES = 16",
          "  138: export function SearchBlock()",
          "src/b.ts:",
          "  4: import { search } from './a'",
          "",
          "(Showing the first 3 matches; narrow pattern or path to see more.)",
        ].join("\n"),
      }),
    );
    expect(model).toMatchObject({
      kind: "matches",
      shown: 3,
      fileCount: 2,
      truncated: true,
      summary: "显示 3 条匹配 · 2 个文件",
    });
    expect(model?.groups[0]).toMatchObject({
      path: "src/a.ts",
      matches: [
        { lineNumber: 16, line: "export const DEFAULT_SEARCH_MAX_LINES = 16" },
        { lineNumber: 138, line: "export function SearchBlock()" },
      ],
    });
    expect(model?.recovery).toContain("Showing the first 3 matches");
  });

  it("parses glob paths and total counts", () => {
    const model = searchModel(
      row({
        name: "glob",
        arguments: '{"pattern":"*.ts"}',
        result: "src/a.ts\nsrc/b.ts\n\n(Showing 2 of 5 paths. Narrow pattern or path to see more.)",
      }),
    );
    expect(model).toMatchObject({
      kind: "paths",
      paths: ["src/a.ts", "src/b.ts"],
      shown: 2,
      total: 5,
      truncated: true,
      summary: "显示 2/5 个文件",
    });
  });

  it("reports an empty grep result", () => {
    expect(
      searchModel(row({ name: "grep", arguments: '{"pattern":"zzz"}', result: "No matches found." })),
    ).toMatchObject({ kind: "matches", groups: [], summary: "无匹配" });
  });
});

describe("todoModel", () => {
  it("counts statuses and names the first active item", () => {
    const model = todoModel(
      row({
        name: "todo_write",
        arguments: JSON.stringify({
          todos: [
            { content: "梳理需求", status: "completed" },
            { content: "实现 fixture 样本", status: "in_progress" },
            { content: "跑后台构建", status: "in_progress" },
            { content: "浏览器验收", status: "pending" },
          ],
        }),
        result: "{}",
      }),
    );
    expect(model).toMatchObject({
      done: 1,
      total: 4,
      activeContent: "实现 fixture 样本",
      activeExtra: 1,
      completed: 1,
      inProgress: 2,
      pending: 1,
    });
  });

  it("returns null for malformed lists", () => {
    expect(todoModel(row({ name: "todo_write", arguments: '{"todos":{}}', result: "" }))).toBeNull();
    expect(
      todoModel(
        row({
          name: "todo_write",
          arguments: '{"todos":[{"content":"a","status":"unknown"}]}',
          result: "",
        }),
      ),
    ).toBeNull();
  });
});

describe("readModel", () => {
  it("restores the recorded window", () => {
    const model = readModel(
      row({
        name: "read",
        arguments: '{"file_path":"src/a.ts"}',
        result: "1: alpha\n2: beta",
        meta: {
          path: "src/a.ts",
          offset: 1,
          totalLines: 2,
          lang: "ts",
          lines: [
            { number: 1, text: "alpha" },
            { number: 2, text: "beta" },
          ],
        },
      }),
    );
    expect(model).toMatchObject({ path: "src/a.ts", offset: 1, totalLines: 2, lang: "ts" });
    expect(model?.lines).toEqual([
      { number: 1, text: "alpha" },
      { number: 2, text: "beta" },
    ]);
  });

  it("returns null without valid line metadata", () => {
    expect(readModel(row({ name: "read", arguments: "{}", result: "" }))).toBeNull();
    expect(
      readModel(
        row({
          name: "read",
          arguments: "{}",
          result: "",
          meta: { path: "a", offset: 1, totalLines: 1, lines: [{ number: 1 }] },
        }),
      ),
    ).toBeNull();
  });
});

describe("diffModel", () => {
  it("keeps update hunks and a create result", () => {
    const update = diffModel(
      row({
        name: "edit",
        arguments: '{"file_path":"e.txt"}',
        result: "updated",
        meta: {
          operation: "update",
          diffs: [{ path: "e.txt", oldText: "alpha", newText: "A" }],
        },
      }),
    );
    expect(update).toMatchObject({ operation: "update", path: "e.txt" });
    expect(update?.hunks).toEqual([{ path: "e.txt", oldText: "alpha", newText: "A" }]);
    expect(
      diffModel(
        row({
          name: "write",
          arguments: '{"file_path":"m.txt","content":"x"}',
          result: "created",
          meta: { operation: "create", diffs: [] },
        }),
      ),
    ).toMatchObject({ operation: "create", path: "m.txt", hunks: [] });
  });
});

describe("image and web models", () => {
  it("reads the image path and detail text", () => {
    expect(
      imageModel(
        row({
          name: "read_image",
          arguments: '{"file_path":"a.png"}',
          result: "image/png image, 10x20 px, 30 bytes",
          meta: { path: "a.png" },
        }),
      ),
    ).toMatchObject({ path: "a.png" });
  });

  it("summarizes web search queries and fetch status", () => {
    expect(
      webSearchModel(
        row({ name: "web_search", arguments: '{"queries":["one","two"]}', result: "hits" }),
      ),
    ).toMatchObject({ summary: "one · two", text: "hits" });
    expect(
      webFetchModel(
        row({
          name: "web_fetch",
          arguments: '{"url":"https://example.com"}',
          result: "Fetched https://example.com (HTTP 200)\n\nbody",
        }),
      ),
    ).toMatchObject({ summary: "https://example.com · HTTP 200" });
  });
});