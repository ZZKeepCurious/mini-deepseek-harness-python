// workspace/follow roster fold.
// Source: packages/api/workspace-controller feed.ts WorkspaceFollowIncrement.

export interface WorkspaceView {
  workspaceId: string;
  path: string;
  title: string;
  sessionIds: string[];
  createdAt?: string | number;
  updatedAt?: string | number;
}

export interface WorkspaceRoster {
  items: WorkspaceView[];
  archivedSessionIds: string[];
  pinnedSessionIds: string[];
}

export const EMPTY_WORKSPACE_ROSTER: WorkspaceRoster = {
  items: [],
  archivedSessionIds: [],
  pinnedSessionIds: [],
};

export type WorkspaceFollowFrame =
  | { type: "baseline"; value: WorkspaceRoster }
  | { type: "upsert"; workspace: WorkspaceView }
  | { type: "remove"; workspaceId: string }
  | { type: "order"; workspaceIds: string[] }
  | { type: "archived"; archivedSessionIds: string[] }
  | { type: "pinned"; pinnedSessionIds: string[] };

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function workspaceViewOf(value: unknown): WorkspaceView | undefined {
  const record = asRecord(value);
  if (record === null || typeof record.workspaceId !== "string" || typeof record.path !== "string") {
    return undefined;
  }
  return {
    workspaceId: record.workspaceId,
    path: record.path,
    title: typeof record.title === "string" ? record.title : record.workspaceId,
    sessionIds: stringList(record.sessionIds),
    ...(record.createdAt === undefined ? {} : { createdAt: record.createdAt as string | number }),
    ...(record.updatedAt === undefined ? {} : { updatedAt: record.updatedAt as string | number }),
  };
}

export function workspaceFollowFrameOf(value: unknown): WorkspaceFollowFrame | undefined {
  const record = asRecord(value);
  if (record === null || typeof record.type !== "string") return undefined;
  if (record.type === "baseline") {
    const nested = asRecord(record.value);
    if (nested === null) return undefined;
    const items: WorkspaceView[] = [];
    for (const item of Array.isArray(nested.items) ? nested.items : []) {
      const view = workspaceViewOf(item);
      if (view !== undefined) items.push(view);
    }
    return {
      type: "baseline",
      value: {
        items,
        archivedSessionIds: stringList(nested.archivedSessionIds),
        pinnedSessionIds: stringList(nested.pinnedSessionIds),
      },
    };
  }
  if (record.type === "upsert") {
    const workspace = workspaceViewOf(record.workspace);
    return workspace === undefined ? undefined : { type: "upsert", workspace };
  }
  if (record.type === "remove" && typeof record.workspaceId === "string") {
    return { type: "remove", workspaceId: record.workspaceId };
  }
  if (record.type === "order") {
    return { type: "order", workspaceIds: stringList(record.workspaceIds) };
  }
  if (record.type === "archived") {
    return { type: "archived", archivedSessionIds: stringList(record.archivedSessionIds) };
  }
  if (record.type === "pinned") {
    return { type: "pinned", pinnedSessionIds: stringList(record.pinnedSessionIds) };
  }
  return undefined;
}

export function applyWorkspaceFollowFrame(
  current: WorkspaceRoster,
  frame: WorkspaceFollowFrame,
): WorkspaceRoster {
  if (frame.type === "baseline") return frame.value;
  if (frame.type === "upsert") {
    const items = current.items.filter((item) => item.workspaceId !== frame.workspace.workspaceId);
    items.push(frame.workspace);
    return { ...current, items };
  }
  if (frame.type === "remove") {
    return { ...current, items: current.items.filter((item) => item.workspaceId !== frame.workspaceId) };
  }
  if (frame.type === "order") {
    const byId = new Map(current.items.map((item) => [item.workspaceId, item]));
    const items = frame.workspaceIds.flatMap((id) => {
      const item = byId.get(id);
      return item === undefined ? [] : [item];
    });
    for (const item of current.items) {
      if (!frame.workspaceIds.includes(item.workspaceId)) items.push(item);
    }
    return { ...current, items };
  }
  if (frame.type === "archived") return { ...current, archivedSessionIds: frame.archivedSessionIds };
  return { ...current, pinnedSessionIds: frame.pinnedSessionIds };
}
