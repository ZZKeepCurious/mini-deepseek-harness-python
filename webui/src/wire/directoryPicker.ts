import { rpc } from "./rpc";

export interface DirectoryCrumb {
  name: string;
  path: string;
  hidden: boolean;
}

export interface DirectoryEntry {
  name: string;
  path: string;
  hidden: boolean;
}

export interface DirectoryListResult {
  path: string;
  home: string;
  crumbs: DirectoryCrumb[];
  entries: DirectoryEntry[];
  truncated: boolean;
}

export async function listDirectory(path?: string): Promise<DirectoryListResult> {
  return rpc<DirectoryListResult>("directoryPicker/list", path === undefined ? {} : { path });
}

export async function createDirectory(path: string, name: string): Promise<string> {
  return rpc<string>("directoryPicker/createDirectory", { path, name });
}
