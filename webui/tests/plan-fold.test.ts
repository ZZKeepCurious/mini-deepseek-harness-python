// Client fold of the `plan` projection (upstream plan-mode wire.view).
// pending = wanted differs from the committed active flag.

import { describe, expect, it } from "vitest";
import { foldPlanProjection, planChipTarget, planSelectionOf } from "../src/wire/plan";
import type { EventEnvelope } from "../src/wire";

function event(type: string, data: Record<string, unknown>, seq = 1): EventEnvelope {
  return { type, seq, data };
}

describe("planSelectionOf", () => {
  it("reads the host {active, pending} unit and ignores a malformed value", () => {
    expect(planSelectionOf({ active: true, pending: false })).toEqual({
      active: true,
      pending: false,
    });
    expect(planSelectionOf({ active: "yes" })).toBeNull();
    expect(planSelectionOf(null)).toBeNull();
  });
});

describe("planChipTarget", () => {
  it("uses the pending invert while a /plan command is unsettled", () => {
    expect(planChipTarget({ active: false, pending: true })).toBe(true);
    expect(planChipTarget({ active: true, pending: true })).toBe(false);
    expect(planChipTarget({ active: true, pending: false })).toBe(true);
    expect(planChipTarget({ active: false, pending: false })).toBe(false);
  });
});

describe("foldPlanProjection", () => {
  it("stays off on an empty log and turns on at plan/mode", () => {
    expect(foldPlanProjection([])).toEqual({ active: false, pending: false });
    expect(
      foldPlanProjection([event("plan/mode", { active: true })]),
    ).toEqual({ active: true, pending: false });
  });

  it("marks pending only after a successful /plan that changes the mode", () => {
    const running = [
      event("command/run", { name: "plan", commandId: "c1", args: "" }),
    ];
    expect(foldPlanProjection(running)).toEqual({ active: false, pending: true });

    const settled = [
      ...running,
      event("command/done", { commandId: "c1", kind: "success" }),
    ];
    expect(foldPlanProjection(settled)).toEqual({ active: false, pending: true });

    const committed = [...settled, event("plan/mode", { active: true })];
    expect(foldPlanProjection(committed)).toEqual({ active: true, pending: false });
  });

  it("clears pending when the command fails or is a no-op", () => {
    expect(
      foldPlanProjection([
        event("command/run", { name: "plan", commandId: "c1", args: "" }),
        event("command/done", { commandId: "c1", kind: "error" }),
      ]),
    ).toEqual({ active: false, pending: false });

    expect(
      foldPlanProjection([
        event("plan/mode", { active: true }),
        event("command/run", { name: "plan", commandId: "c2", args: "" }),
        event("command/done", { commandId: "c2", kind: "success" }),
      ]),
    ).toEqual({ active: true, pending: false });
  });
});
