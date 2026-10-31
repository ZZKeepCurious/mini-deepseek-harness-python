import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SessionList } from "../src/ui/SessionList";
import type { SessionSummary } from "../src/wire";

const SESSIONS: SessionSummary[] = [
  { sessionId: "s1", cwd: "/home/u/proj", running: false },
  { sessionId: "s2", cwd: "/home/u/old", running: false },
];

function renderList(overrides: Partial<Parameters<typeof SessionList>[0]> = {}) {
  return render(
    <SessionList
      sessions={SESSIONS}
      selectedId="s1"
      home="/home/u"
      archivedIds={["s2"]}
      showArchived={false}
      creating={false}
      onSelect={vi.fn()}
      onCreate={vi.fn()}
      onFork={vi.fn()}
      onArchive={vi.fn()}
      onUnarchive={vi.fn()}
      onShowArchived={vi.fn()}
      {...overrides}
    />,
  );
}

describe("SessionList", () => {
  it("shows a tilde path and keeps the original on the title", () => {
    renderList();
    const path = screen.getByText("~/proj");
    expect(path.getAttribute("title")).toBe("/home/u/proj");
    expect(screen.queryByText("/home/u/proj")).toBeNull();
    expect(screen.queryByText("~/old")).toBeNull();
  });

  it("offers fork and archive on the active row", () => {
    const onFork = vi.fn();
    const onArchive = vi.fn();
    renderList({ onFork, onArchive });
    fireEvent.click(screen.getByRole("button", { name: "分叉会话" }));
    expect(onFork).toHaveBeenCalledWith("s1");
    fireEvent.click(screen.getByRole("button", { name: "归档会话" }));
    expect(onArchive).toHaveBeenCalledWith("s1");
  });

  it("reveals archived sessions and lets them be restored", () => {
    const onUnarchive = vi.fn();
    const onShowArchived = vi.fn();
    renderList({ showArchived: true, onUnarchive, onShowArchived });
    expect(screen.getByText("~/old")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消归档" }));
    expect(onUnarchive).toHaveBeenCalledWith("s2");
    fireEvent.click(screen.getByRole("button", { name: "隐藏已归档" }));
    expect(onShowArchived).toHaveBeenCalledWith(false);
  });

  it("shows a create refusal and retries", () => {
    const onCreate = vi.fn();
    renderList({
      createError: "session/conflict: session \"s1\" belongs to \"/tmp\", not \"/other\"",
      onCreate,
    });
    expect(screen.getByText(/session\/conflict/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试新建" }));
    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it("offers directory browse when wired", () => {
    const onBrowseDirectory = vi.fn();
    renderList({ onBrowseDirectory });
    fireEvent.click(screen.getByRole("button", { name: "浏览目录" }));
    expect(onBrowseDirectory).toHaveBeenCalledTimes(1);
  });
});
