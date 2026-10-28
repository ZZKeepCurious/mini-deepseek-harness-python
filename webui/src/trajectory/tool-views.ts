import type { ToolRow } from "./model";

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export interface TodoModel {
  items: TodoItem[];
  done: number;
  total: number;
  activeContent: string | null;
  activeExtra: number;
  completed: number;
  inProgress: number;
  pending: number;
}

export interface ShellModel {
  command: string;
  description: string;
  cwd: string;
  background: boolean;
  body: string;
  output: string;
  stderr: string;
  exitCode: number | null;
  signal: string | null;
  timedOut: boolean;
  stopped: string | null;
  summary: string;
}

export interface SearchMatch {
  lineNumber: number;
  line: string;
}

export interface SearchGroup {
  path: string;
  matches: SearchMatch[];
}

export interface SearchModel {
  kind: "matches" | "paths";
  groups: SearchGroup[];
  paths: string[];
  shown: number;
  fileCount: number;
  total: number | null;
  truncated: boolean;
  recovery: string | null;
  summary: string;
}

export interface ReadLine {
  number: number;
  text: string;
}

export interface ReadModel {
  path: string;
  offset: number;
  lines: ReadLine[];
  totalLines: number;
  lang: string | null;
}

export interface DiffHunk {
  path: string;
  oldText: string | null;
  newText: string;
}

export interface DiffModel {
  operation: "create" | "update";
  path: string;
  hunks: DiffHunk[];
}

export interface ImageModel {
  path: string;
  detail: string;
}

export interface WebModel {
  summary: string;
  text: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseArgs(raw: string): Record<string, unknown> | null {
  try {
    return record(JSON.parse(raw));
  } catch {
    return null;
  }
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function shellModel(row: ToolRow): ShellModel | null {
  if (row.name !== "bash" && row.name !== "pwsh") return null;
  const args = parseArgs(row.arguments);
  if (args === null) return null;
  const command = textOf(args.command);
  if (command === "") return null;

  let body = row.result;
  let exitCode: number | null = null;
  let signal: string | null = null;
  let timedOut = false;
  let stopped: string | null = null;

  const exit = /\n?\[exit code: (\d+)\]$/u.exec(body);
  if (exit !== null) {
    exitCode = Number(exit[1]);
    body = body.slice(0, exit.index);
  }
  const killed = /\n?\[killed by signal: ([^\]\n]+)\]$/u.exec(body);
  if (killed !== null) {
    signal = killed[1];
    body = body.slice(0, killed.index);
  }
  const timeout = /\n?\[timed out after [^\]]+\]$/u.exec(body);
  if (timeout !== null) {
    timedOut = true;
    body = body.slice(0, timeout.index);
  }
  const halt = /\n?\[stopped: ([^\]\n]+)\]$/u.exec(body);
  if (halt !== null) {
    stopped = halt[1];
    body = body.slice(0, halt.index);
  }

  const stderrAt = body.indexOf("\n[stderr]\n");
  const output = stderrAt >= 0 ? body.slice(0, stderrAt) : body;
  const stderr = stderrAt >= 0 ? body.slice(stderrAt + "\n[stderr]\n".length) : "";
  const summary = row.isError
    ? "失败"
    : timedOut
      ? "超时"
      : stopped !== null
        ? "已停止"
        : signal !== null
          ? `信号 ${signal}`
          : exitCode !== null && exitCode !== 0
            ? `退出码 ${exitCode}`
            : "完成";

  return {
    command,
    description: textOf(args.description),
    cwd: textOf(row.meta?.cwd) || textOf(args.cwd),
    background: args.run_in_background === true,
    body,
    output,
    stderr,
    exitCode,
    signal,
    timedOut,
    stopped,
    summary,
  };
}

export function searchModel(row: ToolRow): SearchModel | null {
  if (row.name !== "grep" && row.name !== "glob") return null;
  const args = parseArgs(row.arguments);
  if (args === null) return null;
  const pattern = textOf(args.pattern);
  if (row.name === "grep" ? pattern === "" : pattern.trim() === "") return null;

  if (row.name === "glob") {
    const paths: string[] = [];
    let total: number | null = null;
    let truncated = false;
    let recovery: string | null = null;
    for (const raw of row.result.split(/\r?\n/u)) {
      const line = raw.trim();
      if (line === "") continue;
      const count = /^\(Showing (\d+) of (\d+) paths\./u.exec(line);
      if (count !== null) {
        total = Number(count[2]);
        truncated = Number(count[1]) < Number(count[2]);
        recovery = line;
        continue;
      }
      if (line.startsWith("(")) continue;
      paths.push(line);
    }
    const shown = paths.length;
    const summary = total === null
      ? `${shown} 个文件`
      : truncated
        ? `显示 ${shown}/${total} 个文件`
        : `${shown} 个文件`;
    return {
      kind: "paths",
      groups: [],
      paths,
      shown,
      fileCount: shown,
      total,
      truncated,
      recovery,
      summary,
    };
  }

  if (row.result.trim() === "No matches found.") {
    return {
      kind: "matches",
      groups: [],
      paths: [],
      shown: 0,
      fileCount: 0,
      total: null,
      truncated: false,
      recovery: null,
      summary: "无匹配",
    };
  }

  const groups: SearchGroup[] = [];
  let current: SearchGroup | null = null;
  let truncated = false;
  let recovery: string | null = null;
  let shown = 0;
  for (const raw of row.result.split(/\r?\n/u)) {
    const note = /^\(Showing the first (\d+) matches;/u.exec(raw.trim());
    if (note !== null) {
      truncated = true;
      recovery = raw.trim();
      continue;
    }
    if (raw.trim() === "") continue;
    const match = /^\s+(\d+): (.*)$/u.exec(raw);
    if (match !== null) {
      if (current === null) continue;
      current.matches.push({ lineNumber: Number(match[1]), line: match[2] });
      shown += 1;
      continue;
    }
    if (!raw.startsWith(" ") && raw.endsWith(":")) {
      current = { path: raw.slice(0, -1), matches: [] };
      groups.push(current);
    }
  }
  return {
    kind: "matches",
    groups,
    paths: [],
    shown,
    fileCount: groups.length,
    total: null,
    truncated,
    recovery,
    summary: groups.length === 0
      ? "无匹配"
      : `显示 ${shown} 条匹配 · ${groups.length} 个文件`,
  };
}

export function todoModel(row: ToolRow): TodoModel | null {
  if (row.name !== "todo_write") return null;
  const args = parseArgs(row.arguments);
  if (args === null) return null;
  const raw = args.todos;
  if (!Array.isArray(raw)) return null;
  const items: TodoItem[] = [];
  for (const value of raw) {
    const item = record(value);
    if (item === null) return null;
    const content = textOf(item.content).trim();
    const status = item.status;
    if (content === "") return null;
    if (status !== "pending" && status !== "in_progress" && status !== "completed") return null;
    items.push({ content, status });
  }
  const active = items.filter((item) => item.status === "in_progress");
  const first = active[0]?.content;
  const named = first !== undefined && first !== "";
  return {
    items,
    done: items.filter((item) => item.status === "completed").length,
    total: items.length,
    activeContent: named ? first : null,
    activeExtra: named ? active.length - 1 : 0,
    completed: items.filter((item) => item.status === "completed").length,
    inProgress: active.length,
    pending: items.filter((item) => item.status === "pending").length,
  };
}

export function readModel(row: ToolRow): ReadModel | null {
  if (row.name !== "read") return null;
  const meta = row.meta;
  if (meta === null) return null;
  const path = textOf(meta.path);
  const totalLines = meta.totalLines;
  const offset = meta.offset;
  const rawLines = meta.lines;
  if (path === "" || typeof totalLines !== "number" || typeof offset !== "number") return null;
  if (!Array.isArray(rawLines)) return null;
  const lines: ReadLine[] = [];
  for (const value of rawLines) {
    const line = record(value);
    if (line === null) return null;
    if (typeof line.number !== "number" || typeof line.text !== "string") return null;
    lines.push({ number: line.number, text: line.text });
  }
  return {
    path,
    offset,
    lines,
    totalLines,
    lang: typeof meta.lang === "string" ? meta.lang : null,
  };
}

export function diffModel(row: ToolRow): DiffModel | null {
  if (row.name !== "write" && row.name !== "edit") return null;
  const meta = row.meta;
  if (meta === null) return null;
  const operation = meta.operation;
  if (operation !== "create" && operation !== "update") return null;
  const rawDiffs = meta.diffs;
  if (!Array.isArray(rawDiffs)) return null;
  const hunks: DiffHunk[] = [];
  for (const value of rawDiffs) {
    const diff = record(value);
    if (diff === null) return null;
    if (typeof diff.path !== "string" || typeof diff.newText !== "string") return null;
    if (diff.oldText !== null && typeof diff.oldText !== "string") return null;
    hunks.push({ path: diff.path, oldText: diff.oldText, newText: diff.newText });
  }
  const args = parseArgs(row.arguments);
  const path = hunks[0]?.path ?? textOf(args?.file_path);
  if (path === "") return null;
  return { operation, path, hunks };
}

export function imageModel(row: ToolRow): ImageModel | null {
  if (row.name !== "read_image") return null;
  const args = parseArgs(row.arguments);
  const path = textOf(row.meta?.path) || textOf(args?.file_path);
  if (path === "") return null;
  return { path, detail: row.result };
}

export function webSearchModel(row: ToolRow): WebModel | null {
  if (row.name !== "web_search") return null;
  const args = parseArgs(row.arguments);
  const queries = args?.queries;
  if (!Array.isArray(queries) || queries.length === 0) return null;
  if (!queries.every((query) => typeof query === "string" && query.trim() !== "")) return null;
  return { summary: (queries as string[]).join(" · "), text: row.result };
}

export function webFetchModel(row: ToolRow): WebModel | null {
  if (row.name !== "web_fetch") return null;
  const args = parseArgs(row.arguments);
  const url = textOf(args?.url);
  if (url === "") return null;
  const status = /\(HTTP (\d+)\)/u.exec(row.result);
  return { summary: status === null ? url : `${url} · HTTP ${status[1]}`, text: row.result };
}