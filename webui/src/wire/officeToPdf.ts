import { rpc } from "./rpc";

export interface RenderedDocumentBytes {
  absolutePath: string;
  version?: string;
  offset: number;
  eof: boolean;
  bytes: number;
  data: Uint8Array;
  missingFonts: string[];
  generation: string;
}

export async function getOfficeToPdfGeneration(): Promise<string> {
  const value = await rpc<{ generation: string }>("officeToPdf/getGeneration", {});
  return value.generation;
}

export async function renderOfficeToPdf(
  sessionId: string,
  path: string,
  priority: "foreground" | "background" = "foreground",
): Promise<RenderedDocumentBytes> {
  return rpc<RenderedDocumentBytes>("officeToPdf/render", {
    workspaceFileScopeId: sessionId,
    path,
    priority,
  });
}
