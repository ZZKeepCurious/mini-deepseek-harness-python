// `goals` Remote namespace (packages/goal/goal host plane).
// Unary `POST /api/goals/<method>`; Agent identity is `agentId`.
// Source: docs/interface-wire.md §4.12 + miniharness/web/api.py goals_*.

import { rpc, type RpcOptions } from "./rpc";

export type GoalPhase = "active" | "paused" | "blocked" | "complete";
export type GoalActivation = "armed" | "disarmed";

export interface GoalRef {
  id: string;
  revision: number;
}

export interface GoalBlockedReason {
  code: string;
  message: string;
}

export interface GoalView {
  id: string;
  revision: number;
  objective: string;
  phase: GoalPhase;
  maxGoalRounds: number;
  blockedReason?: GoalBlockedReason;
  roundsStarted?: number;
  createdAt?: number;
  updatedAt?: number;
  activation?: GoalActivation;
}

export interface GoalCreateRequest {
  objective: string;
  maxGoalRounds?: number;
}

export interface GoalEditRequest {
  objective?: string;
  maxGoalRounds?: number;
}

const PHASES = new Set<GoalPhase>(["active", "paused", "blocked", "complete"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function blockedReasonOf(value: unknown): GoalBlockedReason | undefined {
  if (!isRecord(value)) return undefined;
  const code = value.code;
  const message = value.message;
  if (typeof code !== "string" || code === "") return undefined;
  if (typeof message !== "string" || message === "") return undefined;
  return { code, message };
}

/** Decode one host GoalView. Null / tombstone / unknown phase are absent. */
export function goalViewOf(value: unknown): GoalView | null {
  if (!isRecord(value)) return null;
  const id = value.id;
  const revision = value.revision;
  const objective = value.objective;
  const phase = value.phase;
  const maxGoalRounds = value.maxGoalRounds;
  if (typeof id !== "string" || id === "") return null;
  if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 1) return null;
  if (typeof objective !== "string" || objective === "") return null;
  if (typeof phase !== "string" || !PHASES.has(phase as GoalPhase)) return null;
  if (typeof maxGoalRounds !== "number" || !Number.isSafeInteger(maxGoalRounds) || maxGoalRounds < 1) {
    return null;
  }
  const blocked = blockedReasonOf(value.blockedReason);
  const activation = value.activation;
  return {
    id,
    revision,
    objective,
    phase: phase as GoalPhase,
    maxGoalRounds,
    ...(blocked === undefined ? {} : { blockedReason: blocked }),
    ...(typeof value.roundsStarted === "number" ? { roundsStarted: value.roundsStarted } : {}),
    ...(typeof value.createdAt === "number" ? { createdAt: value.createdAt } : {}),
    ...(typeof value.updatedAt === "number" ? { updatedAt: value.updatedAt } : {}),
    ...(activation === "armed" || activation === "disarmed" ? { activation } : {}),
  };
}

export function goalRefOf(view: GoalView): GoalRef {
  return { id: view.id, revision: view.revision };
}

function refOf(value: unknown): GoalRef {
  if (!isRecord(value)) {
    throw new TypeError("goals: missing ref");
  }
  const id = value.id;
  const revision = value.revision;
  if (typeof id !== "string" || id === "") {
    throw new TypeError("goals: missing ref id");
  }
  if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 1) {
    throw new TypeError("goals: missing ref revision");
  }
  return { id, revision };
}

export async function getGoal(agentId: string, opts?: RpcOptions): Promise<GoalView | null> {
  const value = await rpc<unknown>("goals/get", { agentId }, opts);
  return goalViewOf(value);
}

export async function createGoal(
  agentId: string,
  request: GoalCreateRequest,
  opts?: RpcOptions,
): Promise<GoalRef> {
  const value = await rpc<{ ref?: unknown }>("goals/create", { agentId, request }, opts);
  return refOf(value?.ref);
}

export async function editGoal(
  agentId: string,
  ref: GoalRef,
  request: GoalEditRequest,
  opts?: RpcOptions,
): Promise<GoalView> {
  const view = goalViewOf(await rpc("goals/edit", { agentId, ref, request }, opts));
  if (view === null) throw new TypeError("goals/edit: invalid view");
  return view;
}

export async function pauseGoal(agentId: string, ref: GoalRef, opts?: RpcOptions): Promise<GoalView> {
  const view = goalViewOf(await rpc("goals/pause", { agentId, ref }, opts));
  if (view === null) throw new TypeError("goals/pause: invalid view");
  return view;
}

export async function resumeGoal(agentId: string, ref: GoalRef, opts?: RpcOptions): Promise<GoalView> {
  const view = goalViewOf(await rpc("goals/resume", { agentId, ref }, opts));
  if (view === null) throw new TypeError("goals/resume: invalid view");
  return view;
}

export async function completeGoal(
  agentId: string,
  ref: GoalRef,
  opts?: RpcOptions,
): Promise<GoalView> {
  const view = goalViewOf(await rpc("goals/complete", { agentId, ref }, opts));
  if (view === null) throw new TypeError("goals/complete: invalid view");
  return view;
}

export async function clearGoal(agentId: string, ref: GoalRef, opts?: RpcOptions): Promise<GoalRef> {
  return refOf(await rpc("goals/clear", { agentId, ref }, opts));
}
