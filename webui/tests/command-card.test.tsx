import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CommandCard } from "../src/ui/CommandCard";
import { groupRows } from "../src/trajectory/model";
import type { EventEnvelope } from "../src/wire";

function envelope(type: string, seq: number, data: Record<string, unknown>): EventEnvelope {
  return { type, seq, data };
}

function goalEvents(kind: "success" | "error", text: string): EventEnvelope[] {
  return [
    envelope("command/run", 0, { commandId: "c1", name: "goal", source: { kind: "user" } }),
    envelope("command/done", 1, { commandId: "c1", kind, text }),
  ];
}

describe("command rows", () => {
  it("folds a command/run and command/done pair into one command row", () => {
    const text = "No goal is currently set.\nUsage: /goal [<objective>|clear|edit <objective>|pause|resume]";
    const rows = groupRows(goalEvents("error", text));
    expect(rows).toEqual([expect.objectContaining({
      kind: "command",
      name: "goal",
      outcome: { kind: "error", text },
    })]);
  });

  it("keeps a running command open until command/done arrives", () => {
    const rows = groupRows([
      envelope("command/run", 0, { commandId: "c1", name: "goal", source: { kind: "user" } }),
    ]);
    expect(rows[0]).toMatchObject({ kind: "command", name: "goal", outcome: null });
  });
});

describe("CommandCard", () => {
  it("exposes the goal usage as the command button name", () => {
    const text = "No goal is currently set.\nUsage: /goal [<objective>|clear|edit <objective>|pause|resume]";
    const [row] = groupRows(goalEvents("error", text));
    if (row.kind !== "command") throw new Error("expected a command row");
    render(<CommandCard row={row} />);
    expect(screen.getByRole("button", {
      name: "goal No goal is currently set. Usage: /goal [<objective>|clear|edit <objective>|pause|resume]",
    })).toBeTruthy();
  });

  it("expands multiline output on the row click", () => {
    const text = "Goal set\nStatus: active\nObjective: ship";
    const [row] = groupRows(goalEvents("success", text));
    if (row.kind !== "command") throw new Error("expected a command row");
    render(<CommandCard row={row} />);
    expect(document.querySelector(".command-card-body")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /goal Goal set/ }));
    expect(document.querySelector(".command-card-body")?.textContent).toContain("Status: active");
  });
});
