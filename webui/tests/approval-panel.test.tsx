// Approval takeover: reason resolution (localized first), correlated tool
// detail, keyboard decisions, and the answered state.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApprovalPanel } from "../src/ui/ApprovalPanel";
import type { PendingApproval } from "../src/app/useBackend";
import type { ToolRow } from "../src/trajectory/model";

function approval(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    eventId: "event-1",
    agentId: "s1",
    toolName: "bash",
    ...overrides,
  };
}

const DETAIL: ToolRow = {
  kind: "tool",
  key: "tool:7",
  turn: 1,
  seq: 7,
  callId: "call-1",
  name: "bash",
  arguments: JSON.stringify({ command: "rm -rf build" }),
  result: "removed",
  isError: false,
  meta: null,
  height: 60,
};

describe("ApprovalPanel", () => {
  it("prefers the localized reason, then the audit reason, then the generic copy", () => {
    const { unmount } = render(
      <ApprovalPanel
        approval={approval({ displayReason: { zh: "需要写入构建目录", en: "needs build access" } })}
        detail={null}
        onAnswer={vi.fn(async () => undefined)}
      />,
    );
    expect(screen.getByText("需要写入构建目录")).toBeTruthy();
    expect(screen.queryByText("needs build access")).toBeNull();
    unmount();

    const second = render(
      <ApprovalPanel
        approval={approval({ reason: "needs access", displayReason: { zh: "" } })}
        detail={null}
        onAnswer={vi.fn(async () => undefined)}
      />,
    );
    expect(screen.getByText("needs access")).toBeTruthy();
    second.unmount();

    const third = render(
      <ApprovalPanel
        approval={approval({ toolName: "" })}
        detail={null}
        onAnswer={vi.fn(async () => undefined)}
      />,
    );
    expect(screen.getByText("工具 (未知工具) 请求越权执行")).toBeTruthy();
    third.unmount();

    render(
      <ApprovalPanel
        approval={approval()}
        detail={null}
        onAnswer={vi.fn(async () => undefined)}
      />,
    );
    expect(screen.getByText("工具 bash 请求越权执行")).toBeTruthy();
  });

  it("renders the correlated tool call under the reason", () => {
    render(
      <ApprovalPanel
        approval={approval({ callId: "call-1", reason: "needs access" })}
        detail={DETAIL}
        onAnswer={vi.fn(async () => undefined)}
      />,
    );
    const detail = screen.getByRole("group", { name: "审批详情" });
    expect(within(detail).getByText(/rm -rf build/u)).toBeTruthy();
    expect(detail.querySelector(".approval-detail")).not.toBeNull();
  });

  it("answers allow once and reject through the buttons", async () => {
    const allow = vi.fn(async () => undefined);
    const first = render(<ApprovalPanel approval={approval()} detail={null} onAnswer={allow} />);
    fireEvent.click(screen.getByRole("button", { name: "允许一次" }));
    await waitFor(() => expect(allow).toHaveBeenCalledWith(expect.anything(), "allowed-once"));
    first.unmount();

    const reject = vi.fn(async () => undefined);
    render(<ApprovalPanel approval={approval()} detail={null} onAnswer={reject} />);
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    await waitFor(() => expect(reject).toHaveBeenCalledWith(expect.anything(), "rejected"));
  });

  it("maps Enter to allow once and Escape to reject from the detail pane", async () => {
    const allow = vi.fn(async () => undefined);
    const first = render(<ApprovalPanel approval={approval()} detail={null} onAnswer={allow} />);
    const body = screen.getByRole("group", { name: "审批详情" });
    body.focus();
    fireEvent.keyDown(body, { key: "Enter" });
    await waitFor(() => expect(allow).toHaveBeenCalledWith(expect.anything(), "allowed-once"));
    first.unmount();

    const reject = vi.fn(async () => undefined);
    render(<ApprovalPanel approval={approval()} detail={null} onAnswer={reject} />);
    const second = screen.getByRole("group", { name: "审批详情" });
    second.focus();
    fireEvent.keyDown(second, { key: "Escape" });
    await waitFor(() => expect(reject).toHaveBeenCalledWith(expect.anything(), "rejected"));
  });

  it("leaves modified keys, repeats and button activation to their owners", () => {
    const onAnswer = vi.fn(async () => undefined);
    render(<ApprovalPanel approval={approval()} detail={null} onAnswer={onAnswer} />);
    const body = screen.getByRole("group", { name: "审批详情" });
    body.focus();
    fireEvent.keyDown(body, { key: "Enter", ctrlKey: true });
    fireEvent.keyDown(body, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(body, { key: "Escape", altKey: true });
    fireEvent.keyDown(body, { key: "Enter", repeat: true });
    fireEvent.keyDown(body, { key: "a" });

    const reject = screen.getByRole("button", { name: "拒绝" });
    reject.focus();
    fireEvent.keyDown(reject, { key: "Enter" });
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("shows the answered state and stops accepting a second decision", async () => {
    let settle: (() => void) | null = null;
    const onAnswer = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const { container } = render(
      <ApprovalPanel approval={approval()} detail={null} onAnswer={onAnswer} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "允许一次" }));
    expect(container.querySelector(".approval-takeover")?.getAttribute("aria-busy")).toBe("true");
    expect((screen.getByRole("button", { name: "允许一次" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "拒绝" }) as HTMLButtonElement).disabled).toBe(true);

    const body = screen.getByRole("group", { name: "审批详情" });
    body.focus();
    fireEvent.keyDown(body, { key: "Escape" });
    expect(onAnswer).toHaveBeenCalledTimes(1);

    settle!();
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(1));
  });

  it("re-enables the decision when the answer write fails", async () => {
    let fail: (() => void) | null = null;
    const onAnswer = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          fail = () => reject(new Error("$events/result failed"));
        }),
    );
    render(<ApprovalPanel approval={approval()} detail={null} onAnswer={onAnswer} />);
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "拒绝" }) as HTMLButtonElement).disabled).toBe(true),
    );
    fail!();
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "拒绝" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    expect(onAnswer).toHaveBeenCalledTimes(2);
  });
});
