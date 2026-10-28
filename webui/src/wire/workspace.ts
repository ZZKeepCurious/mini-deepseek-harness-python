import { webToken } from "./auth";
import { rpc } from "./rpc";
import type { StreamHandle } from "./mux";

export interface WorkspaceFileEntry {
  name: string;
  type: string;
  size?: number;
}

export interface WorkspaceListResult {
  path: string;
  entries: WorkspaceFileEntry[];
  truncated: boolean;
}

export interface WorkspaceStatResult {
  absolutePath: string;
  version?: string;
  bytes?: number;
}

export interface WorkspaceReadResult extends WorkspaceStatResult {
  offset: number;
  text: string;
  lines: number;
  eof: boolean;
}

export type WorkspaceChangeFrame =
  | { kind: "ready" }
  | {
      kind: "change";
      change: { absolutePath: string; version?: string; absent?: true };
    };


export async function listWorkspaceFiles(
  sessionId: string,
  path: string,
): Promise<WorkspaceListResult> {
  return rpc<WorkspaceListResult>("workspaceFiles/list", {
    workspaceFileScopeId: sessionId,
    path,
  });
}

export async function statWorkspaceFile(
  sessionId: string,
  path: string,
): Promise<WorkspaceStatResult> {
  return rpc<WorkspaceStatResult>("workspaceFiles/stat", {
    workspaceFileScopeId: sessionId,
    path,
  });
}

export async function readWorkspaceFile(
  sessionId: string,
  path: string,
  offset = 1,
  limit = 2000,
): Promise<WorkspaceReadResult> {
  return rpc<WorkspaceReadResult>("workspaceFiles/read", {
    workspaceFileScopeId: sessionId,
    path,
    offset,
    limit,
  });
}

export function workspaceFileUrl(absolutePath: string): string {
  const params = new URLSearchParams({ path: absolutePath });
  const token = webToken();
  if (token) params.set("token", token);
  return `/api/file?${params.toString()}`;
}

export interface WorkspaceServices {
  list(sessionId: string, path: string): Promise<WorkspaceListResult>;
  stat(sessionId: string, path: string): Promise<WorkspaceStatResult>;
  read(sessionId: string, path: string, offset?: number, limit?: number): Promise<WorkspaceReadResult>;
  watch(sessionId: string, path: string): StreamHandle;
}

export interface WorkspaceMux {
  openStream(endpoint: string, payload: unknown): StreamHandle;
}

export const workspaceServices = {
  list: listWorkspaceFiles,
  stat: statWorkspaceFile,
  read: readWorkspaceFile,
};

/**
 * Bind the workspace reads and the target watch stream to one mux. The watch
 * endpoint is scoped by the directory path itself: a `change` frame means that
 * directory's direct entries may have moved, so the tree re-lists that level.
 */
export function createWorkspaceServices(mux: WorkspaceMux): WorkspaceServices {
  return {
    ...workspaceServices,
    watch: (sessionId, path) =>
      mux.openStream("workspaceFiles/changes", {
        args: { workspaceFileScopeId: sessionId, path },
      }),
  };
}