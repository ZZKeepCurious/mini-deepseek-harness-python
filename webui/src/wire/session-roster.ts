// Session list row helpers — `session/list` items and `api-session/*` emit patches.

import type { SessionSummary } from "./types";

export function sessionSummaryOf(x: unknown): SessionSummary | null {
  if (typeof x !== "object" || x === null) return null;
  const r = x as Record<string, unknown>;
  if (typeof r.sessionId !== "string" || r.sessionId === "") return null;
  return r as SessionSummary;
}

export function upsertSessionSummary(
  sessions: SessionSummary[],
  row: SessionSummary,
): SessionSummary[] {
  const idx = sessions.findIndex((s) => s.sessionId === row.sessionId);
  if (idx >= 0) {
    const next = [...sessions];
    next[idx] = { ...next[idx], ...row };
    return next;
  }
  return [row, ...sessions];
}

export function removeSessionSummary(sessions: SessionSummary[], sessionId: string): SessionSummary[] {
  return sessions.filter((s) => s.sessionId !== sessionId);
}

export function patchSessionRunning(
  sessions: SessionSummary[],
  sessionId: string,
  running: boolean,
): SessionSummary[] {
  return sessions.map((s) => (s.sessionId === sessionId ? { ...s, running } : s));
}

export function childSessionsOf(
  parentSessionId: string,
  sessions: SessionSummary[],
): SessionSummary[] {
  return sessions
    .filter((s) => s.parentSessionId === parentSessionId)
    .sort((a, b) => String(b.updatedAt ?? 0).localeCompare(String(a.updatedAt ?? 0)));
}

export function parentSessionOf(
  sessionId: string,
  sessions: SessionSummary[],
): SessionSummary | null {
  const self = sessions.find((s) => s.sessionId === sessionId);
  const parentId = self?.parentSessionId;
  if (typeof parentId !== "string" || parentId === "") return null;
  return sessions.find((s) => s.sessionId === parentId) ?? null;
}

export function shortSessionId(sessionId: string): string {
  if (sessionId.length <= 12) return sessionId;
  return `…${sessionId.slice(-8)}`;
}
