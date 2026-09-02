// Trajectory component tests: Overview bar, collapse/expand, search, and
// virtualized (windowed) rendering for large sessions.

import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Trajectory } from "../src/ui/Trajectory";
import { bigTurn, standardSession } from "./fixtures";

function scrollerOf(container: HTMLElement): HTMLElement {
  const el = container.querySelector(".trajectory-scroll");
  if (!el) throw new Error("missing .trajectory-scroll");
  return el as HTMLElement;
}

describe("Trajectory Overview", () => {
  it("renders one chip per turn with message/tool counts", () => {
    const { container } = render(<Trajectory events={standardSession()} running={false} />);
    const chips = container.querySelectorAll(".turn-chip");
    expect(chips.length).toBe(2);
    expect(chips[0]!.textContent).toContain("回合 1");
    expect(chips[0]!.textContent).toContain("2 条消息");
    expect(chips[0]!.textContent).toContain("1 工具");
    expect(chips[1]!.textContent).toContain("回合 2");
  });

  it("jumps to a turn on chip click", () => {
    const { container } = render(<Trajectory events={standardSession()} running={false} />);
    const scroller = scrollerOf(container);
    fireEvent.click(container.querySelectorAll(".turn-chip")[1]!);
    expect(scroller.scrollTop).toBeGreaterThan(0);
  });
});

describe("Trajectory collapse", () => {
  it("collapses every turn into a summary row and expands on click", () => {
    const { container } = render(<Trajectory events={standardSession()} running={false} />);
    fireEvent.click(screen.getByRole("button", { name: "折叠全部" }));
    expect(container.querySelectorAll(".vrow.summary").length).toBe(2);
    expect(screen.queryByText(/回答一下问题/)).toBeFalsy();
    expect(screen.queryByText("你好世界")).toBeTruthy();
    fireEvent.click(container.querySelectorAll(".vrow.summary")[0]!);
    expect(container.querySelectorAll(".vrow.summary").length).toBe(1);
    expect(screen.getByText(/回答一下问题/)).toBeTruthy();
  });
});

describe("Trajectory search", () => {
  it("filters rows, shows hit count and highlights matches", () => {
    const { container } = render(<Trajectory events={standardSession()} running={false} />);
    const input = container.querySelector(".searchbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "回答" } });
    expect(container.querySelector(".matchcount")!.textContent).toBe("命中 1 行");
    expect(container.querySelectorAll(".vrow").length).toBe(1);
    expect(container.querySelectorAll("mark").length).toBe(1);
  });

  it("reports no match without crashing on remaining events", () => {
    const { container } = render(<Trajectory events={standardSession()} running={false} />);
    const input = container.querySelector(".searchbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(container.querySelector(".matchcount")!.textContent).toBe("无匹配");
    expect(container.querySelectorAll(".vrow").length).toBe(0);
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "折叠全部" }));
    expect(container.querySelectorAll(".vrow.summary").length).toBe(2);
  });
});

describe("Trajectory virtualization", () => {
  it("keeps the mounted DOM bounded for a large session", () => {
    const { container } = render(<Trajectory events={bigTurn(300)} running={false} />);
    const mounted = container.querySelectorAll(".vrow").length;
    expect(mounted).toBeGreaterThan(0);
    expect(mounted).toBeLessThan(300);
  });

  it("scrolls the window and drops off-screen rows from the DOM", () => {
    const { container } = render(<Trajectory events={bigTurn(300)} running={false} />);
    const scroller = scrollerOf(container);
    Object.defineProperty(scroller, "clientHeight", { configurable: true, value: 300 });
    expect(screen.queryByText(/^消息 0 /)).toBeTruthy();
    scroller.scrollTop = 6000;
    fireEvent.scroll(scroller);
    const mounted = container.querySelectorAll(".vrow").length;
    expect(mounted).toBeLessThan(300);
    expect(screen.queryByText(/^消息 0 /)).toBeFalsy();
    expect(screen.queryByText(/^消息 120 /)).toBeTruthy();
  });
});

describe("Trajectory states", () => {
  it("shows the waiting empty state and the running footer", () => {
    const { container } = render(<Trajectory events={[]} running={true} />);
    expect(container.textContent).toContain("跟随会话中…");
  });

  it("shows the running footer while the agent is live", () => {
    const { container } = render(<Trajectory events={standardSession()} running={true} />);
    expect(container.textContent).toContain("agent 运行中…");
  });
});