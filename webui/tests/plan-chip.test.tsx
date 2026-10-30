// PlanChip (upstream ui-plan PlanModeControl): visible only while the
// effective target is plan mode; click runs /plan off.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PlanChip } from "../src/composer/PlanChip";

describe("PlanChip", () => {
  it("hides when the projection is absent or the target is off", () => {
    const missing = render(<PlanChip plan={null} locked={false} onExit={vi.fn(async () => null)} />);
    expect(missing.container.firstChild).toBeNull();
    missing.unmount();

    const off = render(
      <PlanChip
        plan={{ active: false, pending: false }}
        locked={false}
        onExit={vi.fn(async () => null)}
      />,
    );
    expect(off.container.firstChild).toBeNull();
  });

  it("shows while active or while a turn-on command is pending", () => {
    const { rerender } = render(
      <PlanChip
        plan={{ active: true, pending: false }}
        locked={false}
        onExit={vi.fn(async () => null)}
      />,
    );
    expect(screen.getByRole("button", { name: "计划模式已开启，按下关闭" })).toBeTruthy();
    expect(screen.getByText("计划")).toBeTruthy();

    rerender(
      <PlanChip
        plan={{ active: false, pending: true }}
        locked={false}
        onExit={vi.fn(async () => null)}
      />,
    );
    expect(screen.getByRole("button", { name: "计划模式已开启，按下关闭" })).toBeTruthy();
  });

  it("disables while locked or leaving, and reports an exit failure", async () => {
    const onExit = vi.fn(async () => "command refused");
    render(
      <PlanChip plan={{ active: true, pending: false }} locked={false} onExit={onExit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "计划模式已开启，按下关闭" }));
    await waitFor(() => expect(onExit).toHaveBeenCalledTimes(1));
    expect(screen.getByText("退出计划模式失败")).toBeTruthy();
    expect(screen.getByText("退出计划模式失败").getAttribute("title")).toBe("command refused");
  });

  it("does not click while the composer is locked", () => {
    const onExit = vi.fn(async () => null);
    render(<PlanChip plan={{ active: true, pending: false }} locked onExit={onExit} />);
    expect(
      (screen.getByRole("button", { name: "计划模式已开启，按下关闭" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
