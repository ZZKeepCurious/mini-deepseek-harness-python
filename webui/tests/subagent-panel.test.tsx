import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SubagentPanel } from "../src/ui/SubagentPanel";

describe("SubagentPanel", () => {
  it("lists child sessions and opens one", () => {
    const onOpen = vi.fn();
    render(
      <SubagentPanel
        parentSessionId="parent"
        childSessions={[
          { sessionId: "child-1", parentSessionId: "parent", running: true },
          { sessionId: "child-2", parentSessionId: "parent", running: false },
        ]}
        onOpen={onOpen}
      />,
    );

    expect(screen.getByRole("button", { name: "打开子代理 child-1" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开子代理 child-1" }));
    expect(onOpen).toHaveBeenCalledWith("child-1");
  });

  it("hides when there are no children", () => {
    const { container } = render(
      <SubagentPanel parentSessionId="parent" childSessions={[]} onOpen={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
