import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RightDock } from "../src/ui/RightDock";
import {
  DOCK_MIN_WIDTH,
  clampDockWidth,
  loadDockLayout,
  maxDockWidth,
  saveDockLayout,
  type DockLayout,
} from "../src/app/rightDock";

function layout(overrides: Partial<DockLayout> = {}): DockLayout {
  return { view: "console", width: 320, collapsed: false, fullscreen: false, ...overrides };
}

function renderDock(overrides: Partial<DockLayout> = {}) {
  const onView = vi.fn();
  const onWidth = vi.fn();
  const onCollapse = vi.fn();
  const onToggleFullscreen = vi.fn();
  const view = render(
    <RightDock
      layout={layout(overrides)}
      onView={onView}
      onWidth={onWidth}
      onCollapse={onCollapse}
      onToggleFullscreen={onToggleFullscreen}
      top={<div>approvals</div>}
    >
      <div>body</div>
    </RightDock>,
  );
  return { ...view, onView, onWidth, onCollapse, onToggleFullscreen };
}

describe("RightDock", () => {
  it("switches tabs, collapses and toggles fullscreen", () => {
    const dock = renderDock();
    fireEvent.click(screen.getByRole("tab", { name: "文件" }));
    expect(dock.onView).toHaveBeenCalledWith("files");
    fireEvent.click(screen.getByRole("button", { name: "收起右侧栏" }));
    expect(dock.onCollapse).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "全屏右侧栏" }));
    expect(dock.onToggleFullscreen).toHaveBeenCalled();
    expect(screen.getByText("approvals")).toBeTruthy();
    expect(screen.getByText("body")).toBeTruthy();
  });

  it("marks fullscreen and hides the resize handle from the tab order", () => {
    renderDock({ fullscreen: true });
    const panel = screen.getByRole("complementary", { name: "右侧栏" });
    expect(panel.className).toContain("fullscreen");
    expect(screen.getByRole("button", { name: "还原右侧栏" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(screen.getByRole("separator").getAttribute("tabindex")).toBe("-1");
  });

  it("resizes from the keyboard and clamps through its declared range", () => {
    const dock = renderDock();
    const separator = screen.getByRole("separator", { name: "调整右侧栏宽度" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(dock.onWidth).toHaveBeenLastCalledWith(336);
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(dock.onWidth).toHaveBeenLastCalledWith(304);
    fireEvent.keyDown(separator, { key: "Home" });
    expect(dock.onWidth).toHaveBeenLastCalledWith(DOCK_MIN_WIDTH);
    fireEvent.keyDown(separator, { key: "End" });
    expect(dock.onWidth).toHaveBeenLastCalledWith(maxDockWidth());
  });
});

describe("right dock layout storage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("clamps widths to the visible range", () => {
    expect(clampDockWidth(10, 1280)).toBe(DOCK_MIN_WIDTH);
    expect(clampDockWidth(5000, 1280)).toBe(maxDockWidth(1280));
    expect(clampDockWidth(Number.NaN, 1280)).toBe(320);
  });

  it("round-trips a session layout and ignores malformed records", () => {
    saveDockLayout("s1", layout({ view: "terminal", width: 410 }));
    expect(loadDockLayout("s1")).toMatchObject({ view: "terminal", width: 410, collapsed: false });
    localStorage.setItem("dsh.rightdock.v1.s1", "{\"view\":\"bogus\"}");
    expect(loadDockLayout("s1")).toMatchObject({ view: "console", collapsed: true });
    expect(loadDockLayout(null).collapsed).toBe(true);
  });
});
