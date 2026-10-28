import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ToolRow } from "../src/trajectory/model";
import { ToolCallView } from "../src/ui/tools/ToolCallView";

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

describe("ToolCallView", () => {
  it("renders an expandable Bash card with cwd, command and output", () => {
    render(
      <ToolCallView
        row={row({
          name: "bash",
          arguments: JSON.stringify({
            command: "node ./scripts/start-worker.mjs --workspace=/tmp/diagnostics",
            description: "Start the diagnostic worker",
          }),
          result: "done\n[exit code: 0]",
          meta: { cwd: "/workspace" },
        })}
        terms={[]}
      />,
    );
    const head = screen.getByRole("button", { name: /Bash Start the diagnostic worker/ });
    expect(head.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(head);
    expect(head.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("/workspace")).toBeTruthy();
    expect(screen.getByText(/node \.\/scripts\/start-worker/)).toBeTruthy();
    expect(screen.getByText("done")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看原始" }));
    expect(screen.getByText(/"arguments"/)).toBeTruthy();
  });

  it("renders grouped grep matches and the truncation recovery", () => {
    const { container } = render(
      <ToolCallView
        row={row({
          name: "grep",
          arguments: '{"pattern":"search"}',
          result: [
            "src/a.ts:",
            "  16: export const DEFAULT_SEARCH_MAX_LINES = 16",
            "  138: export function SearchBlock()",
            "(Showing the first 2 matches; narrow pattern or path to see more.)",
          ].join("\n"),
        })}
        terms={["search"]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Grep 显示 2 条匹配/ }));
    expect(screen.getByText("src/a.ts")).toBeTruthy();
    expect(container.textContent).toContain("DEFAULT_SEARCH_MAX_LINES");
    expect(screen.getByText(/Showing the first 2 matches/)).toBeTruthy();
    expect(container.querySelectorAll("mark").length).toBeGreaterThan(0);
  });

  it("renders todo counts, active item and per-status rows", () => {
    render(
      <ToolCallView
        row={row({
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
        })}
        terms={[]}
      />,
    );
    const head = screen.getByRole("button", { name: /1\/4 已完成 · 实现 fixture 样本/ });
    expect(head.textContent).toContain("+1");
    fireEvent.click(head);
    expect(screen.getByText("1 已完成 · 2 进行中 · 1 待办")).toBeTruthy();
    expect(screen.getByText("梳理需求")).toBeTruthy();
    expect(screen.getAllByText("进行中").length).toBe(2);
    expect(screen.getByText("待办")).toBeTruthy();
  });

  it("renders a recorded read window", () => {
    render(
      <ToolCallView
        row={row({
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
        })}
        terms={[]}
      />,
    );
    const head = screen.getByRole("button", { name: /读取文件 src\/a.ts/ });
    fireEvent.click(head);
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("beta")).toBeTruthy();
    expect(head.textContent).toContain("行 1-2 / 2");
  });

  it("renders edit hunks and create state", () => {
    render(
      <ToolCallView
        row={row({
          name: "edit",
          arguments: '{"file_path":"e.txt"}',
          result: "updated",
          meta: {
            operation: "update",
            diffs: [{ path: "e.txt", oldText: "alpha", newText: "A" }],
          },
        })}
        terms={[]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /编辑文件 e.txt/ }));
    expect(screen.getByText("- alpha")).toBeTruthy();
    expect(screen.getByText("+ A")).toBeTruthy();
  });

  it("renders web search results as markdown", () => {
    render(
      <ToolCallView
        row={row({
          name: "web_search",
          arguments: '{"queries":["one","two"]}',
          result: "Found [result](https://example.com).",
        })}
        terms={[]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Web 搜索 one · two/ }));
    expect(screen.getByRole("link", { name: "result" })).toBeTruthy();
  });

  it("falls back to the generic card for unknown tools", () => {
    const { container } = render(
      <ToolCallView
        row={row({ name: "git", arguments: '{"cmd":"status"}', result: "clean" })}
        terms={["clean"]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /git clean/ }));
    expect(container.textContent).toContain('{"cmd":"status"}');
    expect(container.querySelector(".tool-output")?.textContent).toContain("clean");
  });
});