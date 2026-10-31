import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SchedulePanel } from "../src/ui/SchedulePanel";

describe("SchedulePanel", () => {
  it("lists schedules and deletes one", () => {
    const onDelete = vi.fn();
    const onRefresh = vi.fn();
    render(
      <SchedulePanel
        sessionId="s1"
        schedules={[
          {
            id: "sch-1",
            kind: "after",
            title: "Reminder",
            prompt: "Check inbox",
            scheduledAt: "2026-09-16T15:00:00.120Z",
          },
        ]}
        catalog={[]}
        loading={false}
        catalogLoading={false}
        error={null}
        catalogError={null}
        onRefresh={onRefresh}
        onRefreshCatalog={() => {}}
        onDelete={onDelete}
        onUpdate={() => {}}
        onLoadHistory={async () => []}
      />,
    );

    expect(screen.getByText("Reminder")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "刷新日程" }));
    expect(onRefresh).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除日程 Reminder" }));
    expect(onDelete).toHaveBeenCalledWith("sch-1", "s1");
  });

  it("prompts to select a session when idle", () => {
    render(
      <SchedulePanel
        sessionId={null}
        schedules={[]}
        catalog={[]}
        loading={false}
        catalogLoading={false}
        error={null}
        catalogError={null}
        onRefresh={() => {}}
        onRefreshCatalog={() => {}}
        onDelete={() => {}}
        onUpdate={() => {}}
        onLoadHistory={async () => []}
      />,
    );
    expect(screen.getByText(/选择左侧会话/)).toBeTruthy();
  });
});
