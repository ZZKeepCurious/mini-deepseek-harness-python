// Current-session permission seat: catalog menu, preset switching, and the
// risk acknowledgement gating full access and experimental auto review.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { PermissionSelect } from "../src/composer/PermissionSelect";
import type { PermissionCatalog, PermissionSelection } from "../src/wire";

const CATALOG: PermissionCatalog = {
  options: [
    { value: "read-only", name: "read-only", description: "Read only files" },
    { value: "workspace-write", name: "workspace-write", description: "Write the workspace" },
    { value: "danger-full-access", name: "danger-full-access" },
    { value: "auto", name: "auto" },
  ],
  defaultOptions: [
    { value: "read-only", name: "read-only" },
    { value: "workspace-write", name: "workspace-write" },
    { value: "danger-full-access", name: "danger-full-access" },
  ],
  defaultPreset: "workspace-write",
};

const SELECTION: PermissionSelection = { currentValue: "workspace-write" };

function trigger(): HTMLButtonElement {
  return screen.getByRole("button", { name: "访问模式，当前：工作区内修改" }) as HTMLButtonElement;
}

describe("PermissionSelect", () => {
  it("renders nothing when the host exposes no permission capability", () => {
    const { container } = render(
      <PermissionSelect
        selection={null}
        catalog={CATALOG}
        locked={false}
        onSelect={vi.fn(async () => undefined)}
      />,
    );
    expect(container.querySelector(".permission-select")).toBeNull();
  });

  it("renders nothing while the catalog read has not settled", () => {
    const { container } = render(
      <PermissionSelect
        selection={SELECTION}
        catalog={null}
        locked={false}
        onSelect={vi.fn(async () => undefined)}
      />,
    );
    expect(container.querySelector(".permission-select")).toBeNull();
  });

  it("lists the catalog under product labels and marks the active preset", () => {
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={vi.fn(async () => undefined)}
      />,
    );
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(trigger());
    const menu = screen.getByRole("menu", { name: "权限模式" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "仅可查看",
      "工作区内修改",
      "完全权限",
      "自动审查EXP",
    ]);
    expect(items[0].getAttribute("aria-current")).toBeNull();
    expect(items[1].getAttribute("aria-current")).toBe("true");
    expect(items[3].querySelector("sup")?.textContent).toBe("EXP");
    expect(items[0].getAttribute("title")).toBe("Read only files");
    expect(items[3].getAttribute("title")).toContain("无沙箱运行");
    expect(screen.getByRole("button", { name: "访问模式，当前：工作区内修改" })).toBeTruthy();
  });

  it("switches to an ungated preset and closes the menu", async () => {
    const onSelect = vi.fn(async () => undefined);
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "仅可查看" }));
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("read-only"));
    expect(screen.queryByRole("menu", { name: "权限模式" })).toBeNull();
  });

  it("ignores the already-selected preset", () => {
    const onSelect = vi.fn(async () => undefined);
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "工作区内修改" }));
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu", { name: "权限模式" })).toBeNull();
  });

  it("gates full access behind the acknowledgement and then switches", async () => {
    const onSelect = vi.fn(async () => undefined);
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "完全权限" }));

    const dialog = screen.getByRole("dialog", { name: "确认启用完全权限？" });
    expect(within(dialog).getByRole("button", { name: "启用完全权限" })).toBeTruthy();
    const confirm = within(dialog).getByRole("button", {
      name: "启用完全权限",
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "我已了解风险，并愿意继续" }));
    expect((within(dialog).getByRole("button", { name: "启用完全权限" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "启用完全权限" }));
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("danger-full-access"));
    expect(screen.queryByRole("dialog", { name: "确认启用完全权限？" })).toBeNull();
  });

  it("gates experimental auto review with its own copy", async () => {
    const onSelect = vi.fn(async () => undefined);
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "自动审查 EXP" }));

    const dialog = screen.getByRole("dialog", { name: "确认启用自动审查（实验）？" });
    expect(within(dialog).getByText(/自动审查不使用沙箱/u)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "我已了解这些风险，并愿意继续" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "启用自动审查" }));
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("auto"));
  });

  it("cancels the risk dialog without switching", () => {
    const onSelect = vi.fn(async () => undefined);
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "完全权限" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "确认启用完全权限？" })).toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("closes the menu on Escape and on an outside press", () => {
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={vi.fn(async () => undefined)}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.keyDown(screen.getByRole("menu", { name: "权限模式" }), { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "权限模式" })).toBeNull();
    expect(document.activeElement).toBe(trigger());

    fireEvent.click(trigger());
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu", { name: "权限模式" })).toBeNull();
  });

  it("moves focus through the menu with the arrow keys", () => {
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={vi.fn(async () => undefined)}
      />,
    );
    fireEvent.click(trigger());
    const menu = screen.getByRole("menu", { name: "权限模式" });
    const items = within(menu).getAllByRole("menuitem");
    items[0].focus();
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(items[3]);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(document.activeElement).toBe(items[3]);
  });

  it("locks the trigger while the composer or a switch is busy", async () => {
    let settle: (() => void) | null = null;
    const onSelect = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const { rerender } = render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    const triggerEl = trigger();
    fireEvent.click(triggerEl);
    fireEvent.click(screen.getByRole("menuitem", { name: "仅可查看" }));
    await waitFor(() => expect(triggerEl.disabled).toBe(true));
    settle!();
    await waitFor(() => expect(triggerEl.disabled).toBe(false));

    rerender(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked
        onSelect={onSelect}
      />,
    );
    expect(trigger().disabled).toBe(true);
    fireEvent.click(trigger());
    expect(screen.queryByRole("menu", { name: "权限模式" })).toBeNull();
  });

  it("clears its pending mark when the switch fails", async () => {
    const onSelect = vi.fn(async () => {
      throw new Error("session/command-failed");
    });
    render(
      <PermissionSelect
        selection={SELECTION}
        catalog={CATALOG}
        locked={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("menuitem", { name: "仅可查看" }));
    await waitFor(() => expect(trigger().disabled).toBe(false));
    expect(onSelect).toHaveBeenCalledWith("read-only");
  });
});
