// schedule Remote namespace (§4.9): list/delete for the active session roster.

import { rpc } from "./rpc";

export interface ScheduleRecord {
  id: string;
  kind: string;
  title: string;
  prompt: string;
  scheduledAt: string;
}

export interface ScheduleCatalogEntry extends ScheduleRecord {
  sessionId: string;
  status: string;
  lastDelivery?: string;
}

export function scheduleRecordOf(x: unknown): ScheduleRecord | null {
  if (typeof x !== "object" || x === null) return null;
  const r = x as Record<string, unknown>;
  if (typeof r.id !== "string" || r.id === "") return null;
  if (typeof r.kind !== "string" || r.kind === "") return null;
  if (typeof r.title !== "string") return null;
  if (typeof r.prompt !== "string") return null;
  if (typeof r.scheduledAt !== "string" || r.scheduledAt === "") return null;
  return {
    id: r.id,
    kind: r.kind,
    title: r.title,
    prompt: r.prompt,
    scheduledAt: r.scheduledAt,
  };
}

export function parseScheduleList(value: unknown): ScheduleRecord[] {
  if (!Array.isArray(value)) return [];
  const out: ScheduleRecord[] = [];
  for (const item of value) {
    const record = scheduleRecordOf(item);
    if (record !== null) out.push(record);
  }
  return sortSchedules(out);
}

export function sortSchedules(records: ScheduleRecord[]): ScheduleRecord[] {
  return [...records].sort((a, b) => {
    const at = a.scheduledAt.localeCompare(b.scheduledAt);
    if (at !== 0) return at;
    return a.id.localeCompare(b.id);
  });
}

const KIND_LABELS: Record<string, string> = {
  after: "延迟一次",
  at: "定时一次",
  every: "固定间隔",
  daily: "每日",
  weekly: "每周",
  cron: "Cron",
};

export function scheduleKindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export function formatScheduleInstant(iso: string, timeZone?: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
  }).format(new Date(ms));
}

export function scheduleDeleteSucceeded(result: unknown): boolean {
  if (typeof result !== "object" || result === null) return false;
  const r = result as { deleted?: unknown };
  return r.deleted === true;
}

export async function loadSessionSchedules(sessionId: string): Promise<ScheduleRecord[]> {
  const value = await rpc<unknown>("schedule/list", { sessionId });
  return parseScheduleList(value);
}

export async function deleteSessionSchedule(sessionId: string, id: string): Promise<boolean> {
  const result = await rpc<unknown>("schedule/delete", { sessionId, id });
  return scheduleDeleteSucceeded(result);
}

export interface ScheduleDeliveryRecord {
  id: string;
  deliveredAt: string;
  messageId?: string;
}

export function parseScheduleHistory(value: unknown): ScheduleDeliveryRecord[] {
  if (typeof value !== "object" || value === null) return [];
  const record = value as Record<string, unknown>;
  if (record.code === "schedule_not_found") return [];
  const items = Array.isArray(record.items) ? record.items : Array.isArray(record.deliveries) ? record.deliveries : [];
  const out: ScheduleDeliveryRecord[] = [];
  for (const raw of items) {
    if (typeof raw !== "object" || raw === null) continue;
    const row = raw as Record<string, unknown>;
    const id = typeof row.id === "string" ? row.id : typeof row.deliveryId === "string" ? row.deliveryId : "";
    const deliveredAt =
      typeof row.deliveredAt === "string"
        ? row.deliveredAt
        : typeof row.at === "string"
          ? row.at
          : "";
    if (id === "" && deliveredAt === "") continue;
    out.push({
      id: id !== "" ? id : deliveredAt,
      deliveredAt,
      ...(typeof row.messageId === "string" ? { messageId: row.messageId } : {}),
    });
  }
  return out;
}

export async function loadScheduleHistory(
  sessionId: string,
  scheduleId: string,
  limit = 20,
): Promise<ScheduleDeliveryRecord[]> {
  const value = await rpc<unknown>("schedule/history", { sessionId, id: scheduleId, limit });
  return parseScheduleHistory(value);
}

export function scheduleCatalogEntryOf(x: unknown): ScheduleCatalogEntry | null {
  const record = scheduleRecordOf(x);
  if (record === null) return null;
  if (typeof x !== "object" || x === null) return null;
  const row = x as Record<string, unknown>;
  if (typeof row.sessionId !== "string" || row.sessionId === "") return null;
  if (typeof row.status !== "string" || row.status === "") return null;
  return {
    ...record,
    sessionId: row.sessionId,
    status: row.status,
    ...(typeof row.lastDelivery === "string" ? { lastDelivery: row.lastDelivery } : {}),
  };
}

export function parseScheduleCatalog(value: unknown): ScheduleCatalogEntry[] {
  if (!Array.isArray(value)) return [];
  const out: ScheduleCatalogEntry[] = [];
  for (const item of value) {
    const entry = scheduleCatalogEntryOf(item);
    if (entry !== null) out.push(entry);
  }
  return sortSchedules(out);
}

export async function loadScheduleCatalog(): Promise<ScheduleCatalogEntry[]> {
  const value = await rpc<unknown>("schedule/catalog", {});
  return parseScheduleCatalog(value);
}

export function scheduleUpdateSucceeded(result: unknown): boolean {
  if (typeof result !== "object" || result === null) return false;
  const row = result as { updated?: unknown };
  return row.updated === true;
}

export async function updateSessionSchedule(
  sessionId: string,
  id: string,
  expected: ScheduleRecord,
  patch: { title?: string; prompt?: string },
): Promise<boolean> {
  const result = await rpc<unknown>("schedule/update", {
    sessionId,
    id,
    expected,
    ...(patch.title !== undefined ? { title: patch.title } : {}),
    ...(patch.prompt !== undefined ? { prompt: patch.prompt } : {}),
  });
  return scheduleUpdateSucceeded(result);
}
