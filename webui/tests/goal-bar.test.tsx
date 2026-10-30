// GoalBar (upstream ui-goal GoalBar): phase label, objective, CAS actions,
// inline edit, and hide-when-absent/complete.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { GoalBar } from "../src/ui/GoalBar";
import type { GoalView } from "../src/wire/goals";

function goal(overrides: Partial<GoalView> = {}): GoalView {
  return {
    id: "goal-1",
    revision: 1,
    objective: "ship the release",
    phase: "active",
    maxGoalRounds: 8,
    activation: "armed",
    ...overrides,
  };
}

function renderBar(view: GoalView | null | undefined, handlers: Record<string, ReturnType<typeof vi.fn>> = {}) {
  const onEdit = handlers.onEdit ?? vi.fn(async () => ({ ok: true as const }));
  const onPause = handlers.onPause ?? vi.fn(async () => ({ ok: true as const }));
  const onResume = handlers.onResume ?? vi.fn(async () => ({ ok: true as const }));
  const onClear = handlers.onClear ?? vi.fn(async () => ({ ok: true as const }));
  const result = render(
    <GoalBar
      goal={view}
      onEdit={onEdit}
      onPause={onPause}
      onResume={onResume}
      onClear={onClear}
    />,
  );
  return { ...result, onEdit, onPause, onResume, onClear };
}

describe("GoalBar", () => {
  it("renders nothing while loading, absent, or complete", () => {
    const loading = renderBar(undefined);
    expect(loading.container.firstChild).toBeNull();
    loading.unmount();

    const absent = renderBar(null);
    expect(absent.container.firstChild).toBeNull();
    absent.unmount();

    const done = renderBar(goal({ phase: "complete" }));
    expect(done.container.firstChild).toBeNull();
  });

  it("shows the armed active label and pause/edit/clear", () => {
    renderBar(goal());
    expect(screen.getByText("进行中的目标")).toBeTruthy();
    expect(screen.getByText("ship the release")).toBeTruthy();
    expect(screen.getByRole("button", { name: "暂停目标" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "恢复目标" })).toBeNull();
    expect(screen.getByRole("button", { name: "编辑目标" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "清除目标" })).toBeTruthy();
  });

  it("treats a disarmed active goal as paused and offers resume", () => {
    renderBar(goal({ activation: "disarmed" }));
    expect(screen.getByText("已暂停的目标")).toBeTruthy();
    expect(screen.getByRole("button", { name: "恢复目标" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "暂停目标" })).toBeNull();
  });

  it("shows the blocked reason as the strip title", () => {
    renderBar(
      goal({
        phase: "blocked",
        blockedReason: { code: "model-reported", message: "needs a human decision" },
      }),
    );
    expect(screen.getByText("受阻的目标")).toBeTruthy();
    expect(document.querySelector("[data-goal-bar] [title]")).toHaveProperty(
      "title",
      "needs a human decision",
    );
  });

  it("pauses, resumes and clears through the injected verbs", async () => {
    const first = renderBar(goal());
    fireEvent.click(screen.getByRole("button", { name: "暂停目标" }));
    await waitFor(() => expect(first.onPause).toHaveBeenCalledTimes(1));
    first.unmount();

    const second = renderBar(goal({ phase: "paused", activation: "disarmed" }));
    fireEvent.click(screen.getByRole("button", { name: "恢复目标" }));
    await waitFor(() => expect(second.onResume).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "清除目标" }));
    await waitFor(() => expect(second.onClear).toHaveBeenCalledTimes(1));
    expect(second.container.firstChild).toBeNull();
  });

  it("edits the objective inline and reports a failed CAS", async () => {
    const onEdit = vi.fn(async () => ({
      ok: false as const,
      error: { code: "GOAL_STALE_REVISION", message: "stale goal ref" },
    }));
    renderBar(goal(), { onEdit });
    fireEvent.click(screen.getByRole("button", { name: "编辑目标" }));
    const field = screen.getByLabelText("目标内容") as HTMLInputElement;
    expect(field.value).toBe("ship the release");
    fireEvent.change(field, { target: { value: "ship it better" } });
    fireEvent.click(screen.getByRole("button", { name: "保存目标" }));
    await waitFor(() => expect(onEdit).toHaveBeenCalledWith("ship it better"));
    expect(screen.getByRole("alert").textContent).toBe("stale goal ref (GOAL_STALE_REVISION)");
  });
});
