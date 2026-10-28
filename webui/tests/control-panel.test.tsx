import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ControlPanel } from "../src/ui/ControlPanel";

describe("ControlPanel queue actions", () => {
  it("edits, steers and removes queued messages", () => {
    const onQueueEdit = vi.fn();
    const onQueueRemove = vi.fn();
    const onQueueSteer = vi.fn();
    render(
      <ControlPanel
        queue={[{ id: "m1", content: [{ type: "text", text: "hello" }] }]}
        jobs={[]}
        running
        sessionId="s1"
        onQueueEdit={onQueueEdit}
        onQueueRemove={onQueueRemove}
        onQueueSteer={onQueueSteer}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByRole("textbox", { name: "编辑队列消息" }), {
      target: { value: "edited" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onQueueEdit).toHaveBeenCalledWith("m1", "edited");

    fireEvent.click(screen.getByRole("button", { name: "插话" }));
    expect(onQueueSteer).toHaveBeenCalledWith("m1");

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(onQueueRemove).toHaveBeenCalledWith("m1");
  });
});