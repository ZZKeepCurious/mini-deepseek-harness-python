// `goals/*` unary client (docs/interface-wire.md §4.12).
// Pins request wrapping, create-ref shape, and GoalView decoding.

import { describe, expect, it, vi } from "vitest";
import {
  clearGoal,
  createGoal,
  editGoal,
  getGoal,
  goalRefOf,
  goalViewOf,
  pauseGoal,
  resumeGoal,
} from "../src/wire/goals";
import type { GoalView } from "../src/wire/goals";

function ok(value: unknown): Response {
  return new Response(
    JSON.stringify({
      type: "server-response",
      rpcId: "rpc-1",
      result: { ok: true, value },
    }),
    { status: 200 },
  );
}

function fetchFor(value: unknown, inspect?: (body: Record<string, unknown>) => void) {
  return vi.fn(async (_url, init) => {
    const body = JSON.parse(String((init as RequestInit | undefined)?.body)) as Record<string, unknown>;
    inspect?.(body);
    return ok(value);
  });
}

const VIEW: GoalView = {
  id: "goal-1",
  revision: 2,
  objective: "ship it",
  phase: "active",
  maxGoalRounds: 4,
  activation: "armed",
};

describe("goalViewOf", () => {
  it("accepts a host view and rejects a missing or complete-only tombstone", () => {
    expect(goalViewOf(VIEW)).toEqual(VIEW);
    expect(goalViewOf(null)).toBeNull();
    expect(goalViewOf({ id: "goal-1", revision: 3 })).toBeNull();
    expect(goalViewOf({ ...VIEW, phase: "done" })).toBeNull();
  });

  it("keeps an optional blocked reason", () => {
    const blocked = goalViewOf({
      ...VIEW,
      phase: "blocked",
      blockedReason: { code: "model-reported", message: "needs a decision" },
    });
    expect(blocked?.blockedReason).toEqual({
      code: "model-reported",
      message: "needs a decision",
    });
  });
});

describe("goals unary", () => {
  it("reads the current view and wraps agentId under args", async () => {
    const fetchImpl = fetchFor(VIEW, (body) => {
      expect(body.method).toBe("goals/get");
      expect(body.payload).toEqual({ args: { agentId: "s1" } });
    });
    await expect(getGoal("s1", { fetchImpl })).resolves.toEqual(VIEW);
  });

  it("returns null when the host has no current goal", async () => {
    const fetchImpl = fetchFor(null);
    await expect(getGoal("s1", { fetchImpl })).resolves.toBeNull();
  });

  it("creates with the remoteExportCreate ref shape", async () => {
    const fetchImpl = fetchFor({ ref: { id: "goal-1", revision: 1 } }, (body) => {
      expect(body.method).toBe("goals/create");
      expect(body.payload).toEqual({
        args: { agentId: "s1", request: { objective: "ship it", maxGoalRounds: 2 } },
      });
    });
    await expect(
      createGoal("s1", { objective: "ship it", maxGoalRounds: 2 }, { fetchImpl }),
    ).resolves.toEqual({ id: "goal-1", revision: 1 });
  });

  it("edits, pauses, resumes and clears against the projected ref", async () => {
    const ref = goalRefOf(VIEW);
    expect(ref).toEqual({ id: "goal-1", revision: 2 });

    const edited = { ...VIEW, revision: 3, objective: "ship it better" };
    await expect(
      editGoal("s1", ref, { objective: "ship it better" }, { fetchImpl: fetchFor(edited) }),
    ).resolves.toEqual(edited);

    const paused = { ...VIEW, phase: "paused" as const, activation: "disarmed" as const };
    await expect(pauseGoal("s1", ref, { fetchImpl: fetchFor(paused) })).resolves.toEqual(paused);
    await expect(resumeGoal("s1", ref, { fetchImpl: fetchFor(VIEW) })).resolves.toEqual(VIEW);
    await expect(
      clearGoal("s1", ref, { fetchImpl: fetchFor({ id: "goal-1", revision: 3 }) }),
    ).resolves.toEqual({ id: "goal-1", revision: 3 });
  });
});
