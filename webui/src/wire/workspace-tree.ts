import type { SessionSummary } from "./types";
import type { WorkspaceRoster, WorkspaceView } from "./workspaces";

export interface WorkspaceSessionGroup {
  workspaceId: string | null;
  title: string;
  sessions: SessionSummary[];
}

function sessionWorkspaceMap(items: WorkspaceView[]): Map<string, WorkspaceView> {
  const map = new Map<string, WorkspaceView>();
  for (const workspace of items) {
    for (const sessionId of workspace.sessionIds) {
      map.set(sessionId, workspace);
    }
  }
  return map;
}

export function groupSessionsByWorkspace(
  roster: WorkspaceRoster,
  sessions: SessionSummary[],
  visibleSessionIds: ReadonlySet<string>,
): WorkspaceSessionGroup[] {
  const byWorkspace = sessionWorkspaceMap(roster.items);
  const buckets = new Map<string | null, SessionSummary[]>();
  const order: Array<string | null> = [];

  const push = (workspaceId: string | null, session: SessionSummary): void => {
    if (!visibleSessionIds.has(session.sessionId)) return;
    if (!buckets.has(workspaceId)) {
      buckets.set(workspaceId, []);
      order.push(workspaceId);
    }
    buckets.get(workspaceId)!.push(session);
  };

  for (const workspace of roster.items) {
    for (const sessionId of workspace.sessionIds) {
      const session = sessions.find((s) => s.sessionId === sessionId);
      if (session !== undefined) push(workspace.workspaceId, session);
    }
  }

  for (const session of sessions) {
    if (!visibleSessionIds.has(session.sessionId)) continue;
    if (byWorkspace.has(session.sessionId)) continue;
    push(null, session);
  }

  return order.map((workspaceId) => {
    const workspace =
      workspaceId === null ? null : roster.items.find((w) => w.workspaceId === workspaceId) ?? null;
    return {
      workspaceId,
      title: workspace?.title ?? workspace?.path ?? "未分组会话",
      sessions: buckets.get(workspaceId) ?? [],
    };
  }).filter((group) => group.sessions.length > 0);
}
