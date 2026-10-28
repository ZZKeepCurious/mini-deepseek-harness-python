import { webToken } from "./auth";
import { rpc } from "./rpc";

export interface CommandInputDescriptor {
  hint?: string;
  attachments?: boolean;
}

export interface CommandDescriptor {
  name: string;
  description: string;
  definitionId?: string;
  input?: CommandInputDescriptor;
}

export interface SkillDescriptor {
  name: string;
  path?: string;
  description: string;
  whenToUse?: string;
  modelInvocable: boolean;
}

export interface FileReferenceCandidate {
  path: string;
  kind: "file" | "directory";
}

export interface SessionReferenceCandidate {
  sessionId: string;
  label: string;
  displayTitle?: string;
  sameWorkspace?: boolean;
  createdAt?: string | number;
  cwd?: string;
  mention?: string;
}

export interface UploadedFile {
  receiptId: string;
  file: {
    name?: string;
    size?: number;
    mediaType?: string;
    [key: string]: unknown;
  };
}

export interface CommandExecution {
  commandId: string;
  result: { kind: "success" | "error"; text?: string; sourceEventSeq?: number };
}

export type PromptMode = "queue" | "steer";

export type PromptPart =
  | { type: "text"; text: string }
  | { type: "file"; receiptId: string };

export interface UploadOptions {
  base?: string;
  fetchImpl?: typeof fetch;
}

export async function listCommands(sessionId: string): Promise<CommandDescriptor[]> {
  return rpc<CommandDescriptor[]>("commands/list", { agentId: sessionId });
}

export async function listSkills(sessionId: string): Promise<SkillDescriptor[]> {
  const result = await rpc<{ skills: SkillDescriptor[] }>("skills/list", {
    request: { sessionId },
  });
  return result.skills ?? [];
}

export async function listFileReferences(
  sessionId: string,
  query: string,
): Promise<FileReferenceCandidate[]> {
  return rpc<FileReferenceCandidate[]>("fileReferences/list", {
    agentId: sessionId,
    query,
  });
}

export async function listSessionReferences(
  sessionId: string,
  query: string,
): Promise<SessionReferenceCandidate[]> {
  return rpc<SessionReferenceCandidate[]>("sessionReferenceResolver/candidates", {
    agentId: sessionId,
    query,
  });
}

export async function executeCommand(
  sessionId: string,
  line: string,
  submittedAttachments: PromptPart[],
): Promise<CommandExecution | null> {
  return rpc<CommandExecution | null>("commands/execute", {
    agentId: sessionId,
    line,
    submittedAttachments,
  });
}

export async function sendPrompt(
  sessionId: string,
  content: PromptPart[],
  mode: PromptMode,
): Promise<void> {
  await rpc("session/prompt", {
    sessionId,
    requestId: `req-${Date.now()}`,
    mode,
    content,
  });
}

export async function uploadFileBinary(
  sessionId: string,
  file: File,
  opts: UploadOptions = {},
): Promise<UploadedFile> {
  const base = opts.base ?? "";
  const fetchImpl = opts.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const token = webToken();
  const params = new URLSearchParams({ sessionId, name: file.name });
  const response = await fetchImpl(`${base}/api/session/uploadFileBinary?${params.toString()}`, {
    method: "POST",
    headers: {
      "content-type": "application/octet-stream",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: file,
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new Error(`upload failed: HTTP ${response.status}`);
  }
  if (typeof body !== "object" || body === null) {
    throw new Error("upload failed: invalid response");
  }
  const envelope = body as {
    ok?: boolean;
    value?: UploadedFile;
    error?: { code?: string; message?: string };
  };
  if (envelope.ok !== true || !envelope.value) {
    const code = envelope.error?.code ?? "gateway/internal";
    const message = envelope.error?.message ?? "upload failed";
    throw new Error(`${code}: ${message}`);
  }
  return envelope.value;
}

export interface ComposerServices {
  listCommands(sessionId: string): Promise<CommandDescriptor[]>;
  listSkills(sessionId: string): Promise<SkillDescriptor[]>;
  listFileReferences(sessionId: string, query: string): Promise<FileReferenceCandidate[]>;
  listSessionReferences(sessionId: string, query: string): Promise<SessionReferenceCandidate[]>;
  uploadFile(sessionId: string, file: File): Promise<UploadedFile>;
  executeCommand(
    sessionId: string,
    line: string,
    submittedAttachments: PromptPart[],
  ): Promise<CommandExecution | null>;
  sendPrompt(sessionId: string, content: PromptPart[], mode: PromptMode): Promise<void>;
}

export const composerServices: ComposerServices = {
  listCommands,
  listSkills,
  listFileReferences,
  listSessionReferences,
  uploadFile: uploadFileBinary,
  executeCommand,
  sendPrompt,
};
