// `plan` projection fold (packages/plan/plan-mode wire.view).
// pending is a replayable fact: a settled /plan whose target differs from
// the last `plan/mode`, or a still-running command.

import type { EventEnvelope } from "./types";

export interface PlanSelection {
  active: boolean;
  pending: boolean;
}

/** Read one session's `plan` projection. Malformed values mean the unit is absent. */
export function planSelectionOf(value: unknown): PlanSelection | null {
  if (typeof value !== "object" || value === null) return null;
  const active = (value as { active?: unknown }).active;
  const pending = (value as { pending?: unknown }).pending;
  if (typeof active !== "boolean" || typeof pending !== "boolean") return null;
  return { active, pending };
}

/** Effective chip target: invert while a host command is still pending. */
export function planChipTarget(plan: PlanSelection): boolean {
  return plan.pending ? !plan.active : plan.active;
}

/**
 * Fold the plan unit from a session log prefix.
 * Source: miniharness/plan/projection.py fold_plan_projection.
 */
export function foldPlanProjection(events: readonly EventEnvelope[]): PlanSelection {
  let active = false;
  let wanted: boolean | null = null;
  let running: { commandId: string; wanted: boolean } | null = null;
  for (const event of events) {
    const data = event.data;
    if (event.type === "command/run" && data.name === "plan") {
      const args = data.args;
      if (args === undefined || args === null) continue;
      const text = typeof args === "string" ? args : String(args);
      const commandId = data.commandId;
      if (typeof commandId !== "string" || commandId === "") continue;
      running = { commandId, wanted: text.trim() !== "off" };
    } else if (
      event.type === "command/done" &&
      running !== null &&
      data.commandId === running.commandId
    ) {
      wanted = data.kind === "success" && running.wanted !== active ? running.wanted : null;
      running = null;
    } else if (event.type === "plan/mode") {
      if (typeof data.active === "boolean") active = data.active;
      wanted = null;
    }
  }
  const effective = running !== null ? running.wanted : wanted;
  return {
    active,
    pending: effective !== null && effective !== active,
  };
}
