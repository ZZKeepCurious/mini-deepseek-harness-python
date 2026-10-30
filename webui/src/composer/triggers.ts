import type {
  CommandDescriptor,
  FileReferenceCandidate,
  SessionReferenceCandidate,
  SkillDescriptor,
} from "../wire/composer";
import type { PermissionOption } from "../wire/permissions";
import {
  displayPermissionPreset,
  presetBadge,
  presetDescription,
} from "../app/permissionPresets";

export interface TriggerHit {
  trigger: "/" | "@";
  query: string;
  quoted: boolean;
  start: number;
  end: number;
}

export interface MenuCandidate {
  id: string;
  group: string;
  label: string;
  detail?: string;
  badge?: string;
  kind: "command" | "skill" | "file" | "directory" | "session" | "permission";
  value: string;
  mention?: string;
  /** The host currently has this value selected (permission presets). */
  active?: boolean;
}

const WORD_CHAR = /[\p{L}\p{N}_]/u;
const WHITESPACE = /\s/u;

function boundaryOk(draft: string, index: number, char: "/" | "@"): boolean {
  if (index === 0) return true;
  const prev = draft.charAt(index - 1);
  if (WHITESPACE.test(prev)) return true;
  if (WORD_CHAR.test(prev)) return false;
  if (char === "/") {
    if (prev === "/") return false;
    if (prev === ":" && index >= 2 && !WHITESPACE.test(draft.charAt(index - 2))) return false;
  }
  return true;
}

interface ActiveAtToken {
  prefix: string;
  query: string;
  quoted: boolean;
}

function activeAtToken(line: string, cursor: number): ActiveAtToken | undefined {
  const beforeCursor = line.slice(0, cursor);
  const quoted = /(?:^|\s)(@"([^"]*))$/u.exec(beforeCursor);
  if (quoted?.[1] !== undefined && quoted[2] !== undefined) {
    return { prefix: quoted[1], query: quoted[2], quoted: true };
  }
  const plain = /(?:^|\s)(@([^\s]*))$/u.exec(beforeCursor);
  if (plain?.[1] === undefined || plain[2] === undefined) return undefined;
  return { prefix: plain[1], query: plain[2], quoted: false };
}

export function detectTrigger(draft: string, caret: number): TriggerHit | null {
  const cursor = Math.max(0, Math.min(caret, draft.length));
  const at = activeAtToken(draft, cursor);
  if (at !== undefined) {
    return {
      trigger: "@",
      query: at.query,
      quoted: at.quoted,
      start: cursor - at.prefix.length,
      end: cursor,
    };
  }
  for (let i = cursor - 1; i >= 0; i--) {
    const char = draft.charAt(i);
    if (WHITESPACE.test(char)) return null;
    if (char !== "/") continue;
    if (!boundaryOk(draft, i, char)) continue;
    return {
      trigger: "/",
      query: draft.slice(i + 1, cursor),
      quoted: false,
      start: i,
      end: cursor,
    };
  }
  return null;
}

export function replaceRange(
  draft: string,
  start: number,
  end: number,
  insertion: string,
): { text: string; caret: number } {
  return {
    text: `${draft.slice(0, start)}${insertion}${draft.slice(end)}`,
    caret: start + insertion.length,
  };
}

/** Argument span of a `/permission <preset>` line under the caret. */
export interface PermissionArgumentHit {
  query: string;
  start: number;
  end: number;
}

const PERMISSION_LINE = /^\/permission(?:[ \t]+([^\n\r]*))?$/u;

/**
 * Detect the `/permission` argument slot: the current line is the bare command
 * plus its first argument. A command with other commands' text or a later line
 * is out of scope.
 */
export function detectPermissionArguments(
  draft: string,
  caret: number,
): PermissionArgumentHit | null {
  const cursor = Math.max(0, Math.min(caret, draft.length));
  const lineStart = draft.lastIndexOf("\n", cursor - 1) + 1;
  const match = PERMISSION_LINE.exec(draft.slice(lineStart, cursor));
  if (match === null) return null;
  const query = match[1] ?? "";
  return { query, start: cursor - query.length, end: cursor };
}

/** Preset choices for the `/permission` argument slot, active value marked. */
export function permissionCandidates(
  options: readonly PermissionOption[],
  currentValue: string | null,
  query: string,
): MenuCandidate[] {
  return rankCandidates(
    options.map((option) => ({
      id: `permission:${option.value}`,
      group: "权限模式",
      label: displayPermissionPreset(option.value, option.name),
      detail: presetDescription(option),
      badge: presetBadge(option.value),
      kind: "permission",
      value: option.value,
      active: option.value === currentValue,
    })),
    query,
  );
}

export function formatFileMention(
  candidate: FileReferenceCandidate,
  preserveQuote: boolean,
): string | undefined {
  const path = candidate.kind === "directory" ? `${candidate.path}/` : candidate.path;
  if (/[\u0000-\u001f\u007f-\u009f"]/u.test(path)) return undefined;
  const quoted = preserveQuote || /\s/u.test(path);
  if (!quoted) return `@${path}`;
  if (candidate.kind === "directory") return `@"${path}`;
  return `@"${path}"`;
}

function escapeSessionLabel(label: string): string {
  return label.replace(/[\\\]]/g, (match) => `\\${match}`);
}

function base64url(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/u, "");
}

export function sessionReferenceUri(sessionId: string): string {
  return `dsh-session:${base64url(JSON.stringify(sessionId))}`;
}

export function formatSessionMention(candidate: SessionReferenceCandidate): string {
  if (candidate.mention) return candidate.mention;
  const label = escapeSessionLabel(candidate.displayTitle || candidate.label || candidate.sessionId);
  return `@[${label}](${sessionReferenceUri(candidate.sessionId)})`;
}

export function parseSlashLine(line: string): { name: string; args: string } | null {
  const match = /^\/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])/u.exec(line.trimStart());
  if (match === null) return null;
  return { name: match[1], args: line.trimStart().slice(match[0].length) };
}

export function commandCandidate(command: CommandDescriptor): MenuCandidate {
  return {
    id: `command:${command.name}`,
    group: "Commands",
    label: command.name,
    detail: command.description,
    kind: "command",
    value: command.name,
  };
}

export function skillCandidate(skill: SkillDescriptor): MenuCandidate {
  return {
    id: `skill:${skill.name}`,
    group: "Skills",
    label: skill.name,
    detail: skill.modelInvocable
      ? "Available to both model and user invocation"
      : "Available only to user invocation",
    badge: skill.modelInvocable ? undefined : "user-only",
    kind: "skill",
    value: skill.name,
  };
}

function score(query: string, candidate: MenuCandidate): number {
  if (query === "") return 0;
  const needle = query.toLocaleLowerCase();
  const label = candidate.label.toLocaleLowerCase();
  if (label.startsWith(needle)) return 0;
  if (label.includes(needle)) return 1;
  let at = 0;
  for (const char of label) {
    if (char === needle[at]) at += 1;
    if (at === needle.length) return 2;
  }
  if (candidate.detail?.toLocaleLowerCase().includes(needle)) return 3;
  // Machine values are typeable for slots whose label is product copy
  // (`/permission auto` names the preset, not its localized label).
  if (candidate.value.toLocaleLowerCase().includes(needle)) return 4;
  return -1;
}

export function rankCandidates(
  candidates: readonly MenuCandidate[],
  query: string,
): MenuCandidate[] {
  const normalized = query.trim();
  if (normalized === "") return candidates.slice();
  return candidates
    .map((candidate, index) => ({ candidate, index, rank: score(normalized, candidate) }))
    .filter((row) => row.rank >= 0)
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map((row) => row.candidate);
}

export function slashCandidates(
  commands: readonly CommandDescriptor[],
  skills: readonly SkillDescriptor[],
  query: string,
): MenuCandidate[] {
  return [
    ...rankCandidates(commands.map(commandCandidate), query),
    ...rankCandidates(skills.map(skillCandidate), query),
  ];
}

export function fileCandidates(
  files: readonly FileReferenceCandidate[],
  query: string,
): MenuCandidate[] {
  return rankCandidates(
    files.map((file) => ({
      id: `file:${file.kind}:${file.path}`,
      group: "Files & folders",
      label: file.kind === "directory" ? `${file.path}/` : file.path,
      detail: file.kind === "directory" ? "Browse folder" : undefined,
      kind: file.kind,
      value: file.path,
    })),
    query,
  );
}

export function sessionCandidates(
  sessions: readonly SessionReferenceCandidate[],
  query: string,
): MenuCandidate[] {
  return rankCandidates(
    sessions.map((session) => ({
      id: `session:${session.sessionId}`,
      group: "Sessions",
      label: session.displayTitle || session.label || session.sessionId,
      detail: session.cwd,
      kind: "session",
      value: session.sessionId,
      mention: session.mention,
    })),
    query,
  );
}
