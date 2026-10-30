// Timed / named user-question wait.
// Source: packages/client/ui-user-questions PendingQuestion + locales.ts
// and packages/interaction/user-questions AskUserQuestionRequestEvent.wait.

import type { QuestionItem } from "./questions";

export interface QuestionWait {
  callId: string;
  timed: boolean;
}

export type QuestionWaitState = "counting" | "focused" | "editing" | "waiting" | "continued";

export type QuestionChannel = "waterfall" | "rpc" | "none";

export interface QuestionClock {
  waitState: QuestionWaitState;
  remainingMs?: number;
  running?: boolean;
  channel: QuestionChannel;
  dismissal: "hide" | "cancel";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

export function questionWaitOf(value: unknown): QuestionWait | undefined {
  const record = asRecord(value);
  if (record === null || typeof record.callId !== "string" || record.callId === "") {
    return undefined;
  }
  return { callId: record.callId, timed: record.timed === true };
}

export function questionCardKey(
  agentId: string,
  wait: QuestionWait | undefined,
  eventId?: string,
): string {
  if (wait !== undefined) return `question:${agentId}:${wait.callId}`;
  return eventId ?? `question:${agentId}`;
}

export function questionWaitStatus(state: QuestionWaitState, remainingMs: number): string {
  const seconds = Math.ceil(remainingMs / 1000);
  if (state === "continued") return "已继续工作，仍可回答";
  if (state === "waiting" || state === "editing") return "会一直等你回答";
  if (state === "focused") return `已暂停 · 剩余 ${seconds} 秒`;
  return `${seconds} 秒后继续工作`;
}

export function questionTakeTimeVisible(state: QuestionWaitState, hasCountdown: boolean): boolean {
  return hasCountdown && (state === "counting" || state === "focused");
}

export interface QuestionCardInit {
  eventId: string;
  agentId: string;
  questions: QuestionItem[];
  wait?: QuestionWait;
}

export class QuestionCard {
  readonly eventId: string;
  readonly agentId: string;
  readonly questions: QuestionItem[];
  readonly wait?: QuestionWait;
  readonly key: string;
  readonly dismissal: "hide" | "cancel";

  #state: "open" | "continued" = "open";
  #held = false;
  #engaged = false;
  #focused = false;
  #timedWait = false;
  #deadline: number | undefined;
  #remainingMs: number | undefined;
  #channel: QuestionChannel = "waterfall";

  constructor(init: QuestionCardInit) {
    this.eventId = init.eventId;
    this.agentId = init.agentId;
    this.questions = init.questions;
    this.wait = init.wait;
    this.key = questionCardKey(init.agentId, init.wait, init.eventId);
    this.dismissal = init.wait === undefined ? "cancel" : "hide";
    if (init.wait?.timed === true) this.#timedWait = true;
  }

  attachDeadline(remainingMs: number, now: number): void {
    this.#timedWait = true;
    this.#remainingMs = remainingMs;
    if (!this.#held && !this.#engaged) this.#deadline = now + remainingMs;
    this.#channel = "waterfall";
  }

  snapshot(now: number): QuestionClock {
    const waitState: QuestionWaitState = this.#state === "continued"
      ? "continued"
      : this.#held ? "waiting"
        : this.#engaged ? "editing"
          : this.#focused ? "focused"
            : "counting";
    const remainingMs = this.#deadline !== undefined
      ? Math.max(0, this.#deadline - now)
      : this.#remainingMs;
    const showCountdown = this.#channel !== "none"
      && this.#state !== "continued"
      && remainingMs !== undefined
      && (this.#deadline !== undefined || this.#focused || this.#engaged);
    return {
      waitState,
      remainingMs: showCountdown ? remainingMs : undefined,
      ...(showCountdown ? { running: this.#deadline !== undefined } : {}),
      channel: this.#channel,
      dismissal: this.dismissal,
    };
  }

  holdFocus(now: number): void {
    if (this.#held || this.#engaged || this.#focused) return;
    this.#remainingMs = this.#deadline === undefined ? undefined : Math.max(0, this.#deadline - now);
    this.#deadline = undefined;
    this.#focused = true;
  }

  releaseFocus(now: number): void {
    if (!this.#focused) return;
    this.#focused = false;
    this.#deadline = this.#timedWait && this.#remainingMs !== undefined
      ? now + this.#remainingMs
      : undefined;
    this.#remainingMs = undefined;
  }

  engage(now: number): void {
    if (this.#held || this.#engaged) return;
    if (this.#remainingMs === undefined && this.#deadline !== undefined) {
      this.#remainingMs = Math.max(0, this.#deadline - now);
    }
    this.#engaged = true;
    this.#focused = false;
    this.#deadline = undefined;
  }

  takeTime(): void {
    if (this.#held) return;
    this.#held = true;
    this.#focused = false;
    this.#remainingMs = undefined;
    this.#deadline = undefined;
  }

  tick(now: number): "timeout" | undefined {
    if (this.#held || this.#engaged || this.#focused) return undefined;
    if (this.#deadline === undefined || now < this.#deadline) return undefined;
    this.#channel = "none";
    this.#timedWait = false;
    this.#deadline = undefined;
    this.#remainingMs = undefined;
    return "timeout";
  }

  setContinued(): void {
    this.#state = "continued";
    this.#channel = "rpc";
  }
}

export interface ActiveUserQuestion {
  callId: string;
  state: "open" | "continued";
}

export function activeUserQuestionsOf(value: unknown): ActiveUserQuestion[] {
  const record = asRecord(value);
  if (record === null || !Array.isArray(record.active)) return [];
  const active: ActiveUserQuestion[] = [];
  for (const item of record.active) {
    const row = asRecord(item);
    if (row === null || typeof row.callId !== "string") continue;
    active.push({
      callId: row.callId,
      state: row.state === "continued" ? "continued" : "open",
    });
  }
  return active;
}
